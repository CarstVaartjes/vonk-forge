"""Agent api: authority concerns."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from vonk_agent_protocol import (
    AgentDirective,
    AgentProgress,
    AgentResult,
    ContainerRuntimeAction,
    SecurityRefusalReason,
    SignedHostHelperGrant,
    UnknownError,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_agent_protocol.enrollment import (
    ActivateRequest,
    ExpiredRenewRequest,
    IssuedCertificateResponse,
    RenewRequest,
)
from vonk_agent_protocol.reason_codes import ControllerErrorCode as Code

from ..agent_jobs import CLAIM_LEASE_SECONDS, AgentResultRefused, StaleAgentAttempt
from ..auth import AgentIdentity, agent_identity_from_scope
from ..contract_graph import raw_json_body
from ..enrollment import (
    EnrollmentDenied,
    EnrollmentIssuanceUncertain,
    ExpiredRenewalGraceExhausted,
    RenewalIssuanceUncertain,
)
from ..enrollment.responses import unknown_response
from ..enrollment_body import _bounded_enrollment_body
from ..enrollment_contract import EnrollmentObservationReply
from ..evidence_repeats import note_repeat, note_success
from ..host_helper_authority import (
    HostHelperAuthorityError,
    HostRuntimeAuthorityService,
    HostRuntimeEvidenceUnavailable,
    HostRuntimeGrantUnsatisfiable,
)
from ..logging import redact_text
from ..models import AgentOperation, AgentOperationAttempt, Job
from ..operator_acceptance import ReceiptPolicy, operator_acceptance
from ..strict_json import ControllerAPIRoute
from .common import (
    AgentApiServices,
    AgentUpgradeGrantRequest,
    EnrollmentRateLimiter,
    HostHelperGrantResponse,
    HostRuntimeGrantRequest,
    PackageActivationGrantRequest,
    _authenticated_activation_identity,
    _authenticated_identity,
    _body_node_matches,
    _host_grant_response,
    _issued_response,
    _json_response,
    _log_evidence_warnings,
    _require_enrollment,
    _require_services,
    _scope_identity,
    _validated_authenticated_source,
    active_agent_identity,
)


def issue_host_runtime_grant(
    service: HostRuntimeAuthorityService,
    body: HostRuntimeGrantRequest,
    *,
    node_id: str,
    certificate_serial: str,
) -> SignedHostHelperGrant:
    """Grant the agent's request exactly as sent, for its mTLS identity."""
    return service.issue_grant(
        node_id=node_id,
        fence=body.fence,
        action=ContainerRuntimeAction(body.action),
        request_sha256=body.request_sha256,
        start_plan_sha256=body.start_plan_sha256,
        stop_plan_sha256=body.stop_plan_sha256,
        run_generation=body.run_generation,
        runtime_run_id=body.runtime_run_id,
        runtime_target_id=body.runtime_target_id,
        runtime_installation_id=body.runtime_installation_id,
        installation_id=body.installation_id,
        reconciliation_identity=body.reconciliation_identity,
        installation_intent_nonce=body.installation_intent_nonce,
        certificate_serial=certificate_serial,
        expires_in_seconds=body.expires_in_seconds,
    )


#: The subject a Spark's refused results are counted under: one per Spark,
#: cleared by its next accepted result.
RESULT_REFUSALS = "agent result"


def _refusal_detail(error: RequestValidationError) -> str:
    first = next(iter(error.errors()), {})
    where = ".".join(str(part) for part in first.get("loc", ()))
    return redact_text(f"{where}: {first.get('msg', 'invalid')}")[:256]


