"""Api: operation and job views and the job resume route."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import (
    Body,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi import Path as ApiPath
from vonk_agent_protocol.lifecycle_vocabulary import OperatorActionName
from vonk_agent_protocol.outcome import InvalidRequestError

from vonk_control.control_limits import MAX_CONTROL_DOCUMENT_BYTES
from vonk_control.operator_receipt_contract import JobOperatorReceipt, ReceiptPolicy

from ..agent_jobs.contracts import OperatorRetirementRefused
from ..api_availability import AVAILABILITY_ERRORS
from ..auth import (
    Actor,
    CursorCodec,
    CursorError,
)
from ..cursor_contract import MAX_CURSOR_LENGTH
from ..failure_evidence import FailureEvidenceService
from ..jobs.operator_receipts import read_job_operator_receipt
from ..model_cache.contract import UUID_PATTERN
from ..operation_api.contracts import (
    JobDetailResponse,
    JobResumeRequest,
    JobResumeResponse,
    OperationApiServices,
    OperationDetailResponse,
    OperationsResponse,
    bounded_error_responses,
)
from ..operation_api.providers import _global_get_operation, _global_list_operations
from ..operation_api.responses import (
    bounded_operation_detail,
    bounded_operations_response,
    decode_offset,
    job_response,
)
from ..operator_acceptance import operator_acceptance
from ..operator_receipt_contract import ReceiptCode
from ..strict_json import warn_unreadable_once
from .common import (
    JobQueue,
)
from .roles import require_mutation_role


def register_job_routes(
    app: FastAPI,
    *,
    authenticated_actor: Any,
    activity_detail: Callable[..., OperationDetailResponse],
    cursor_codec: CursorCodec,
    operations: OperationApiServices | None,
    jobs: JobQueue,
    failure_evidence: FailureEvidenceService | None,
) -> None:
    @app.get(
        "/api/operations",
        response_model=OperationsResponse,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="listOperations",
        openapi_extra={"x-vonk-response-max-bytes": MAX_CONTROL_DOCUMENT_BYTES},
    )
    def operations_view(
        cursor: str | None = Query(default=None, max_length=MAX_CURSOR_LENGTH),
        limit: int = Query(default=20, ge=1, le=100),
        operation_state: str | None = Query(
            default=None,
            alias="state",
            pattern=r"^[a-z][a-z0-9-]{0,31}$",
        ),
        node_id: str | None = Query(default=None, pattern=r"^spk_[0-9a-f]{32}$"),
        request_id: str | None = Query(
            default=None,
            pattern=(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
            ),
        ),
        _actor: Actor = authenticated_actor,
    ) -> OperationsResponse:
        if operations is None:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            )
        try:
            projected_at = operations.clock()
            page = _global_list_operations(
                operations,
                cursor,
                limit,
                operation_state,
                node_id,
                request_id,
                now=projected_at,
            )
        except CursorError:
            raise HTTPException(
                status_code=422, detail="operation cursor is invalid"
            ) from None
        except (TypeError, ValueError):
            return OperationsResponse(
                operations=None,
                total=None,
                projection_issue="Stored operation observations are unreadable; membership and total are unknown.",
            )
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None
        items = [
            activity_detail(item, tolerate_unreadable=True, projected_at=projected_at)
            for item in page.items
        ]
        return bounded_operations_response(
            page,
            items,
            cursors=operations.cursor_codec or cursor_codec,
            state=operation_state,
            node_id=node_id,
            request_id=request_id,
        )

    @app.get(
        "/api/operations/{operation_id}",
        response_model=OperationDetailResponse,
        responses=bounded_error_responses(401, 404, 503),
        operation_id="getOperation",
        openapi_extra={"x-vonk-response-max-bytes": MAX_CONTROL_DOCUMENT_BYTES},
    )
    def operation_view(
        operation_id: str = ApiPath(min_length=1, max_length=128),
        _actor: Actor = authenticated_actor,
    ) -> OperationDetailResponse:
        if operations is None:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            )
        try:
            projected_at = operations.clock()
            item = _global_get_operation(operations, operation_id, now=projected_at)
        except KeyError:
            raise HTTPException(status_code=404, detail="operation not found") from None
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None
        try:
            return bounded_operation_detail(
                activity_detail(
                    item, tolerate_unreadable=True, projected_at=projected_at
                )
            )
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None

    @app.get(
        "/api/jobs/{job_id}",
        response_model=JobDetailResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getJob",
    )
    def job_view(
        job_id: str,
        operation_cursor: str | None = Query(
            default=None, max_length=MAX_CURSOR_LENGTH
        ),
        target_cursor: str | None = Query(default=None, max_length=MAX_CURSOR_LENGTH),
        limit: int = Query(default=20, ge=1, le=100),
        _actor: Actor = authenticated_actor,
    ) -> JobDetailResponse:
        try:
            job = jobs.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found") from None
        try:
            projected = (
                None
                if operations is None
                else operations.job_operations(job_id, operation_cursor, limit)
            )
            return job_response(
                job,
                projected,
                target_cursor=decode_offset(
                    target_cursor,
                    job_id=str(job.id),
                    cursors=cursor_codec,
                ),
                limit=limit,
                cursors=cursor_codec,
                evidence_decorator=failure_evidence.decorate
                if failure_evidence is not None
                else None,
            )
        except CursorError:
            raise HTTPException(
                status_code=422, detail="job cursor is invalid"
            ) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            warn_unreadable_once("job operations", job_id)
            return job_response(
                job,
                None,
                target_cursor=decode_offset(
                    target_cursor, job_id=str(job.id), cursors=cursor_codec
                ),
                limit=limit,
                cursors=cursor_codec,
            )

    @app.get(
        "/api/jobs/{job_id}/requests/{request_id}",
        response_model=JobOperatorReceipt,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getJobOperatorReceipt",
    )
    def job_operator_receipt(
        job_id: str, request_id: str, authenticated: Actor = authenticated_actor
    ) -> JobOperatorReceipt:
        try:
            receipt = read_job_operator_receipt(jobs.get(job_id), request_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="job receipt not found"
            ) from None
        if (
            receipt is None
            or receipt.request_id != request_id
            or receipt.actor != authenticated.subject
        ):
            raise HTTPException(status_code=404, detail="job receipt not found")
        return receipt

    @app.post(
        "/api/jobs/{job_id}/resume",
        response_model=JobResumeResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="resumeJob",
    )
    @operator_acceptance(ReceiptPolicy.JOB)
    def resume_job(
        request: Request,
        job_id: str,
        body: Annotated[JobResumeRequest | None, Body()] = None,
        authenticated: Actor = authenticated_actor,
        request_id: Annotated[
            str | None, Header(alias="X-Request-ID", pattern=UUID_PATTERN)
        ] = None,
    ) -> JobResumeResponse:
        route = "/api/jobs/{job_id}/resume"
        require_mutation_role(authenticated, route)
        if operations is None:
            raise HTTPException(status_code=503, detail="job resume unavailable")
        disposition = (
            OperatorActionName.RETIRE
            if body is not None and body.disposition == OperatorActionName.RETIRE
            else OperatorActionName.RESUME
        )
        try:
            current_job = jobs.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found") from None
        receipt = JobOperatorReceipt(
            job_id=job_id,
            request_id=request_id or request.state.request_id,
            actor=authenticated.subject,
            action=disposition,
            content_digest=current_job.payload_digest,
        )
        if disposition == OperatorActionName.RETIRE:
            if operations.retire_job is None:
                raise HTTPException(
                    status_code=503, detail="job retirement unavailable"
                )
            try:
                operations.retire_job(job_id, receipt=receipt)
            except KeyError:
                raise HTTPException(status_code=404, detail="job not found") from None
            except OperatorRetirementRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            except InvalidRequestError as error:
                raise HTTPException(
                    status_code=409,
                    detail=str(error),
                    headers={
                        "x-vonk-error-code": error.typed_reason or ReceiptCode.MISMATCH
                    },
                ) from None
            except ValueError:
                raise HTTPException(
                    status_code=409, detail="job is not waiting for operator"
                ) from None
            return JobResumeResponse(id=job_id, state="failed")
        try:
            operations.resume_job(job_id, receipt=receipt)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found") from None
        except InvalidRequestError as error:
            raise HTTPException(
                status_code=409,
                detail=str(error),
                headers={
                    "x-vonk-error-code": error.typed_reason or ReceiptCode.MISMATCH
                },
            ) from None
        except ValueError:
            raise HTTPException(
                status_code=409, detail="job is not waiting for operator"
            ) from None
        return JobResumeResponse(id=job_id, state="queued")
