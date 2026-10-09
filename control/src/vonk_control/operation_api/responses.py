"""Operation Api: responses."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from vonk_agent_protocol import (
    OperationFailureCode,
    OperationProgress,
    canonical_message,
)
from vonk_agent_protocol.contracts import AgentFailureResult

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .. import agent_operation_states
from ..auth import CursorCodec, CursorError
from ..bounded_json import BoundedJSONError, require_sequence
from ..fleet_profile_contract import FleetProfileApplicationCancellationView
from ..models import AgentOperation, AgentOperationAttempt
from ..operation_contract import (
    OperationFailure,
    OperationFailureEvidence,
    OperationRecoveryAction,
    recovery_for_operation,
)
from ..operation_item_contract import (
    AGENT_OPERATION_KINDS,
    OperationItem,
    OperationResultFacts,
    OperationRow,
    agent_receipt_for,
    operation_item,
)
from ..operation_progress import project_progress_for_state
from ..strict_json import (
    StrictModel,
    read_stored_model,
    serialize_json_value,
    warn_unreadable_once,
)
from .constants import NodeIdentifier
from .contracts import (
    JobDetailResponse,
    JobOperationProgress,
    JobOperationResponse,
    OperationDetailResponse,
    OperationListPage,
    OperationPage,
    OperationProjectionIssue,
    OperationsResponse,
)
from .diagnostics import _aware
from .providers import _operation_boundary


def _required_text(value: object, detail: str) -> str:
    """Read a required persisted string, failing closed on a wrong JSON type."""

    if not isinstance(value, str):
        raise BoundedJSONError(detail)
    return value


def _optional_text(value: object, detail: str) -> str | None:
    """Read an optional persisted string without coercing a wrong JSON type."""

    return None if value is None else _required_text(value, detail)


def _required_bool(value: object, detail: str) -> bool:
    """Read a required persisted boolean, rejecting Python truthiness."""

    if not isinstance(value, bool):
        raise BoundedJSONError(detail)
    return value


def _required_node_ids(value: object) -> list[NodeIdentifier]:
    """Read a persisted node-id array, failing closed on a wrong shape."""

    return [
        _required_text(member, "operation node id is invalid")
        for member in require_sequence(value, "operation node ids are invalid")
    ]


def _result_uncertain(item: OperationItem) -> bool:
    return item.result_unreadable or (
        item.result is not None and item.result.is_uncertain
    )


def _job_operation_response(
    item: OperationItem, *, now: datetime | None = None
) -> JobOperationResponse:
    """Project one durable job operation member."""

    return JobOperationResponse(
        id=item.id,
        node_id=_required_text(item.node_id, "operation node id is invalid"),
        kind=item.kind,
        state=item.state,
        attempt=item.attempt,
        progress=_progress_projection(item.progress, item.state, now=now),
        updated_at=item.updated_at,
        failure=_item_failure(item),
        evidence_download=item.evidence_download,
        recovery=recovery_for_operation(
            item.state,
            supported_actions=item.supported_actions,
            available_actions=(OperationRecoveryAction.RESUME,),
            uncertain=_result_uncertain(item),
        ),
    )


def job_response(
    job: Any,
    operation_page: OperationPage | None,
    *,
    target_cursor: int,
    limit: int,
    cursors: CursorCodec,
    evidence_decorator: Callable[[OperationItem], OperationItem] | None = None,
) -> JobDetailResponse:
    projected = None
    if operation_page is not None:
        try:
            items = [operation_item(row) for row in operation_page.items]
            if evidence_decorator is not None:
                items = [evidence_decorator(item) for item in items]
            projected = [
                _job_operation_response(item, now=operation_page.projected_at)
                for item in items
            ]
        except (OSError, RuntimeError, TypeError, ValueError):
            warn_unreadable_once("job operations", job.id)
            operation_page = None
    targets = list(job.targets)
    visible_targets = targets[target_cursor : target_cursor + limit]
    target_next_cursor = (
        _encode_offset(
            target_cursor + limit,
            job_id=str(job.id),
            cursors=cursors,
        )
        if target_cursor + limit < len(targets)
        else None
    )
    diagnostics = (
        None if operation_page is None else operation_page.agent_upgrade_diagnostics
    )
    operator_summary = None if diagnostics is None else diagnostics.operator_summary
    return JobDetailResponse(
        id=job.id,
        state=job.state,
        kind=job.kind,
        authority_revision=job.authority_revision,
        targets=visible_targets,
        target_next_cursor=target_next_cursor,
        target_total=len(targets),
        current_attempt=job.current_attempt,
        status_reason=(
            operator_summary if isinstance(operator_summary, str) else job.status_reason
        ),
        operations=projected,
        operation_next_cursor=None
        if operation_page is None
        else operation_page.next_cursor,
        operation_total=None
        if operation_page is None
        else operation_page.progress.total,
        progress=None if operation_page is None else operation_page.progress,
        projection_issue=(
            "Operation observations are unavailable; progress and step membership are unknown."
            if operation_page is None
            else None
        ),
        agent_upgrade_diagnostics=diagnostics,
        recovery=recovery_for_operation(
            job.state,
            supported_actions=()
            if operation_page is None
            else operation_page.recovery_actions,
            available_actions=(OperationRecoveryAction.RESUME,),
        ),
    )


def _encode_offset(offset: int, *, job_id: str, cursors: CursorCodec) -> str:
    return cursors.encode(
        resource="job-targets",
        order="index-asc/v1",
        context={"job_id": job_id},
        boundary=offset,
    )


def decode_offset(
    cursor: str | None,
    *,
    job_id: str,
    cursors: CursorCodec,
) -> int:
    if cursor is None:
        return 0
    try:
        offset = cursors.decode(
            cursor,
            resource="job-targets",
            order="index-asc/v1",
            context={"job_id": job_id},
        )
    except (UnicodeError, ValueError):
        raise CursorError("target cursor is invalid") from None
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CursorError("target cursor is invalid")
    return offset


def _progress_projection(
    value: object, state: object = None, *, now: datetime | None = None
) -> JobOperationProgress | None:
    if value is None:
        return None
    return project_progress_for_state(
        value
        if isinstance(value, JobOperationProgress)
        else read_stored_model(JobOperationProgress, value, strict=True),
        state,
        None if now is None else _aware(now),
    )


def _failure_projection(
    result: OperationResultFacts | None,
) -> OperationFailureEvidence | None:
    """Project a family's stored result into its bounded failure model."""

    return None if result is None else result.failure_evidence()


