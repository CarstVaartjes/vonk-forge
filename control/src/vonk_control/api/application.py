"""Api: application concerns."""

from __future__ import annotations

import json
import re
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi import Path as ApiPath
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import StreamingResponse
from vonk_agent_protocol import CatalogCode, ControllerErrorCode, canonical_message

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from ..agent_api import (
    MAX_RECIPE_IMAGE_BYTES,
    AgentApiServices,
    EnrollmentRateLimiter,
    activation_agent_identity,
    active_agent_identity,
    install_agent_routes,
)
from ..agent_jobs import OperatorRetirementRefused
from ..artifact_job_api import install_artifact_job_routes
from ..artifact_jobs import ArtifactJobService
from ..auth import (
    MUTATION_ROLES,
    Actor,
    AuthError,
    CursorCodec,
    CursorError,
    TokenCodec,
    TrustedProxyAgentIdentityMiddleware,
)
from ..bounded_json import BoundedJSONError
from ..browser_auth import BrowserAuthenticationError, BrowserAuthService
from ..capability_contract import CapabilityUnavailableReply, ControllerCapability
from ..catalog_api import CatalogProblem, install_catalog_routes
from ..catalog_service import CatalogService
from ..download_contract import download_responses
from ..failure_evidence import FailureEvidenceService
from ..failure_evidence_api import install_failure_evidence_routes
from ..fleet_profile_api import install_fleet_profile_routes
from ..fleet_stream import parse_last_event_id
from ..fleet_stream_contract import FLEET_SSE_EVENTS, FleetStreamEvent
from ..gateway_keys import GatewayKeyService, install_gateway_key_routes
from ..http_errors import RetryAfterMiddleware, TemporaryHTTPError
from ..installation_reconciliation_api import install_installation_reconciliation_routes
from ..logging import current_request_id
from ..metrics import MetricsRegistry
from ..model_cache_api import install_model_operator_routes
from ..observation_transfer import (
    ObservationTransferRecord,
    ObservationTransferResponse,
    observation_openapi,
    observation_response,
)
from ..operation_api import (
    ErrorContextResponse,
    HealthzResponse,
    JobDetailResponse,
    JobResumeRequest,
    JobResumeResponse,
    OperationApiServices,
    OperationDetailResponse,
    OperationOwnerReference,
    OperationsResponse,
    ReadyzResponse,
    RequestValidationIssue,
    RequestValidationProblem,
    _global_get_operation,
    _global_list_operations,
    bounded_error_responses,
    bounded_operation_detail,
    bounded_operations_response,
    decode_offset,
    job_response,
    operation_detail_response,
)
from ..operation_contract import OperationRecoveryAction
from ..operation_item_contract import OperationRow, operation_item
from ..operator_projection_api import (
    FleetOperatorServices,
    install_operator_projection_routes,
)
from ..platform_observation import (
    PlatformObserver,
    api_only_capture,
    api_only_observation,
)
from ..platform_observation_errors import (
    ObservationCaptureUnavailable,
    observation_capture_unavailable_response,
)
from ..profile_application_cancel_api import install_profile_application_cancel_route
from ..prometheus_api import PrometheusReader
from ..recipe_operations import RecipeOperationService
from ..run_switch_operations import RunSwitchOperationService
from ..strict_json import ControllerAPIRoute, read_stored_model, warn_unreadable_once
from .common import (
    _ARTIFACT_INPUT_UPLOAD,
    _ARTIFACT_OUTPUT_UPLOAD,
    _LOGIN_PATH,
    _MAX_TELEMETRY_BODY_BYTES,
    _RECIPE_IMAGE_UPLOAD,
    _TELEMETRY_PATH,
    JobQueue,
    _bounded_error_content,
    _bounded_request_body,
    _catalog_error_content,
    _DuplicateJsonKey,
    _FleetEventStreamResponse,
    _http_error_code,
    _invalid_login_content,
    _log_agent_rejection,
    _log_request_failure,
    _reject_duplicate_json_keys,
    _RequestBodyTooLarge,
    _validation_detail,
)