def install_authority_routes(
    agent: APIRouter, services: AgentApiServices | None, limiter: EnrollmentRateLimiter
) -> None:
    def note_result(request: Request, refused: str | None) -> None:
        # A result refused on every delivery is shown as repeated evidence,
        # never acted on; the Spark's next accepted result clears it.
        if services is None:
            return
        identity = agent_identity_from_scope(dict(request.scope))
        if identity is None or not active_agent_identity(services, identity):
            return
        with suppress(SQLAlchemyError), services.sessions.begin() as session:
            if refused is None:
                note_success(session, RESULT_REFUSALS, identity.node_id)
            else:
                note_repeat(
                    session,
                    RESULT_REFUSALS,
                    identity.node_id,
                    Code.INVALID_REQUEST,
                    now=services.clock(),
                    detail=refused,
                )

    class NotedResultRoute(ControllerAPIRoute):
        """A result body refused before the route runs is still counted."""

        def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
            handler = super().get_route_handler()

            async def noted(request: Request) -> Response:
                try:
                    return await handler(request)
                except RequestValidationError as error:
                    await run_in_threadpool(
                        note_result, request, _refusal_detail(error)
                    )
                    raise

            return noted

    def note_grant(node_id: str, subject: str, *, denied: bool) -> None:
        # A grant denied on every ask is shown as repeated evidence; the
        # answer itself is unchanged, and a granted ask clears it.
        required = _require_services(services)
        with suppress(SQLAlchemyError), required.sessions.begin() as session:
            if denied:
                note_repeat(
                    session,
                    subject,
                    node_id,
                    SecurityRefusalReason.HOST_HELPER_AUTHORITY_DENIED,
                    now=required.clock(),
                )
            else:
                note_success(session, subject, node_id)

    def observe_grant(
        issue: Callable[[], SignedHostHelperGrant],
        evidence_available: Callable[[], bool] = lambda: True,
        *,
        node_id: str,
        subject: str,
    ) -> SignedHostHelperGrant:
        # A new read of the exact authority precedes each attempt. No grant is
        # issued on unreadable evidence, and this request owns no durable slot.
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                grant = issue()
            except HostRuntimeGrantUnsatisfiable as refused:
                # Deterministic: this attempt's plan can never be granted. A
                # definite answer the agent ends the work with, not a wait.
                raise HTTPException(
                    status_code=409,
                    detail="grant authority can never bind this attempt",
                    headers={"x-vonk-error-code": refused.reason.value},
                ) from None
            except HostRuntimeEvidenceUnavailable:
                # Evidence not readable yet: observe again, then retry later.
                continue
            except HostHelperAuthorityError:
                try:
                    if not evidence_available():
                        continue
                except (KeyError, TypeError, ValueError, SQLAlchemyError):
                    continue
                # The authority's denied binding remains a security refusal.
                note_grant(node_id, subject, denied=True)
                raise HTTPException(
                    status_code=403, detail="grant authority denied"
                ) from None
            except (KeyError, TypeError, ValueError, SQLAlchemyError):
                continue
            note_grant(node_id, subject, denied=False)
            return grant
        raise UnknownOutcomeError(
            "grant authority observation is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )

    def attempt_evidence(fence: str) -> bool:
        # Inspect only availability after the authority's answer. This is no
        # independent authorization test and cannot mint or broaden a grant.
        required = _require_services(services)
        with required.sessions() as session:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.fence == fence
                )
            )
            if attempt is None:
                return False
            operation = session.get(AgentOperation, attempt.operation_id)
            return (
                operation is not None
                and session.get(Job, operation.parent_job_id) is not None
            )

    def activation_evidence(node_id: str) -> bool:
        required = _require_services(services)
        with required.sessions() as session:
            return (
                session.scalar(
                    select(AgentOperation.id)
                    .where(
                        AgentOperation.node_id == node_id,
                        AgentOperation.kind == OperationKind.AGENT_UPGRADE,
                    )
                    .limit(1)
                )
                is not None
            )

    def helper_identity(request: Request) -> AgentIdentity:
        _scope_identity(request)
        required = _require_services(services)
        return _authenticated_identity(request, required)

    def host_runtime_service() -> HostRuntimeAuthorityService:
        required = services.host_runtime_authority if services is not None else None
        if required is None:
            raise HTTPException(
                status_code=503, detail="host runtime authority unavailable"
            )
        return required

    @agent.post("/host-runtime/grant", response_model=HostHelperGrantResponse)
    @operator_acceptance(ReceiptPolicy.AGENT_GRANT)
    def host_runtime_grant(body: HostRuntimeGrantRequest, request: Request) -> Response:
        identity = helper_identity(request)
        required = host_runtime_service()
        grant = observe_grant(
            lambda: issue_host_runtime_grant(
                required,
                body,
                node_id=identity.node_id,
                certificate_serial=identity.certificate_serial,
            ),
            evidence_available=lambda: attempt_evidence(body.fence),
            node_id=identity.node_id,
            subject=f"host runtime {body.action} grant",
        )
        return _json_response(_host_grant_response(grant))

    @agent.post(
        "/agent-upgrade/activation-grant", response_model=HostHelperGrantResponse
    )
    @operator_acceptance(ReceiptPolicy.AGENT_GRANT)
    def package_activation_grant(
        body: PackageActivationGrantRequest, request: Request
    ) -> Response:
        identity = helper_identity(request)
        grant = observe_grant(
            lambda: host_runtime_service().issue_package_activation_grant(
                node_id=identity.node_id,
                receipt=body.receipt,
                runtime_identity=body.runtime_identity,
                certificate_serial=identity.certificate_serial,
            ),
            evidence_available=lambda: activation_evidence(identity.node_id),
            node_id=identity.node_id,
            subject="package activation grant",
        )
        return _json_response(_host_grant_response(grant))

    @agent.post("/agent-upgrade/grant", response_model=HostHelperGrantResponse)
    @operator_acceptance(ReceiptPolicy.AGENT_GRANT)
    def agent_upgrade_grant(
        body: AgentUpgradeGrantRequest, request: Request
    ) -> Response:
        identity = helper_identity(request)
        required = host_runtime_service()
        grant = observe_grant(
            lambda: required.issue_agent_upgrade_grant(
                node_id=identity.node_id,
                fence=body.fence,
                package_sha256=body.package_sha256,
                package_signature=body.package_signature,
                certificate_serial=identity.certificate_serial,
                expires_in_seconds=body.expires_in_seconds,
            ),
            evidence_available=lambda: attempt_evidence(body.fence),
            node_id=identity.node_id,
            subject="agent upgrade grant",
        )
        return _json_response(_host_grant_response(grant))

    @agent.post("/heartbeat", response_model=AgentDirective)
    @operator_acceptance(ReceiptPolicy.AGENT_HEARTBEAT)
    def heartbeat(body: AgentProgress, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        message = body
        _log_evidence_warnings(body, endpoint="heartbeat", node_id=identity.node_id)
        source = _validated_authenticated_source(request, required, identity)
        try:
            response = required.operations.heartbeat(
                message,
                message.progress,
                CLAIM_LEASE_SECONDS,
                source=source,
            )
        except StaleAgentAttempt as error:
            if required.operations.known_superseded_cancellation(
                message, source=source
            ):
                logging.getLogger("vonk_control.agent_api").info(
                    "ignored heartbeat for superseded cancelled fence %s",
                    message.fence,
                )
                raise HTTPException(
                    status_code=409,
                    detail="superseded operation was cancelled",
                    headers={
                        "x-vonk-error-code": Code.SUPERSEDED_OPERATION_CANCELLED.value
                    },
                ) from None
            required.operations.record_boundary_refusal(
                str(message.fence),
                boundary="heartbeat",
                check="stale-attempt",
            )
            raise HTTPException(status_code=409, detail=str(error)) from None
        except ValueError as error:
            required.operations.record_boundary_refusal(
                str(message.fence),
                boundary="heartbeat",
                check="invalid-progress",
            )
            raise HTTPException(status_code=409, detail=str(error)) from None
        return _json_response(AgentDirective.model_validate(response))

    @operator_acceptance(ReceiptPolicy.AGENT_RESULT)
    def result(body: AgentResult, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        message = body
        _log_evidence_warnings(body, endpoint="result", node_id=identity.node_id)
        source = _validated_authenticated_source(request, required, identity)
        try:
            # The failed-result identity rule (a failed status plus a stable
            # error code) is part of the operation result contract and is
            # applied by ``validate_result_for_operation`` inside
            # ``record_result``.  Keeping no second copy here is what makes the
            # producer and the ingress enforce exactly one rule.
            required.operations.record_result(message, source=source)
        except StaleAgentAttempt as error:
            try:
                required.operations.record_late_result(message, source=source)
            except StaleAgentAttempt:
                required.operations.record_boundary_refusal(
                    str(message.fence),
                    boundary="result",
                    check="stale-attempt",
                )
                raise HTTPException(status_code=409, detail=str(error)) from None
            except ValueError as invalid:
                required.operations.record_boundary_refusal(
                    str(message.fence),
                    boundary="result",
                    check="late-result-invalid",
                )
                raise HTTPException(status_code=422, detail=str(invalid)) from None
            return Response(status_code=status.HTTP_202_ACCEPTED)
        except ValueError as error:
            required.operations.record_boundary_refusal(
                str(message.fence),
                boundary="result",
                check="invalid-result",
            )
            if isinstance(error, AgentResultRefused):
                # The contract refuses these bytes on every delivery: settle the
                # authenticated attempt failed (invalid contract) with the
                # validation detail, instead of letting its lease lapse into a
                # reissue of the same work.
                with suppress(StaleAgentAttempt, UnknownOutcomeError):
                    required.operations.fail(
                        str(message.fence),
                        f"agent result refused at input validation: {error}",
                        source=source,
                    )
            raise HTTPException(status_code=422, detail=str(error)) from None
        note_result(request, None)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    agent.add_api_route(
        "/result",
        result,
        methods=["POST"],
        status_code=status.HTTP_204_NO_CONTENT,
        route_class_override=NotedResultRoute,
    )

    @agent.post(
        "/renew/expired",
        response_model=IssuedCertificateResponse,
        responses={503: {"model": EnrollmentObservationReply}},
    )
    @raw_json_body(ExpiredRenewRequest)
    @operator_acceptance(ReceiptPolicy.ENROLLMENT_ISSUE)
    async def renew_expired(request: Request) -> Response:
        required = _require_services(services)
        if not limiter.admit():
            raise HTTPException(
                status_code=429,
                detail="expired renewal rate limit exceeded",
                headers={"retry-after": str(limiter.retry_after_seconds())},
            )
        raw = await _bounded_enrollment_body(request, required)
        try:
            body = ExpiredRenewRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(
                status_code=422, detail="expired renewal proof is malformed"
            ) from None
        try:
            issued = await asyncio.to_thread(
                _require_enrollment(required).renew_expired, body
            )
        except (EnrollmentIssuanceUncertain, RenewalIssuanceUncertain) as error:
            return unknown_response(error)
        except (EnrollmentDenied, ValueError) as error:
            code = (
                error.reason_code
                if isinstance(error, ExpiredRenewalGraceExhausted)
                else SecurityRefusalReason.AGENT_EXPIRED_RENEWAL_REFUSED
            )
            response = _json_response(
                {"detail": {"reason_code": code, "message": str(error)}},
                status_code=403,
            )
            response.headers["X-Vonk-Error-Code"] = code
            return response
        if isinstance(issued, UnknownError):
            return unknown_response(issued)
        return _json_response(_issued_response(issued))

    @agent.post(
        "/renew",
        response_model=IssuedCertificateResponse,
        responses={503: {"model": EnrollmentObservationReply}},
    )
    @operator_acceptance(ReceiptPolicy.ENROLLMENT_ISSUE)
    def renew(body: RenewRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        _body_node_matches(body.node_id, identity)
        try:
            issued = _require_enrollment(required).renew(
                identity.node_id, identity.certificate_serial, body.csr.encode("ascii")
            )
        except UnicodeEncodeError:
            raise HTTPException(
                status_code=422, detail="CSR must be ASCII PEM"
            ) from None
        except (EnrollmentIssuanceUncertain, RenewalIssuanceUncertain) as error:
            return unknown_response(error)
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        if isinstance(issued, UnknownError):
            return unknown_response(issued)
        return _json_response(_issued_response(issued))

    @agent.post(
        "/renew/recover",
        response_model=IssuedCertificateResponse,
        responses={503: {"model": EnrollmentObservationReply}},
    )
    @operator_acceptance(ReceiptPolicy.ENROLLMENT_ISSUE)
    def recover_renewal(body: RenewRequest, request: Request) -> Response:
        """Recover a staged certificate that was created for another CSR.

        The active source identity is deliberately used for admission.  The
        Controller retires and CA-revokes the conflicting staged identity
        before issuing the durable pending CSR.
        """
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        _body_node_matches(body.node_id, identity)
        try:
            issued = _require_enrollment(required).recover_rotation(
                identity.node_id, identity.certificate_serial, body.csr.encode("ascii")
            )
        except UnicodeEncodeError:
            raise HTTPException(
                status_code=422, detail="CSR must be ASCII PEM"
            ) from None
        except (EnrollmentIssuanceUncertain, RenewalIssuanceUncertain) as error:
            return unknown_response(error)
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        if isinstance(issued, UnknownError):
            return unknown_response(issued)
        return _json_response(_issued_response(issued))

    @agent.post(
        "/renew/activate",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={503: {"model": EnrollmentObservationReply}},
    )
    @operator_acceptance(ReceiptPolicy.ENROLLMENT_ACTIVATE)
    def activate(body: ActivateRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_activation_identity(request, required)
        _body_node_matches(body.node_id, identity)
        try:
            outcome = _require_enrollment(required).activate(
                identity.node_id,
                identity.certificate_serial,
                body.generation,
            )
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        if isinstance(outcome, UnknownError):
            return unknown_response(outcome)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