def _item_failure(item: OperationItem) -> OperationFailure | None:
    """Select the authoritative contract by producer, before union egress."""
    if item.failure_recorded:
        return item.failure
    if item.kind in AGENT_OPERATION_KINDS:
        if item.state not in {"failed", *agent_operation_states.PARKED}:
            return None
        if item.result_unreadable:
            return OperationFailureEvidence(
                error_code=OperationFailureCode.STORED_RESULT_UNREADABLE,
                summary="Stored operation result is unreadable",
                detail="The durable operation identity and state remain known; its failure receipt cannot be verified.",
                uncertain=True,
            )
        parsed = item.agent_receipt
        if parsed is None:
            return None
        if isinstance(parsed, AgentFailureResult):
            return parsed
        # A job process receipt has its own canonical result contract. Its
        # complete manifest remains on the artifact-job result endpoint.
        reason = (
            parsed.reason or f"Artifact process exited with code {parsed.exit_code}"
        )
        return OperationFailureEvidence(
            error_code=OperationFailureCode.ARTIFACT_PROCESS_FAILED,
            summary=reason[:256],
            detail=reason,
        )
    return _failure_projection(item.result)


def _advertised_actions(value: object) -> list[str] | None:
    """The actions a worker advertised; anything but a list of words is none."""

    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _operation_item(
    operation: AgentOperation,
    attempt: AgentOperationAttempt | None,
    *,
    now: datetime | None = None,
) -> OperationItem:
    """Project one durable operation without exposing its unbounded payload."""

    progress = None
    result = None
    status_reason = operation.status_reason
    if attempt is not None:
        try:
            progress = _progress_projection(attempt.progress, operation.state, now=now)
        except (TypeError, ValueError):
            progress_issue = "Stored progress evidence is unreadable; operation identity and state remain known."
            if status_reason is None:
                status_reason = progress_issue
        result = attempt.result
    receipt, unreadable = agent_receipt_for(operation.kind, operation.state, result)
    return OperationItem(
        attempt=operation.current_attempt,
        id=operation.id,
        kind=operation.kind,
        node_ids=[operation.node_id],
        parent_id=operation.parent_job_id,
        progress=progress,
        result=(
            None
            if result is None or unreadable
            else OperationResultFacts.model_validate(result)
        ),
        agent_receipt=receipt,
        result_unreadable=unreadable,
        supported_actions=(
            _advertised_actions(operation.payload.get("supported_actions"))
            if isinstance(operation.payload, Mapping)
            else None
        ),
        state=operation.state,
        status_reason=status_reason,
        updated_at=_aware(operation.updated_at).isoformat(),
    )


