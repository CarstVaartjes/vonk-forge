"""Agent api: authority concerns."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from vonk_agent_protocol import (
    AgentDirective,
    AgentProgress,
    AgentResult,
    ContainerRuntimeAction,
    SecurityRefusalReason,
)
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
    CertificateResponseCapacityRefused,
    EnrollmentDenied,
    ExpiredRenewalGraceExhausted,
    RenewalConflictRevocationUncertain,
    RenewalInProgress,
    RenewalIssuanceUncertain,
)
from ..enrollment_body import _bounded_enrollment_body
from ..host_helper_authority import (
    HostHelperAuthorityError,
    HostRuntimeAuthorityService,
)
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
        try:
            grant = required.issue_grant(
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
                certificate_serial=identity.certificate_serial,
                expires_in_seconds=body.expires_in_seconds,
            )
            return _json_response(_host_grant_response(grant))
        except (KeyError, TypeError, ValueError, HostHelperAuthorityError):
            raise HTTPException(
                status_code=409, detail="host runtime authority rejected request"
            ) from None

    @agent.post(
        "/agent-upgrade/activation-grant", response_model=HostHelperGrantResponse
    )
    def package_activation_grant(
        body: PackageActivationGrantRequest, request: Request
    ) -> Response:
        identity = helper_identity(request)
        try:
            grant = host_runtime_service().issue_package_activation_grant(
                node_id=identity.node_id,
                receipt=body.receipt,
                runtime_identity=body.runtime_identity,
                certificate_serial=identity.certificate_serial,
            )
            return _json_response(_host_grant_response(grant))
        except (KeyError, TypeError, ValueError, HostHelperAuthorityError):
            raise HTTPException(
                status_code=409, detail="package activation authority rejected request"
            ) from None

    @agent.post("/agent-upgrade/grant", response_model=HostHelperGrantResponse)
    def agent_upgrade_grant(
        body: AgentUpgradeGrantRequest, request: Request
    ) -> Response:
        identity = helper_identity(request)
        required = host_runtime_service()
        try:
            grant = required.issue_agent_upgrade_grant(
                node_id=identity.node_id,
                fence=body.fence,
                package_sha256=body.package_sha256,
                package_signature=body.package_signature,
                certificate_serial=identity.certificate_serial,
                expires_in_seconds=body.expires_in_seconds,
            )
            return _json_response(_host_grant_response(grant))
        except (KeyError, TypeError, ValueError, HostHelperAuthorityError):
            raise HTTPException(
                status_code=409, detail="agent upgrade authority rejected request"
            ) from None

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

    @agent.post("/renew/expired", response_model=IssuedCertificateResponse)
    @raw_json_body(ExpiredRenewRequest)
    async def renew_expired(request: Request) -> Response:
        required = _require_services(services)
        if not limiter.admit():
            raise HTTPException(
                status_code=429, detail="enrollment rate limit exceeded"
            )
        raw = await _bounded_enrollment_body(request, required)
        try:
            body = ExpiredRenewRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(
                status_code=422, detail="expired renewal proof is malformed"
            ) from None
        try:
            issued = await run_in_threadpool(
                _require_enrollment(required).renew_expired, body
            )
        except CertificateResponseCapacityRefused as error:
            return _json_response(
                {"detail": {"reason_code": error.reason_code, "message": str(error)}},
                status_code=422,
            )
        except (
            RenewalInProgress,
            RenewalIssuanceUncertain,
            RenewalConflictRevocationUncertain,
        ) as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
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
        return _json_response(_issued_response(issued))

    @agent.post("/renew", response_model=IssuedCertificateResponse)
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
        except CertificateResponseCapacityRefused as error:
            return _json_response(
                {"detail": {"reason_code": error.reason_code, "message": str(error)}},
                status_code=422,
            )
        except (RenewalInProgress, RenewalIssuanceUncertain) as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        return _json_response(_issued_response(issued))

    @agent.post("/renew/recover", response_model=IssuedCertificateResponse)
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
        except CertificateResponseCapacityRefused as error:
            return _json_response(
                {"detail": {"reason_code": error.reason_code, "message": str(error)}},
                status_code=422,
            )
        except (RenewalInProgress, RenewalIssuanceUncertain) as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        return _json_response(_issued_response(issued))

    @agent.post("/renew/activate", status_code=status.HTTP_204_NO_CONTENT)
    def activate(body: ActivateRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_activation_identity(request, required)
        _body_node_matches(body.node_id, identity)
        try:
            _require_enrollment(required).activate(
                identity.node_id,
                identity.certificate_serial,
                body.generation,
            )
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)
