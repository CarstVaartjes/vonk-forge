"""Api: common concerns."""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from fastapi import Request
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, StreamingResponse
from vonk_agent_protocol import (
    CatalogCode,
    ControllerErrorCode,
    SecurityRefusalReason,
    canonical_message,
)
from vonk_agent_protocol.http_failure import HttpFailureResponse
from vonk_agent_protocol.telemetry import MAX_TELEMETRY_REPORT_BYTES

from ..catalog_api import CatalogProblem
from ..fleet_projection import FleetSnapshot
from ..metrics import MetricsRegistry
from ..operation_api import (
    BoundedErrorResponse,
    ErrorContextResponse,
    RequestValidationIssue,
    RequestValidationProblem,
)

"""Versioned authenticated control API."""


_LOGGER = logging.getLogger("vonk_control.api")


_RECIPE_IMAGE_UPLOAD = re.compile(
    r"/agent/recipe-builds/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/image\Z"
)


_LOGIN_PATH = "/api/auth/login"


_TELEMETRY_PATH = "/agent/telemetry"


_MAX_TELEMETRY_BODY_BYTES = MAX_TELEMETRY_REPORT_BYTES


_ARTIFACT_INPUT_UPLOAD = re.compile(
    r"/api/artifact-jobs/[0-9a-f-]{36}/inputs/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z"
)


_ARTIFACT_OUTPUT_UPLOAD = re.compile(
    r"/agent/recipe-jobs/[0-9a-f-]{36}/outputs/[0-9a-f]{64}\Z"
)


_CATALOG_HTTP_ERROR_CODES = {
    400: CatalogCode.INVALID_REQUEST,
    401: SecurityRefusalReason.CATALOG_AUTHENTICATION_REQUIRED.value,
    403: CatalogCode.INSUFFICIENT_ROLE,
    404: CatalogCode.NOT_FOUND,
    409: CatalogCode.CONFLICT,
    422: CatalogCode.INVALID_REQUEST,
    503: CatalogCode.UNAVAILABLE,
}


def _bounded_error_content(
    detail: object,
    *,
    validation: bool = False,
    context: ErrorContextResponse | None = None,
    candidates: list[str] | None = None,
) -> bytes:
    """Serialize the documented non-agent HTTP error contract.

    The detail is redacted as well as truncated: several routes build it from a
    caught exception, and a ``pydantic`` ``ValidationError`` stringifies the
    submitted input alongside the type error.
    """

    from ..logging import redact_text

    if not isinstance(detail, str):
        detail = "request failed"
    detail = redact_text(detail)[:256]
    response = (
        RequestValidationProblem(
            detail=detail, issues=[], context=context, candidates=candidates
        )
        if validation
        else BoundedErrorResponse(detail=detail, context=context)
    )
    return canonical_message(response.model_dump(mode="json", exclude_none=True))


def _validation_detail(issues: list[RequestValidationIssue]) -> str:
    """Name the first rejected fields, so the bare status line is diagnosable.

    ``issues`` keeps every structural error; the detail carries as many as fit
    the response bound, as ``field.path: reason``.
    """

    prefix = "request is invalid"
    shown: list[str] = []
    for issue in issues:
        field = ".".join(str(part) for part in issue.loc) or "body"
        shown.append(f"{field}: {issue.msg}")
    detail = prefix
    for count, entry in enumerate(shown):
        candidate = f"{detail}{':' if count == 0 else ';'} {entry}"
        remaining = len(shown) - count - 1
        suffix = f" (+{remaining} more)" if remaining else ""
        if len(candidate) + len(suffix) > 256:
            if count == 0:
                return candidate[:256]
            return f"{detail} (+{len(shown) - count} more)"[:256]
        detail = candidate
    return detail


def _invalid_login_content() -> bytes:
    from ..auth_api import LoginRequestInvalid

    return canonical_message(
        LoginRequestInvalid(detail="login request is invalid").model_dump(mode="json")
    )


def _catalog_error_content(request: Request, error: StarletteHTTPException) -> bytes:
    """Serialize catalog HTTP errors through the route's public model."""

    detail = error.detail if isinstance(error.detail, str) else "catalog request failed"
    response = CatalogProblem(
        code=_CATALOG_HTTP_ERROR_CODES.get(
            error.status_code, CatalogCode.REQUEST_FAILED
        ),
        detail=detail[:256],
        request_id=request.state.request_id,
    )
    return canonical_message(response.model_dump(mode="json"))


