"""Agent api: enrollment concerns."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError
from vonk_agent_protocol import UnknownError
from vonk_agent_protocol.enrollment import (
    EnrollmentBootstrapResponse,
    EnrollmentSubmitRequest,
    IssuedCertificateResponse,
)

from ..contract_graph import raw_json_body
from ..enrollment import (
    EnrollmentDenied,
    EnrollmentIssuanceUncertain,
    RenewalIssuanceUncertain,
)
from ..enrollment.responses import unknown_response
from ..enrollment_body import (
    _bounded_enrollment_body,
    _scan_enrollment_grants,
)
from ..enrollment_contract import EnrollmentObservationReply
from ..operation_api import bounded_error_responses
from .common import (
    AgentApiServices,
    EnrollmentRateLimiter,
    _issued_response,
    _json_response,
    _require_enrollment,
    _require_services,
)


def install_enrollment_routes(
    agent: APIRouter, services: AgentApiServices | None, limiter: EnrollmentRateLimiter
) -> None:
    @agent.get(
        "/bootstrap",
        response_model=EnrollmentBootstrapResponse,
        responses=bounded_error_responses(503),
    )
    def enrollment_bootstrap() -> Response:
        required = _require_services(services)
        if required.bootstrap is None:
            raise HTTPException(
                status_code=503,
                detail="agent enrollment bootstrap is unavailable",
            )
        if required.host_runtime_authority is None:
            raise HTTPException(
                status_code=503,
                detail="host runtime authority is unavailable",
            )
        helper_public_key = required.host_runtime_authority.public_key_document.get(
            "public_key"
        )
        if (
            not isinstance(helper_public_key, str)
            or re.fullmatch(r"[0-9a-f]{64}", helper_public_key) is None
        ):
            raise HTTPException(
                status_code=503,
                detail="host runtime authority is unavailable",
            )
        return _json_response(
            EnrollmentBootstrapResponse(
                controller_endpoint=required.bootstrap.controller_endpoint,
                enrollment_endpoint=required.bootstrap.enrollment_endpoint,
                ca_fingerprint=required.bootstrap.ca_fingerprint,
                ca_pem=required.bootstrap.ca_pem,
                controller_address=required.bootstrap.controller_address,
                service_hostnames=list(required.bootstrap.service_hostnames),
                host_helper_authority_public_key=helper_public_key,
            )
        )

    @agent.post(
        "/enroll",
        response_model=IssuedCertificateResponse,
        responses={503: {"model": EnrollmentObservationReply}},
    )
    @raw_json_body(EnrollmentSubmitRequest)
    async def enroll(request: Request) -> Response:
        required = _require_services(services)
        if not limiter.admit():
            raise HTTPException(
                status_code=429,
                detail="enrollment rate limit exceeded",
                headers={"retry-after": str(limiter.retry_after())},
            )
        raw = await _bounded_enrollment_body(request, required)
        scan = _scan_enrollment_grants(raw)
        content_type = request.headers.get("content-type", "")
        if (
            re.fullmatch(
                r"application/json(?:\s*;\s*charset=(?:utf-8|utf8))?",
                content_type,
                re.IGNORECASE,
            )
            is None
        ):
            raise HTTPException(
                status_code=415,
                detail="enrollment content type must be application/json",
            )
        try:
            body = json.loads(raw.decode("utf-8"))
        except (TypeError, UnicodeDecodeError, ValueError, RecursionError):
            raise HTTPException(
                status_code=422, detail="enrollment request must be JSON"
            ) from None
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=422, detail="enrollment request must be a JSON object"
            )
        if scan.top_level_keys != 1:
            raise HTTPException(status_code=422, detail="enrollment grant is ambiguous")
        try:
            submitted = EnrollmentSubmitRequest.model_validate(body)
        except ValidationError:
            raise HTTPException(
                status_code=422, detail="enrollment request is invalid"
            ) from None
        csr_bytes = submitted.csr.encode("ascii")
        try:
            outcome = _require_enrollment(required).submit(
                submitted.grant_token, csr_bytes, submitted.evidence.model_dump()
            )
        except (EnrollmentIssuanceUncertain, RenewalIssuanceUncertain) as error:
            return unknown_response(error)
        except EnrollmentDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        if isinstance(outcome, UnknownError):
            return unknown_response(outcome)
        return _json_response(_issued_response(outcome))