def create_app(
    *,
    jobs: JobQueue,
    tokens: TokenCodec,
    fleet_projection: Any | None = None,
    fleet_services: FleetOperatorServices | None = None,
    failure_evidence: FailureEvidenceService | None = None,
    fleet_stream: Any | None = None,
    library_projection: Any | None = None,
    now: Callable[[], int] = lambda: int(time.time()),
    metrics: MetricsRegistry | None = None,
    prometheus: PrometheusReader | None = None,
    metrics_token: str | Callable[[], str] | None = None,
    metrics_refresh: Callable[[], None] | None = None,
    agent: AgentApiServices | None = None,
    trusted_agent_proxy_auth: bytes | Callable[[], bytes] = b"",
    enrollment_rate_limiter: EnrollmentRateLimiter | None = None,
    operations: OperationApiServices | None = None,
    catalog: CatalogService | None = None,
    recipe_library: Any | None = None,
    managed_catalog_sync: Any | None = None,
    recipe_operations: RecipeOperationService | None = None,
    run_switch_operations: RunSwitchOperationService | None = None,
    artifact_jobs: ArtifactJobService | None = None,
    fleet_profiles: Any | None = None,
    agent_upgrades: Any | None = None,
    browser_auth: BrowserAuthService | None = None,
    model_cache: Any | None = None,
    recipe_image_availability: Any | None = None,
    gateway_keys: GatewayKeyService | None = None,
    lifespan: Any | None = None,
    platform_observer: PlatformObserver | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Vonk Forge Control",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        responses=bounded_error_responses(422, 429, 503),
        lifespan=lifespan,
    )
    app.router.route_class = ControllerAPIRoute
    from ..capabilities import RecoveringService

    cursor_codec = cast(
        CursorCodec,
        RecoveringService(
            ControllerCapability.CURSOR_AUTH,
            CursorCodec,
            tokens.cursor_codec,
            lambda: datetime.now(UTC),
        ),
    )

    @app.exception_handler(StarletteHTTPException)
    async def canonical_agent_http_error(
        request: Request, error: StarletteHTTPException
    ) -> Response:
        from ..library_api import SelectorAmbiguityHTTPError

        if isinstance(error, TemporaryHTTPError):
            return Response(
                error.answer.model_dump_json(),
                status_code=error.status_code,
                headers=error.headers,
                media_type="application/json",
            )

        if isinstance(error.detail, CapabilityUnavailableReply):
            return Response(
                error.detail.model_dump_json(),
                status_code=503,
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
        from ..logging import redact_text

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
            else 512 * 1024**2
            if artifact_input_upload
            else 1024**3
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
            _log_request_failure(request, 500, error)
            response = Response(
                content=_bounded_error_content(
                    "internal server error",
                    context=ErrorContextResponse(
                        operation=f"{request.method} {request.url.path}",
                        endpoint=request.url.path,
                        http_status=500,
                        code=ControllerErrorCode.INTERNAL_ERROR,
                        request_id=request_id,
                        source="unknown",
                        decision="exit",
                    ),
                ),
                status_code=500,
                media_type="application/json",
            )
            response.headers["x-vonk-error-code"] = ControllerErrorCode.INTERNAL_ERROR
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

    def actor(request: Request) -> Actor:
        authorization = request.headers.get("authorization", "")
        cookie_auth = False
        if authorization.startswith("Bearer "):
            encoded = authorization.removeprefix("Bearer ")
            if not encoded:
                raise HTTPException(status_code=401, detail="authentication required")
            try:
                authenticated = tokens.verify(encoded, now=now())
            except AuthError:
                raise HTTPException(
                    status_code=401, detail="authentication failed"
                ) from None
        else:
            encoded = request.cookies.get("vonk_session", "")
            cookie_auth = bool(encoded)
            if not encoded:
                raise HTTPException(status_code=401, detail="authentication required")
            if browser_auth is None:
                raise HTTPException(status_code=401, detail="authentication failed")
            try:
                authenticated = browser_auth.resolve(encoded).actor
            except BrowserAuthenticationError:
                raise HTTPException(
                    status_code=401, detail="authentication failed"
                ) from None
        if cookie_auth and request.method not in {"GET", "HEAD", "OPTIONS"}:
            cookie = request.cookies.get("vonk_csrf")
            header = request.headers.get("x-csrf-token")
            if not cookie or not header or not secrets.compare_digest(cookie, header):
                raise HTTPException(status_code=403, detail="CSRF validation failed")
        return authenticated

    def require_mutation_role(
        authenticated: Actor, path: str, method: str = "POST"
    ) -> None:
        if authenticated.role not in MUTATION_ROLES[(method, path)]:
            raise HTTPException(status_code=403, detail="insufficient role")

    install_agent_routes(
        app,
        services=agent,
        enrollment_rate_limiter=enrollment_rate_limiter,
    )
    authenticated_actor = Depends(actor)

    if browser_auth is not None:
        from ..auth_api import install_auth_routes

        install_auth_routes(
            app,
            browser_auth,
            authenticated_actor,
            tokens=tokens,
            now=now,
        )

    install_catalog_routes(
        app,
        actor_dependency=authenticated_actor,
        service=catalog,
        managed_sync=managed_catalog_sync,
    )
    install_fleet_profile_routes(
        app,
        actor_dependency=authenticated_actor,
        profiles=fleet_profiles,
        operations=operations,
    )
    install_profile_application_cancel_route(
        app,
        actor_dependency=authenticated_actor,
        profiles=fleet_profiles,
    )
    install_gateway_key_routes(
        app, actor_dependency=authenticated_actor, service=gateway_keys
    )
    install_failure_evidence_routes(
        app,
        actor_dependency=authenticated_actor,
        service=failure_evidence,
    )
    install_artifact_job_routes(
        app,
        actor_dependency=authenticated_actor,
        service=artifact_jobs,
    )
    install_model_operator_routes(
        app,
        actor_dependency=authenticated_actor,
        service=model_cache,
    )
    from ..recipe_image_availability_api import install_recipe_operator_routes

    install_recipe_operator_routes(
        app,
        actor_dependency=authenticated_actor,
        service=recipe_image_availability,
    )
    install_installation_reconciliation_routes(
        app,
        actor_dependency=authenticated_actor,
        operations=run_switch_operations,
    )

    from ..cli_update_contract import install_cli_update_contract_routes

    install_cli_update_contract_routes(
        app,
        actor_dependency=authenticated_actor,
        capture=api_only_capture
        if platform_observer is None
        else platform_observer.capture,
    )

    @app.get(
        "/api/platform",
        response_class=ObservationTransferResponse,
        response_model=None,
        operation_id="getPlatformObservation",
        responses={
            200: {"model": ObservationTransferRecord},
            **bounded_error_responses(401, 503),
        },
        openapi_extra=observation_openapi("PlatformObservation"),
    )
    def platform_observation(
        _actor: Actor = authenticated_actor,
    ) -> ObservationTransferResponse | Response:
        try:
            observation = (
                api_only_observation()
                if platform_observer is None
                else platform_observer.read()
            )
        except ObservationCaptureUnavailable as error:
            return observation_capture_unavailable_response(
                error, operation="getPlatformObservation", endpoint="/api/platform"
            )
        return observation_response(observation, resource="platform")

    @app.get("/api/healthz", response_model=HealthzResponse)
    def healthz() -> HealthzResponse:
        return HealthzResponse(status="ok")

    @app.get("/api/readyz", response_model=ReadyzResponse)
    def readyz() -> ReadyzResponse:
        return ReadyzResponse(status="ready")

    @app.get(
        "/metrics",
        response_class=Response,
        responses=download_responses("text/plain"),
        openapi_extra={"x-vonk-streaming-transport": True},
    )
    def platform_metrics(request: Request) -> Response:
        if metrics is None or metrics_token is None:
            raise HTTPException(status_code=404, detail="not found")
        authorization = request.headers.get("authorization", "")
        expected_metrics_token = (
            metrics_token() if callable(metrics_token) else metrics_token
        )
        if not secrets.compare_digest(
            authorization, f"Bearer {expected_metrics_token}"
        ):
            raise HTTPException(status_code=401, detail="authentication required")
        if metrics_refresh is not None:
            metrics_refresh()
        return Response(
            metrics.render(),
            media_type="application/openmetrics-text; version=1.0.0; charset=utf-8",
        )

    @app.get(
        "/api/fleet/stream",
        response_class=_FleetEventStreamResponse,
        responses={
            200: {
                "model": FleetStreamEvent,
                "description": (
                    "Durable Fleet event stream. The schema describes the JSON "
                    "data in each refresh notice, telemetry, or change SSE frame. "
                    "A refresh notice requires a verified complete Fleet read."
                ),
            },
            **bounded_error_responses(400, 401, 503),
        },
        openapi_extra={
            "x-vonk-streaming-transport": True,
            "x-vonk-response-frame-max-bytes": MAX_CONTROL_DOCUMENT_BYTES,
            "x-vonk-sse-events": FLEET_SSE_EVENTS,
        },
        operation_id="streamFleetEvents",
    )
    async def fleet_event_stream(
        request: Request,
        _documented_last_event_id: Annotated[
            str | None,
            Header(
                alias="Last-Event-ID",
                description=(
                    "Optional durable Fleet cursor; duplicate and numeric validity "
                    "are checked from the raw header list."
                ),
            ),
        ] = None,
        _actor: Actor = authenticated_actor,
    ) -> StreamingResponse:
        try:
            last_event_id = parse_last_event_id(
                request.headers.getlist("last-event-id")
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        if fleet_stream is None:
            raise HTTPException(status_code=503, detail="Fleet stream unavailable")
        return _FleetEventStreamResponse(
            fleet_stream.events(last_event_id),
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    # Register literal stream/progress/artifact paths before selector routes.
    install_operator_projection_routes(
        app,
        actor_dependency=authenticated_actor,
        fleet_projection=fleet_projection,
        library_projection=library_projection,
        fleet_services=fleet_services,
        prometheus=prometheus,
    )

    def activity_detail(
        row: OperationRow,
        *,
        tolerate_unreadable: bool = False,
        projected_at: datetime | None = None,
    ) -> OperationDetailResponse:
        """Expose recovery only when its family route is installed."""
        try:
            item = operation_item(row)
            if failure_evidence is not None:
                item = failure_evidence.decorate(item)
            available_actions = (
                (OperationRecoveryAction.RESUME,)
                if item.supported_actions and "resume" in item.supported_actions
                else ()
            )
            return operation_detail_response(
                item, available_actions=available_actions, now=projected_at
            )
        except (BoundedJSONError, OSError, RuntimeError, TypeError, ValueError):
            if not tolerate_unreadable:
                raise
            # A corrupt row keeps its durable identity and timestamp visible;
            # its broken historical details do not hide other current work.
            raw = dict(row) if isinstance(row, Mapping) else row.model_dump(mode="json")
            operation_id = raw.get("id")
            created_at = raw.get("created_at")
            if (
                not isinstance(operation_id, str)
                or not operation_id
                or len(operation_id) > 128
                or not isinstance(created_at, str)
                or not created_at
                or len(created_at) > 64
            ):
                raise
            warn_unreadable_once("operation", operation_id)
            raw_nodes = raw.get("node_ids")
            node_ids = (
                [
                    node
                    for node in raw_nodes
                    if isinstance(node, str) and re.fullmatch(r"spk_[0-9a-f]{32}", node)
                ][:1024]
                if isinstance(raw_nodes, (list, tuple))
                else []
            )
            raw_owner = raw.get("owner")
            owner = None
            if isinstance(raw_owner, Mapping):
                try:
                    owner = read_stored_model(OperationOwnerReference, raw_owner)
                except ValueError:
                    owner = None
            return OperationDetailResponse(
                id=operation_id,
                node_ids=node_ids,
                kind="unreadable",
                state="unavailable",
                attempt=0,
                created_at=created_at,
                owner=owner,
                status_reason="Stored operation history is malformed.",
            )

    @app.get(
        "/api/operations",
        response_model=OperationsResponse,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="listOperations",
        openapi_extra={"x-vonk-response-max-bytes": MAX_CONTROL_DOCUMENT_BYTES},
    )
    def operations_view(
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=20, ge=1, le=100),
        operation_state: str | None = Query(
            default=None,
            alias="state",
            pattern=r"^[a-z][a-z0-9-]{0,31}$",
        ),
        node_id: str | None = Query(default=None, pattern=r"^spk_[0-9a-f]{32}$"),
        request_id: str | None = Query(
            default=None,
            pattern=(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
            ),
        ),
        _actor: Actor = authenticated_actor,
    ) -> OperationsResponse:
        if operations is None:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            )
        try:
            projected_at = operations.clock()
            page = _global_list_operations(
                operations,
                cursor,
                limit,
                operation_state,
                node_id,
                request_id,
                now=projected_at,
            )
        except CursorError:
            raise HTTPException(
                status_code=422, detail="operation cursor is invalid"
            ) from None
        except (TypeError, ValueError):
            return OperationsResponse(
                operations=None,
                total=None,
                projection_issue="Stored operation observations are unreadable; membership and total are unknown.",
            )
        except RuntimeError:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None
        items = [
            activity_detail(item, tolerate_unreadable=True, projected_at=projected_at)
            for item in page.items
        ]
        return bounded_operations_response(
            page,
            items,
            cursors=operations.cursor_codec or cursor_codec,
            state=operation_state,
            node_id=node_id,
            request_id=request_id,
        )

    @app.get(
        "/api/operations/{operation_id}",
        response_model=OperationDetailResponse,
        responses=bounded_error_responses(401, 404, 503),
        operation_id="getOperation",
        openapi_extra={"x-vonk-response-max-bytes": MAX_CONTROL_DOCUMENT_BYTES},
    )
    def operation_view(
        operation_id: str = ApiPath(min_length=1, max_length=128),
        _actor: Actor = authenticated_actor,
    ) -> OperationDetailResponse:
        if operations is None:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            )
        try:
            projected_at = operations.clock()
            item = _global_get_operation(operations, operation_id, now=projected_at)
        except KeyError:
            raise HTTPException(status_code=404, detail="operation not found") from None
        except (RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None
        try:
            return bounded_operation_detail(
                activity_detail(
                    item, tolerate_unreadable=True, projected_at=projected_at
                )
            )
        except BoundedJSONError as error:
            raise HTTPException(status_code=503, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            # A stored document that no longer validates is a declared server
            # fault, not an undeclared 500 and not the caller's request fault.
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None

    @app.get(
        "/api/jobs/{job_id}",
        response_model=JobDetailResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getJob",
    )
    def job_view(
        job_id: str,
        operation_cursor: str | None = Query(default=None, max_length=512),
        target_cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=20, ge=1, le=100),
        _actor: Actor = authenticated_actor,
    ) -> JobDetailResponse:
        try:
            job = jobs.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found") from None
        try:
            projected = (
                None
                if operations is None
                else operations.job_operations(job_id, operation_cursor, limit)
            )
            return job_response(
                job,
                projected,
                target_cursor=decode_offset(
                    target_cursor,
                    job_id=str(job.id),
                    cursors=cursor_codec,
                ),
                limit=limit,
                cursors=cursor_codec,
                evidence_decorator=failure_evidence.decorate
                if failure_evidence is not None
                else None,
            )
        except CursorError:
            raise HTTPException(
                status_code=422, detail="job cursor is invalid"
            ) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            warn_unreadable_once("job operations", job_id)
            return job_response(
                job,
                None,
                target_cursor=decode_offset(
                    target_cursor, job_id=str(job.id), cursors=cursor_codec
                ),
                limit=limit,
                cursors=cursor_codec,
            )

    @app.post(
        "/api/jobs/{job_id}/resume",
        response_model=JobResumeResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="resumeJob",
    )
    def resume_job(
        job_id: str,
        body: Annotated[JobResumeRequest | None, Body()] = None,
        authenticated: Actor = authenticated_actor,
    ) -> JobResumeResponse:
        route = "/api/jobs/{job_id}/resume"
        require_mutation_role(authenticated, route)
        if operations is None:
            raise HTTPException(status_code=503, detail="job resume unavailable")
        disposition = "resume" if body is None else body.disposition
        if disposition == "retire":
            if operations.retire_job is None:
                raise HTTPException(
                    status_code=503, detail="job retirement unavailable"
                )
            try:
                operations.retire_job(job_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="job not found") from None
            except OperatorRetirementRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            except ValueError:
                raise HTTPException(
                    status_code=409, detail="job is not waiting for operator"
                ) from None
            return JobResumeResponse(id=job_id, state="failed")
        try:
            operations.resume_job(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found") from None
        except ValueError:
            raise HTTPException(
                status_code=409, detail="job is not waiting for operator"
            ) from None
        return JobResumeResponse(id=job_id, state="queued")

    return app
