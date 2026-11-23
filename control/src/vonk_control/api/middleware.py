"""Api: request boundary middleware, installed in a fixed order."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable

from fastapi import (
    FastAPI,
    Request,
    Response,
)
from vonk_agent_protocol import (
    ControllerErrorCode,
    Disposition,
)
from vonk_agent_protocol.agent_words import AgentClientDecision
from vonk_agent_protocol.error_response import (
    ErrorContextResponse,
)

from vonk_control.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from ..agent_api.common import (
    MAX_RECIPE_IMAGE_BYTES,
    AgentApiServices,
    activation_agent_identity,
    active_agent_identity,
)
from ..artifact_job_api import (
    MAX_ARTIFACT_INPUT_UPLOAD_BYTES,
    MAX_ARTIFACT_OUTPUT_UPLOAD_BYTES,
)
from ..auth import (
    TrustedProxyAgentIdentityMiddleware,
)
from ..failure_classification import disposition as failure_disposition
from ..http_errors import RetryAfterMiddleware
from ..logging import current_request_id
from ..metrics.registry import MetricsRegistry
from ..observation.delivery import ObservationDeliveryMiddleware
from .common import (
    _ARTIFACT_INPUT_UPLOAD,
    _ARTIFACT_OUTPUT_UPLOAD,
    _LOGIN_PATH,
    _MAX_TELEMETRY_BODY_BYTES,
    _RECIPE_IMAGE_UPLOAD,
    _TELEMETRY_PATH,
    _bounded_error_content,
    _bounded_request_body,
    _DuplicateJsonKey,
    _http_error_code,
    _invalid_login_content,
    _log_request_failure,
    _reject_duplicate_json_keys,
    _RequestBodyTooLarge,
)


def install_request_boundaries(
    app: FastAPI,
    *,
    agent: AgentApiServices | None,
    trusted_agent_proxy_auth: bytes | Callable[[], bytes],
    metrics: MetricsRegistry | None,
) -> None:
    @app.middleware("http")
    async def telemetry_request_boundary(request: Request, call_next):
        if request.method != "POST" or request.url.path != _TELEMETRY_PATH:
            return await call_next(request)
        try:
            json.loads(
                await _bounded_request_body(request, _MAX_TELEMETRY_BODY_BYTES),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except _RequestBodyTooLarge:
            return Response(status_code=413)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey):
            return Response(
                content=_bounded_error_content(
                    "telemetry request is invalid", validation=True
                ),
                status_code=422,
                media_type="application/json",
            )
        return await call_next(request)

    app.add_middleware(ObservationDeliveryMiddleware, services=agent)

    app.add_middleware(
        TrustedProxyAgentIdentityMiddleware,
        trusted_proxy_auth=trusted_agent_proxy_auth,
        agent_identity_validator=(
            lambda identity: active_agent_identity(agent, identity)
        )
        if agent is not None
        else None,
        activation_identity_validator=(
            lambda identity: activation_agent_identity(agent, identity)
        )
        if agent is not None
        else None,
    )

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        started = time.monotonic()
        request_id = request.headers.get("x-request-id")
        try:
            request_id = str(uuid.UUID(request_id)) if request_id else str(uuid.uuid4())
        except ValueError:
            request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        # Each request runs in its own task context, so this binding cannot
        # leak into another request.
        current_request_id.set(request_id)
        length = request.headers.get("content-length")
        recipe_image_upload = (
            request.method == "PUT"
            and _RECIPE_IMAGE_UPLOAD.fullmatch(request.url.path) is not None
        )
        artifact_input_upload = (
            request.method == "PUT"
            and _ARTIFACT_INPUT_UPLOAD.fullmatch(request.url.path) is not None
        )
        artifact_output_upload = (
            request.method == "PUT"
            and _ARTIFACT_OUTPUT_UPLOAD.fullmatch(request.url.path) is not None
        )
        telemetry_ingest = (
            request.method == "POST" and request.url.path == _TELEMETRY_PATH
        )
        maximum = (
            MAX_RECIPE_IMAGE_BYTES
            if recipe_image_upload
            else MAX_ARTIFACT_INPUT_UPLOAD_BYTES
            if artifact_input_upload
            else MAX_ARTIFACT_OUTPUT_UPLOAD_BYTES
            if artifact_output_upload
            else MAX_CONTROL_DOCUMENT_BYTES
        )
        try:
            if telemetry_ingest:
                response = await call_next(request)
            elif (
                length and int(length) > maximum and request.url.path != "/agent/enroll"
            ):
                response = Response(status_code=413)
            else:
                body_too_large = False
                invalid_login_document = False
                if request.method == "POST" and request.url.path == _LOGIN_PATH:
                    try:
                        json.loads(
                            await _bounded_request_body(request, maximum),
                            object_pairs_hook=_reject_duplicate_json_keys,
                        )
                    except _RequestBodyTooLarge:
                        body_too_large = True
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        _DuplicateJsonKey,
                    ):
                        invalid_login_document = True
                if body_too_large:
                    response = Response(status_code=413)
                elif invalid_login_document:
                    response = Response(
                        content=_invalid_login_content(),
                        status_code=422,
                        media_type="application/json",
                    )
                else:
                    response = await call_next(request)
        except Exception as error:  # noqa: BLE001 - middleware safety net, see log below
            # Preserve the correlation key without serializing the exception,
            # request body, headers, or URL query into logs.
            unavailable = failure_disposition(error) in {
                Disposition.WAIT,
                Disposition.OBSERVE,
            }
            http_status = 503 if unavailable else 500
            code = (
                ControllerErrorCode.UNAVAILABLE
                if unavailable
                else ControllerErrorCode.INTERNAL_ERROR
            )
            decision = (
                AgentClientDecision.RETRY if unavailable else AgentClientDecision.EXIT
            )
            _log_request_failure(request, http_status, error)
            response = Response(
                content=_bounded_error_content(
                    "Controller dependency unavailable"
                    if unavailable
                    else "internal server error",
                    context=ErrorContextResponse(
                        operation=f"{request.method} {request.url.path}",
                        endpoint=request.url.path,
                        http_status=http_status,
                        code=code,
                        request_id=request_id,
                        source="unknown",
                        decision=decision.value,
                        retryable=unavailable,
                    ),
                ),
                status_code=http_status,
                media_type="application/json",
            )
            response.headers["x-vonk-error-code"] = code
        response.headers["x-request-id"] = request_id
        response.headers["x-content-type-options"] = "nosniff"
        if response.status_code >= 400 and "x-vonk-error-code" not in response.headers:
            response.headers["x-vonk-error-code"] = _http_error_code(
                response.status_code
            )
        if request.url.path.startswith("/api/auth/"):
            response.headers["cache-control"] = "no-store"
        if metrics is not None:
            metrics.observe_api(
                request.method, response.status_code, time.monotonic() - started
            )
        return response

    app.add_middleware(RetryAfterMiddleware)
