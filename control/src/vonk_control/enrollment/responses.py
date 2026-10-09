"""Typed, retryable observation responses for enrollment transport consumers."""

import json
from datetime import UTC, datetime

from fastapi.responses import JSONResponse
from starlette.responses import Response
from vonk_agent_protocol import LifecycleState, UnknownError, canonical_message
from vonk_agent_protocol.enrollment import IssuedCertificateResponse

from ..enrollment_contract import (
    EnrollmentObservationOutcome,
    EnrollmentObservationReply,
)
from ..pki import IssuedCertificate
from .types import EnrollmentIssuanceUncertain, RenewalIssuanceUncertain


def unknown_response(
    observation: UnknownError | EnrollmentIssuanceUncertain | RenewalIssuanceUncertain,
) -> JSONResponse:
    if not isinstance(observation, UnknownError):
        typed = observation.typed_error()
        assert typed is not None
        observation = typed
    return JSONResponse(
        EnrollmentObservationReply(
            detail=observation,
            state=observation.state
            if isinstance(observation, EnrollmentObservationOutcome)
            else LifecycleState.OBSERVING,
        ).model_dump(mode="json"),
        status_code=503,
        headers={"retry-after": "5", "Cache-Control": "no-store"},
    )


def _wire(value: object) -> object:
    return json.loads(canonical_message(value))


def _now(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _issued_response(issued: IssuedCertificate) -> IssuedCertificateResponse:
    return IssuedCertificateResponse(
        node_id=issued.node_id,
        certificate_pem=issued.certificate_pem.decode("ascii"),
        chain_pem=issued.chain_pem.decode("ascii"),
        serial=issued.serial,
        fingerprint=issued.fingerprint,
        not_before=_now(issued.not_before).isoformat(),
        not_after=_now(issued.not_after).isoformat(),
        generation=issued.generation,
    )


def _json_response(value: object, *, status_code: int = 200) -> Response:
    return Response(
        content=canonical_message(value),
        status_code=status_code,
        media_type="application/json",
    )