def _http_error_code(status_code: int) -> str:
    """Return a stable, secret-free code for an HTTP boundary failure."""

    if status_code == 401:
        return SecurityRefusalReason.CONTROLLER_AUTHENTICATION_REQUIRED.value
    if status_code == 403:
        # Middleware cannot reliably recover route detail from a wrapped
        # response. Keep the classification generic unless a trusted producer
        # supplies a canonical code directly.
        return SecurityRefusalReason.CONTROLLER_REQUEST_REJECTED.value
    return {
        400: ControllerErrorCode.INVALID_REQUEST,
        404: ControllerErrorCode.NOT_FOUND,
        409: ControllerErrorCode.CONFLICT,
        413: ControllerErrorCode.REQUEST_TOO_LARGE,
        422: ControllerErrorCode.INVALID_REQUEST,
        503: ControllerErrorCode.UNAVAILABLE,
    }.get(status_code, f"{ControllerErrorCode.HTTP}{status_code}")


class _FleetEventStreamResponse(StreamingResponse):
    """Keep the response's actual media type in FastAPI's generated schema."""

    media_type = "text/event-stream"


class _DuplicateJsonKey(ValueError):
    pass


class _RequestBodyTooLarge(ValueError):
    pass


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise _DuplicateJsonKey
        document[key] = value
    return document


def _log_request_failure(
    request: Request, status: int, cause: BaseException | None
) -> None:
    """Log a 5xx with the request id the caller sees and a redacted traceback."""
    from ..logging import log_event

    log_event(
        _LOGGER,
        "api.request_failed",
        service="controller",
        operation=f"{request.method} {request.url.path}",
        endpoint=request.url.path,
        request_id=getattr(request.state, "request_id", None),
        http_status=status,
        failure_type=type(cause).__name__,
        traceback=traceback.format_exception(cause)[-64:] if cause else [],
    )


def _log_agent_rejection(request: Request, status: int, reason: str) -> None:
    """One line for a 4xx on an agent endpoint: request id and reason, no body."""
    from ..logging import log_event

    log_event(
        _LOGGER,
        "agent.request_rejected",
        service="controller",
        endpoint=request.url.path,
        request_id=getattr(request.state, "request_id", None),
        http_status=status,
        reason=reason[:200],
    )


async def _bounded_request_body(request: Request, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise _RequestBodyTooLarge
        body.extend(chunk)
    bounded = bytes(body)
    request._body = bounded
    return bounded


class SpaFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if (
                error.status_code == 404
                and "." not in path
                and self.directory is not None
            ):
                return FileResponse(Path(self.directory) / "index.html")
            raise


class JobQueue(Protocol):
    def enqueue(
        self,
        kind: str,
        actor: str,
        authority_revision: str,
        targets: Sequence[str],
        payload: Mapping[str, object],
        *,
        request_id: str,
    ) -> Any: ...
    def get(self, job_id: str) -> Any: ...


def refresh_fleet_metrics(
    metrics: MetricsRegistry,
    fleet_snapshot: FleetSnapshot,
) -> None:
    """Refresh metrics from the single typed FleetProjection evidence path."""

    metrics.update_fleet(fleet_snapshot)


def http_failure_response(
    status_code: int, retry_after: str | None = None, declared: str | None = None
) -> HttpFailureResponse:
    """Publish one shared decision for every non-success, including middleware.

    Request validation is still rejected before effects by the endpoint's
    diagnostic contract. It is never promoted into a security refusal.
    """
    from vonk_agent_protocol.http_failure import (
        HttpFailureFamily,
        HttpFailureResponse,
        HttpTransient,
        TransientReason,
    )

    if declared is not None:
        try:
            return HttpFailureResponse.model_validate_json(declared)
        except ValueError:
            pass
    delay = 1
    if retry_after is not None and retry_after.isdecimal():
        delay = min(int(retry_after), 3600)
    reason = (
        TransientReason.RATE_LIMITED
        if status_code == 429
        else TransientReason.ADMISSION_BUSY
        if status_code == 409
        else TransientReason.DEPENDENCY_UNAVAILABLE
    )
    return HttpFailureResponse(
        failure=HttpTransient(
            family=HttpFailureFamily.TRANSIENT,
            reason=reason,
            retry_after=delay,
            resolution_window=max(300, delay),
        )
    )