def operation_detail_response(
    row: OperationRow, *, available_actions: object = (), now: datetime | None = None
) -> OperationDetailResponse:
    """Build the bounded generic read representation from a durable projection."""

    item = operation_item(row)
    failure = _item_failure(item)
    return OperationDetailResponse(
        id=item.id,
        parent_id=item.parent_id,
        node_ids=item.node_ids,
        kind=item.kind,
        state=item.state,
        attempt=item.attempt,
        progress=_progress_projection(item.progress, item.state, now=now),
        created_at=item.created_at if _operation_boundary(item) is not None else None,
        observation_unavailable=_operation_boundary(item) is None
        or item.result_unreadable,
        updated_at=item.updated_at,
        failure=failure,
        evidence_download=item.evidence_download,
        cancellation=item.cancellation,
        model_cache_cancellation=item.model_cache_cancellation,
        status_reason=item.status_reason,
        recovery=recovery_for_operation(
            item.state,
            supported_actions=item.supported_actions,
            available_actions=available_actions,
            uncertain=bool(failure is not None and getattr(failure, "uncertain", False))
            or _result_uncertain(item),
        ),
        owner=item.owner,
        blockers=item.blockers or [],
        next_attempt_at=item.next_attempt_at,
    )


def _response_bytes(response: StrictModel) -> int:
    # JSONResponse emits compact UTF-8; key ordering does not affect byte size.
    return len(canonical_message(serialize_json_value(response)))


def bounded_operation_detail(
    detail: OperationDetailResponse, *, envelope_bytes: int = 0
) -> OperationDetailResponse:
    """Keep known durable facts; identify an optional decoration that cannot fit.

    This is an observation projection, never an admission or execution rewrite.
    The exact outer envelope is included when a detail is the first list item.
    """
    observed = _response_bytes(detail) + envelope_bytes
    if observed <= MAX_CONTROL_DOCUMENT_BYTES:
        return detail
    issues = list(detail.projection_issues or [])
    optional_facts: list[
        tuple[
            Literal["progress", "cancellation"],
            OperationProgress | FleetProfileApplicationCancellationView | None,
        ]
    ] = [("progress", detail.progress), ("cancellation", detail.cancellation)]
    decorations = sorted(
        optional_facts,
        key=lambda pair: len(canonical_message(pair[1])) if pair[1] is not None else 0,
        reverse=True,
    )
    for field, value in decorations:
        if value is None:
            continue
        issues.append(
            OperationProjectionIssue(
                field=field,
                observed_bytes=observed,
                budget_bytes=MAX_CONTROL_DOCUMENT_BYTES,
            )
        )
        detail = detail.model_copy(update={field: None, "projection_issues": issues})
        if _response_bytes(detail) + envelope_bytes <= MAX_CONTROL_DOCUMENT_BYTES:
            return detail
    # Exact operation membership and known state survive an unavailable stored
    # projection; decorations confer no actions when their evidence is missing.
    return OperationDetailResponse(
        id=detail.id,
        parent_id=detail.parent_id,
        node_ids=detail.node_ids,
        kind=detail.kind,
        state=detail.state,
        attempt=detail.attempt,
        created_at=detail.created_at,
        observation_unavailable=True,
    )


