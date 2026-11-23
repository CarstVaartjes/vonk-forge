"""Api: the canonical error handlers."""

from __future__ import annotations

from fastapi import (
    FastAPI,
    Request,
    Response,
)
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from vonk_agent_protocol.error_response import (
    RequestValidationIssue,
    RequestValidationProblem,
)
from vonk_agent_protocol.reason_codes.catalog import CatalogCode
from vonk_agent_protocol.wire_model import canonical_message

from ..capabilities import CapabilityUnavailableError
from ..catalog.api import CatalogProblem
from ..http_errors import TemporaryHTTPError
from ..library_projection.common import SelectorAmbiguityHTTPError
from ..logging import redact_text
from .common import (
    _LOGIN_PATH,
    _bounded_error_content,
    _catalog_error_content,
    _invalid_login_content,
    _log_agent_rejection,
    _log_request_failure,
    _validation_detail,
)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(CapabilityUnavailableError)
    async def capability_unavailable(
        _request: Request, error: CapabilityUnavailableError
    ) -> Response:
        return Response(
            error.reply.model_dump_json(),
            status_code=503,
            media_type="application/json",
        )

    @app.exception_handler(StarletteHTTPException)
    async def canonical_agent_http_error(
        request: Request, error: StarletteHTTPException
    ) -> Response:

        if isinstance(error, TemporaryHTTPError):
            return Response(
                error.answer.model_dump_json(),
                status_code=error.status_code,
                headers=error.headers,
                media_type="application/json",
            )

        cause = error.__context__
        if error.status_code >= 500 and cause is not None:
            # A handler turned this failure into a bare 5xx; keep the cause
            # (the raise ... from None only hides it from the traceback).
            _log_request_failure(request, error.status_code, cause)
        if (
            request.url.path.startswith("/agent/")
            and 400 <= error.status_code < 500
            and error.status_code not in {401, 403}
        ):
            _log_agent_rejection(request, error.status_code, str(error.detail))
        if request.url.path.startswith("/api/catalog/"):
            return Response(
                content=_catalog_error_content(request, error),
                status_code=error.status_code,
                headers=error.headers,
                media_type="application/json",
            )
        if not request.url.path.startswith("/agent/"):
            if request.url.path.startswith("/api/"):
                return Response(
                    content=_bounded_error_content(
                        error.detail,
                        validation=error.status_code == 422,
                        candidates=(
                            error.problem.candidates
                            if isinstance(error, SelectorAmbiguityHTTPError)
                            else None
                        ),
                    ),
                    status_code=error.status_code,
                    headers=error.headers,
                    media_type="application/json",
                )
            return await http_exception_handler(request, error)
        return Response(
            content=_bounded_error_content(
                error.detail, validation=error.status_code == 422
            ),
            status_code=error.status_code,
            headers=error.headers,
            media_type="application/json",
        )

    @app.exception_handler(RequestValidationError)
    async def canonical_agent_validation_error(
        request: Request, error: RequestValidationError
    ) -> Response:
        if request.url.path == _LOGIN_PATH:
            return Response(
                content=_invalid_login_content(),
                status_code=422,
                media_type="application/json",
            )
        if request.url.path.startswith("/api/catalog/"):
            response = CatalogProblem(
                code=CatalogCode.INVALID_REQUEST,
                detail="catalog request is invalid",
                request_id=request.state.request_id,
            )
            return Response(
                content=canonical_message(response.model_dump(mode="json")),
                status_code=422,
                media_type="application/json",
            )
        if not request.url.path.startswith(("/agent/", "/api/")):
            return await request_validation_exception_handler(request, error)

        if request.url.path.startswith("/agent/"):
            _log_agent_rejection(
                request,
                422,
                "invalid field "
                + ", ".join(
                    ".".join(str(part) for part in item["loc"])
                    for item in error.errors()[:8]
                ),
            )
        issues = [
            RequestValidationIssue(
                type=item["type"],
                loc=list(item["loc"]),
                msg=redact_text(item["msg"]),
            )
            for item in error.errors()
        ]
        response = RequestValidationProblem(
            detail=_validation_detail(issues), issues=issues
        )
        return Response(
            content=canonical_message(response.model_dump(mode="json")),
            status_code=422,
            media_type="application/json",
        )
