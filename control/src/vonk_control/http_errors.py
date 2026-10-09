"""Explicit security-edge HTTP producers; storage faults never use this type."""

from fastapi import HTTPException
from starlette.responses import Response
from vonk_agent_protocol.http_failure import (
    HttpFailureFamily,
    HttpFailureResponse,
    HttpRefusal,
    HttpRefusalReason,
)


def refusal_header(reason: HttpRefusalReason) -> str:
    return HttpFailureResponse(
        failure=HttpRefusal(family=HttpFailureFamily.REFUSAL, reason=reason)
    ).model_dump_json()


class SecurityHTTPError(HTTPException):
    def __init__(
        self,
        *,
        reason: HttpRefusalReason,
        status_code: int,
        detail: object,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            detail=detail,
            headers={**(headers or {}), "x-vonk-outcome": refusal_header(reason)},
        )


def authentication_response() -> Response:
    return Response(
        status_code=401,
        headers={
            "x-vonk-outcome": refusal_header(HttpRefusalReason.AUTHENTICATION_REQUIRED)
        },
    )