def bounded_operations_response(
    page: OperationListPage,
    details: Sequence[OperationDetailResponse],
    *,
    cursors: CursorCodec,
    state: str | None,
    node_id: str | None,
    request_id: str | None,
) -> OperationsResponse:
    """Largest contiguous byte-sized page, with true total and signed boundary."""
    context = {"state": state, "node_id": node_id, "request_id": request_id}
    continuation_unavailable = page.continuation_unavailable or any(
        _operation_boundary(operation_item(item)) is None for item in page.items
    )

    def continuation(index: int) -> str | None:
        if index == len(details) - 1:
            return page.next_cursor
        boundary = _operation_boundary(operation_item(page.items[index]))
        if boundary is None:
            return None
        created_at, operation_id = boundary
        return cursors.encode(
            resource="operations",
            order="created-at-desc/id-desc/v1",
            context=context,
            boundary=[created_at.isoformat(), operation_id],
        )

    projected: list[OperationDetailResponse] = []
    prefix_bytes = 0
    best_count = 0
    best_cursor = None
    cursorless_envelope = _response_bytes(
        OperationsResponse(
            operations=[],
            total=page.total,
            next_cursor=None,
            projection_issue=page.projection_issue,
            continuation_unavailable=continuation_unavailable,
        )
    )
    for index, detail in enumerate(details):
        cursor = continuation(index)
        single = OperationsResponse(
            operations=[detail],
            total=page.total,
            next_cursor=cursor,
            projection_issue=page.projection_issue,
            continuation_unavailable=continuation_unavailable,
        )
        envelope = _response_bytes(single) - _response_bytes(detail)
        projected_detail = bounded_operation_detail(detail, envelope_bytes=envelope)
        projected.append(projected_detail)
        prefix_bytes += _response_bytes(projected_detail)
        comma_bytes = len(projected) - 1
        exact_envelope = _response_bytes(
            OperationsResponse(
                operations=[],
                total=page.total,
                next_cursor=cursor,
                projection_issue=page.projection_issue,
                continuation_unavailable=continuation_unavailable,
            )
        )
        if exact_envelope + prefix_bytes + comma_bytes <= MAX_CONTROL_DOCUMENT_BYTES:
            best_count = len(projected)
            best_cursor = cursor
        if (
            cursorless_envelope + prefix_bytes + comma_bytes
            > MAX_CONTROL_DOCUMENT_BYTES
        ):
            # Even removing the cursor cannot make this or a longer prefix fit.
            break
    if not details:
        return OperationsResponse(
            operations=[],
            total=page.total,
            next_cursor=page.next_cursor,
            projection_issue=page.projection_issue,
            continuation_unavailable=continuation_unavailable,
        )
    if best_count == 0:
        return OperationsResponse(
            operations=[],
            total=page.total,
            continuation_unavailable=True,
            projection_issue="Operation observations exceed the current reader allocation.",
        )
    return OperationsResponse(
        operations=projected[:best_count],
        total=page.total,
        next_cursor=best_cursor,
        projection_issue=page.projection_issue,
        continuation_unavailable=continuation_unavailable,
    )
