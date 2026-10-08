"""Typed, retryable observation responses for enrollment transport consumers."""

from functools import partial

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from vonk_agent_protocol import UnknownError
from vonk_agent_protocol.enrollment import IssuedCertificateResponse

from ..enrollment_contract import EnrollmentObservationReply


def unknown_response(observation: UnknownError) -> JSONResponse:
    return JSONResponse(
        EnrollmentObservationReply(detail=observation).model_dump(mode="json"),
        status_code=503,
    )


def certificate_post(router: APIRouter) -> partial:
    return partial(
        router.post,
        response_model=IssuedCertificateResponse,
        responses={503: {"model": EnrollmentObservationReply}},
    )
