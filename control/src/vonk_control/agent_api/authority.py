"""Agent api: authority concerns."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, Response, status
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

from ..agent_jobs import CLAIM_LEASE_SECONDS, StaleAgentAttempt
from ..auth import AgentIdentity
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
from ..host_helper_authority import (
    HostHelperAuthorityError,
    HostRuntimeAuthorityService,
)
from ..models import AgentOperation, AgentOperationAttempt, Job
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
)


def install_authority_routes(
    agent: APIRouter, services: AgentApiServices | None, limiter: EnrollmentRateLimiter
) -> None:
    def observe_grant(
        issue: Callable[[], SignedHostHelperGrant],
        evidence_available: Callable[[], bool] = lambda: True,
    ) -> SignedHostHelperGrant:
        # A new read of the exact authority precedes each attempt. No grant is
        # issued on unreadable evidence, and this request owns no durable slot.
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return issue()
            except HostHelperAuthorityError:
                try:
                    if not evidence_available():
                        continue
                except (KeyError, TypeError, ValueError, SQLAlchemyError):
                    continue
                # The authority's denied binding remains a security refusal.
                raise HTTPException(
                    status_code=403, detail="grant authority denied"
                ) from None
            except (KeyError, TypeError, ValueError, SQLAlchemyError):
                continue
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
    def host_runtime_grant(body: HostRuntimeGrantRequest, request: Request) -> Response:
        identity = helper_identity(request)
        required = host_runtime_service()
        grant = observe_grant(
            lambda: required.issue_grant(
                node_id=identity.node_id,
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
                certificate_serial=identity.certificate_serial,
                expires_in_seconds=body.expires_in_seconds,
            ),
            evidence_available=lambda: attempt_evidence(body.fence),
        )
        return _json_response(_host_grant_response(grant))

    @agent.post(
        "/agent-upgrade/activation-grant", response_model=HostHelperGrantResponse
    )
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
        )
        return _json_response(_host_grant_response(grant))

    @agent.post("/agent-upgrade/grant", response_model=HostHelperGrantResponse)
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
        )
        return _json_response(_host_grant_response(grant))

    @agent.post("/heartbeat", response_model=AgentDirective)
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

    @agent.post("/result", status_code=status.HTTP_204_NO_CONTENT)
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
            raise HTTPException(status_code=422, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.post(
        "/renew/expired",
        response_model=IssuedCertificateResponse,
        responses={503: {"model": EnrollmentObservationReply}},
    )
    @raw_json_body(ExpiredRenewRequest)
    async def renew_expired(request: Request) -> Response:
        required = _require_services(services)
        if not limiter.admit():
            raise HTTPException(
                status_code=429,
                detail="enrollment rate limit exceeded",
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
