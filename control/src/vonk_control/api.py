"""Versioned authenticated control API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Protocol

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
from fastapi import (
    Path as ApiPath,
)
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, StreamingResponse
from vonk_agent_protocol import (
    CatalogCode,
    ControllerErrorCode,
    SecurityRefusalReason,
    canonical_message,
)
from vonk_agent_protocol.telemetry import MAX_TELEMETRY_REPORT_BYTES

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .agent_api import (
    MAX_RECIPE_IMAGE_BYTES,
    AgentApiServices,
    EnrollmentRateLimiter,
    activation_agent_identity,
    active_agent_identity,
    install_agent_routes,
)
from .agent_jobs import OperatorRetirementRefused
from .artifact_blob_store import ArtifactBlobStore
from .artifact_job_api import install_artifact_job_routes
from .artifact_jobs import ArtifactJobService
from .auth import (
    MUTATION_ROLES,
    Actor,
    AgentSource,
    AuthError,
    CursorError,
    TokenCodec,
    TrustedProxyAgentIdentityMiddleware,
)
from .bounded_json import BoundedJSONError
from .browser_auth import BrowserAuthenticationError, BrowserAuthService
from .catalog_api import CatalogProblem, install_catalog_routes
from .catalog_service import CatalogService
from .catalog_sync import (
    ManagedRecipeCatalogSyncService,
    run_automatic_sync,
)
from .cluster_mappings import ClusterMappingService
from .distribution_executor import CompositeDistributionPhaseExecutor
from .download_contract import download_responses
from .failure_evidence import FailureEvidenceService
from .failure_evidence_api import install_failure_evidence_routes
from .fleet_profile_api import install_fleet_profile_routes
from .fleet_projection import (
    FleetSnapshot,
)
from .fleet_stream import parse_last_event_id
from .fleet_stream_contract import FleetStreamEvent
from .gateway_keys import (
    GatewayKeyService,
    install_gateway_key_routes,
    keep_default_key,
)
from .installation_reconciliation_api import (
    install_installation_reconciliation_routes,
)
from .library_assessment import LibraryAssessment
from .logging import configure_controller_logging, current_request_id
from .metrics import MetricsRegistry, runnable_job_ages
from .model_cache_api import (
    install_model_operator_routes,
    register_model_cache_operation_provider,
)
from .operation_api import (
    BoundedErrorResponse,
    ErrorContextResponse,
    HealthzResponse,
    JobDetailResponse,
    JobProgress,
    JobResumeRequest,
    JobResumeResponse,
    OperationApiServices,
    OperationDetailResponse,
    OperationOwnerReference,
    OperationPage,
    OperationsResponse,
    ReadyzResponse,
    RequestValidationIssue,
    RequestValidationProblem,
    _global_get_operation,
    _global_list_operations,
    bounded_error_responses,
    decode_offset,
    job_response,
    operation_detail_response,
)
from .operation_contract import OperationRecoveryAction
from .operation_item_contract import OperationRow, operation_item
from .operator_projection_api import (
    FleetOperatorServices,
    build_fleet_operator_services,
    install_operator_projection_routes,
)
from .profile_application_cancel_api import install_profile_application_cancel_route
from .recipe_builds import RecipeBuildService
from .recipe_operations import RecipeOperationService
from .recipe_packages import RecipePackageClient
from .resource_planning import PLATFORM_MEMORY_FLOOR_BYTES
from .run_switch_operations import RunSwitchOperationService
from .settings import (
    AGENT_CA_PROVISIONER_NAME,
    AGENT_CA_URL,
    AGENT_RELEASE_API_URL,
    ARTIFACT_JOB_RETENTION_SECONDS,
    ARTIFACT_JOB_STORAGE_MAX_BYTES,
    DISTRIBUTED_START_TIMEOUT_SECONDS,
    MODEL_CACHE_MAX_DOWNLOAD_STREAMS,
    MODEL_CACHE_PARALLEL_DOWNLOADS,
    MODEL_CACHE_RESERVE_BYTES,
    RECIPE_IMAGE_PARALLEL_PREPARATIONS,
    RECIPE_LIBRARY_API_URL,
    RECIPE_LIBRARY_ASSET_URL,
    RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS,
    WORKER_MEMORY_REPORT_MAX_AGE_SECONDS,
    Settings,
)
from .source_bundles import DatabaseSourceBundleStore
from .strict_json import (
    ControllerAPIRoute,
    read_stored_model,
    warn_unreadable_once,
)

_LOGGER = logging.getLogger(__name__)

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

    from .logging import redact_text

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
    from .auth_api import LoginRequestInvalid

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
    from .logging import log_event

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
    from .logging import log_event

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


def build_agent_services(
    settings: Any,
    sessions: Any,
    clock: Callable[[], Any],
    *,
    distribution: Any | None = None,
    model_cache: Any | None = None,
) -> AgentApiServices:
    """Construct the fail-closed production agent runtime from one provider."""
    from .agent_jobs import AgentJobService
    from .enrollment import EnrollmentService
    from .enrollment_bootstrap import EnrollmentBootstrapConfig
    from .host_helper_authority import (
        HostHelperGrantIssuer,
        HostRuntimeAuthorityService,
    )
    from .presence import AgentPresenceService, ManagementAddressPolicy
    from .step_ca import StepCertificateAuthority

    if distribution is None and model_cache is not None:
        from .distribution import build_distribution_service_from_components

        distribution = build_distribution_service_from_components(
            model_cache,
            sessions,
            settings.agent_artifact_root,
            clock=clock,
        )
    if distribution is not None:
        attach_sessions = getattr(distribution, "attach_sessions", None)
        if callable(attach_sessions):
            attach_sessions(sessions)

    if not settings.agent_runtime_enabled:
        # Local development still needs the durable operation queue and fleet
        # presence service, but deliberately has no enrollment or certificate
        # authority.  Agent HTTP routes stay disabled by production_app.
        operations = AgentJobService(
            sessions,
            clock=clock,
        )
        policy = ManagementAddressPolicy.parse(
            settings.management_cidrs or "127.0.0.1/32",
            forbidden_cidrs=settings.direct_fabric_cidrs,
        )
        presence = AgentPresenceService(sessions, policy, clock=clock)
        return AgentApiServices(
            enrollment=None,
            operations=operations,
            sessions=sessions,
            clock=clock,
            presence=presence,
            artifact_root=settings.agent_artifact_root,
            source_bundles=DatabaseSourceBundleStore(sessions),
            distribution=distribution,
        )

    bootstrap = EnrollmentBootstrapConfig.from_paths(
        controller_endpoint=settings.agent_controller_origin,
        enrollment_endpoint=settings.agent_enrollment_origin,
        controller_ca_path=settings.controller_ca_path,
        controller_address=settings.nas_lan_ip,
        service_hostnames=(
            settings.agent_service_hostnames if settings.nas_lan_ip else ()
        ),
        installer_url=(
            "https://install.vonkforge.ai/dev/spark"
            if settings.install_channel == "dev"
            else "https://install.vonkforge.ai/spark"
        ),
    )
    authority = StepCertificateAuthority(
        ca_url=AGENT_CA_URL,
        root_certificate_path=settings.agent_ca_root_path,
        intermediate_certificate_path=settings.agent_intermediate_certificate_path,
        provisioner_name=AGENT_CA_PROVISIONER_NAME,
        provisioner_kid=settings.agent_ca_provisioner_kid,
        credential_path=settings.agent_ca_credential_path,
        provisioner_public_jwk_path=settings.agent_ca_provisioner_public_jwk_path,
        certificate_lifetime_seconds=settings.agent_ca_certificate_lifetime_seconds,
    )
    settings.agent_artifact_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    presence = AgentPresenceService(
        sessions,
        ManagementAddressPolicy.parse(
            settings.management_cidrs,
            forbidden_cidrs=settings.direct_fabric_cidrs,
        ),
        clock=clock,
    )
    operations = AgentJobService(
        sessions,
        clock=clock,
    )

    def observe_contact(session: Session, source: AgentSource) -> None:
        presence.observe_in_session(session, source)

    operations.set_contact_consumer(observe_contact)
    host_runtime_key_path = settings.host_runtime_grant_private_key_path
    if host_runtime_key_path is None:
        raise RuntimeError("host runtime authority key is unavailable")
    host_runtime_authority = HostRuntimeAuthorityService(
        sessions,
        HostHelperGrantIssuer.from_private_key_file(host_runtime_key_path, clock=clock),
        clock=clock,
    )
    return AgentApiServices(
        enrollment=EnrollmentService(sessions, authority, clock=clock),
        operations=operations,
        sessions=sessions,
        clock=clock,
        presence=presence,
        artifact_root=settings.agent_artifact_root,
        source_bundles=DatabaseSourceBundleStore(sessions),
        distribution=distribution,
        host_runtime_authority=host_runtime_authority,
        fabric_policy=(
            ManagementAddressPolicy.parse(
                settings.direct_fabric_cidrs,
                forbidden_cidrs=settings.management_cidrs,
            )
            if settings.direct_fabric_cidrs
            else None
        ),
        bootstrap=bootstrap,
    )


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
    metrics_token: str | None = None,
    metrics_refresh: Callable[[], None] | None = None,
    agent: AgentApiServices | None = None,
    trusted_agent_proxy_auth: bytes = b"",
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
) -> FastAPI:
    app = FastAPI(
        title="Vonk Forge Control",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        responses={422: {"model": RequestValidationProblem}},
        lifespan=lifespan,
    )
    app.router.route_class = ControllerAPIRoute
    cursor_codec = tokens.cursor_codec()

    @app.exception_handler(StarletteHTTPException)
    async def canonical_agent_http_error(
        request: Request, error: StarletteHTTPException
    ) -> Response:
        from .library_api import SelectorAmbiguityHTTPError

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
        from .logging import redact_text

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
        from .auth_api import install_auth_routes

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
    from .recipe_image_availability_api import install_recipe_operator_routes

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
        if not secrets.compare_digest(authorization, f"Bearer {metrics_token}"):
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
                    "data in each snapshot, telemetry, or change SSE frame."
                ),
            },
            **bounded_error_responses(400, 401, 503),
        },
        openapi_extra={"x-vonk-streaming-transport": True},
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
    )

    def activity_detail(
        row: OperationRow, *, tolerate_unreadable: bool = False
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
            return operation_detail_response(item, available_actions=available_actions)
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
            page = _global_list_operations(
                operations, cursor, limit, operation_state, node_id, request_id
            )
        except CursorError:
            raise HTTPException(
                status_code=422, detail="operation cursor is invalid"
            ) from None
        except (RuntimeError, TypeError, ValueError):
            # Anything else here is the projection refusing stored operation
            # state, not the caller's cursor, so it must not read as a request
            # fault.
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None
        items = [activity_detail(item, tolerate_unreadable=True) for item in page.items]
        return OperationsResponse(
            operations=items,
            next_cursor=page.next_cursor,
            total=page.total,
        )

    @app.get(
        "/api/operations/{operation_id}",
        response_model=OperationDetailResponse,
        responses=bounded_error_responses(401, 404, 503),
        operation_id="getOperation",
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
            item = _global_get_operation(operations, operation_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="operation not found") from None
        except (RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None
        try:
            return activity_detail(item)
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
                OperationPage(
                    (), None, JobProgress(completed=0, failed=0, running=0, total=0)
                )
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
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None

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


def production_app(settings: Settings | None = None) -> FastAPI:
    configure_controller_logging()
    from sqlalchemy import func, select

    from .agent_upgrades import AgentUpgradeService
    from .availability_production import build_recipe_image_availability
    from .db import build_engine, session_factory
    from .execution_plan_service import ControllerExecutionPlanService
    from .fleet_events import FleetEventRepository
    from .fleet_projection import FleetProjection
    from .fleet_stream import FleetStream
    from .install_admission import (
        InstallAdmissionService,
    )
    from .jobs import JobService
    from .library_projection import LibraryProjection
    from .metrics import MetricsRegistry, OperationalMetricsCollector
    from .model_cache import ModelCacheService
    from .models import Job
    from .operation_api import durable_operation_services
    from .presence import ManagementAddressPolicy
    from .recipe_routes import AtomicRecipeRoutePublisher, RecipeRouteService
    from .route_runtime import AtomicRouteBundlePublisher, FileSupervisorAcknowledger
    from .run_admission import RunAdmissionService
    from .runtime_image_preparation import (
        FilesystemRuntimeImageStorage,
        OciLayoutImageTransport,
        make_runtime_image_receipt_preparer,
        stored_runtime_image_resolver,
    )
    from .telemetry import TelemetryRepository
    from .worker_memory import read_worker_memory_report, worker_memory_report_path

    if settings is None:
        settings = Settings.from_env_and_secrets()
    sessions = session_factory(build_engine(settings.database_url, component="api"))
    # Planning, profile choices, and preparation share the same managed OCI root.
    runtime_image_storage = FilesystemRuntimeImageStorage(settings.agent_artifact_root)

    def clock() -> datetime:
        return datetime.now(UTC)

    token_codec = TokenCodec(settings.token_signing_key)
    cursor_codec = token_codec.cursor_codec()
    job_service = JobService(sessions, clock=clock)
    database_bundles = DatabaseSourceBundleStore(sessions)
    telemetry_repository = TelemetryRepository(sessions, clock=clock)
    fleet_event_repository = FleetEventRepository(sessions, clock=clock)
    visual_fleet = FleetProjection(
        sessions,
        clock=clock,
        events=fleet_event_repository,
        telemetry=telemetry_repository,
    )
    visual_fleet_stream = FleetStream(
        fleet_event_repository,
        telemetry_repository,
        visual_fleet,
        clock=clock,
    )
    metrics = MetricsRegistry()
    operational_metrics = OperationalMetricsCollector(
        metrics,
        sessions,
        clock=clock,
    )
    model_cache = ModelCacheService(
        sessions,
        settings.model_cache_root,
        reserve_bytes=MODEL_CACHE_RESERVE_BYTES,
        max_parallel_downloads=MODEL_CACHE_PARALLEL_DOWNLOADS,
        max_download_streams=MODEL_CACHE_MAX_DOWNLOAD_STREAMS,
        clock=clock,
        huggingface_token_path=settings.huggingface_token_path,
        runtime_archive_available=runtime_image_storage.build_archive_available,
    )
    model_cache.resume_operations()

    agent_services = build_agent_services(
        settings,
        sessions,
        clock,
        model_cache=model_cache,
    )
    runtime_image_transport = OciLayoutImageTransport()
    prepare_runtime_image_receipt = make_runtime_image_receipt_preparer(
        runtime_image_storage,
        runtime_image_transport,
        clock=clock,
    )

    execution_plans = ControllerExecutionPlanService(
        model_cache,
        runtime_image_resolver=stored_runtime_image_resolver(runtime_image_storage),
    )

    recipe_route_runtime = AtomicRouteBundlePublisher(
        Path("/routes"),
        await_supervisor_ack=FileSupervisorAcknowledger(
            Path("/supervisor/ack.json"), clock=clock
        ),
    )
    recipe_routes = RecipeRouteService(
        sessions,
        publisher=AtomicRecipeRoutePublisher(recipe_route_runtime),
        management_policy=ManagementAddressPolicy.parse(
            settings.management_cidrs,
            forbidden_cidrs=settings.direct_fabric_cidrs,
        ),
        clock=clock,
    )
    recipe_builds = RecipeBuildService(
        sessions,
        bundles=database_bundles,
        inventory_max_age=300,
        build_archive_available=runtime_image_storage.build_archive_available,
        prepared_builds=runtime_image_storage.find_build,
    )
    recipe_operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(
            sessions,
            inventory_max_age=300,
            disk_floor_bytes=10_000_000_000,
            compiled_plan_provider=execution_plans.compile_installation,
        ),
        run_admission=RunAdmissionService(
            sessions,
            inventory_max_age=300,
            memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
        ),
        agent_jobs=agent_services.operations,
        clock=clock,
        route_publications=recipe_routes,
        builds=recipe_builds,
        mappings=ClusterMappingService(sessions),
        distributed_start_timeout_seconds=DISTRIBUTED_START_TIMEOUT_SECONDS,
    )
    run_switch_operations = RunSwitchOperationService(
        sessions,
        memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
        lifecycle=recipe_operations,
        clock=clock,
        mappings=ClusterMappingService(sessions),
        model_cache=model_cache,
        build_archive_available=runtime_image_storage.build_archive_available,
        artifact_phase_executor=CompositeDistributionPhaseExecutor(
            sessions,
            agent_services.operations,
            agent_services.distribution,
            model_cache=model_cache,
            runtime_image_preparer=prepare_runtime_image_receipt,
            clock=clock,
        ),
    )
    visual_library = LibraryProjection(
        sessions,
        cursors=cursor_codec,
        clock=clock,
        runtime_archive_available=runtime_image_storage.build_archive_available,
        # The Library reports stored images; it never gates on them. Recent
        # answers are reused so a read does not re-walk network storage.
        image_present_ttl_seconds=30.0,
        image_absent_ttl_seconds=5.0,
        assessment=LibraryAssessment(
            sessions,
            run_switch=run_switch_operations,
            model_cache=model_cache,
            clock=clock,
        ),
    )
    artifact_jobs = ArtifactJobService(
        sessions,
        recipe_operations=recipe_operations,
        blob_store=ArtifactBlobStore(
            settings.state_path / "artifact-jobs" / "blobs",
            max_stored_bytes=ARTIFACT_JOB_STORAGE_MAX_BYTES,
        ),
        clock=clock,
        retention_seconds=ARTIFACT_JOB_RETENTION_SECONDS,
    )
    artifact_jobs.reconcile_storage()
    from .fleet_profiles import build_production_fleet_profile_service

    fleet_profiles = build_production_fleet_profile_service(
        sessions,
        clock=clock,
        run_switch_operations=run_switch_operations,
        cache_resolver=model_cache.resolve_latest_cached,
    )
    agent_upgrades = AgentUpgradeService(
        sessions,
        agent_services.operations,
        clock=clock,
        channel=settings.install_channel,
        release_api_url=AGENT_RELEASE_API_URL,
    )

    def consume_agent_result(session, operation, attempt, message) -> None:
        artifact_jobs.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)
        agent_upgrades.consume_agent_result(session, operation, attempt, message)

    agent_services.operations.set_result_consumer(consume_agent_result)

    def refresh_metrics() -> None:
        operational_metrics.refresh()
        now = datetime.now(UTC)
        metrics.set_worker_memory(
            read_worker_memory_report(
                worker_memory_report_path(settings.state_path),
                now=now,
                max_age_seconds=WORKER_MEMORY_REPORT_MAX_AGE_SECONDS,
            ),
            now,
        )
        refresh_fleet_metrics(metrics, visual_fleet.read())
        now = datetime.now(UTC)
        with sessions() as session:
            job_counts = [
                (kind, state, count)
                for kind, state, count in session.execute(
                    select(Job.kind, Job.state, func.count()).group_by(
                        Job.kind, Job.state
                    )
                )
            ]
            metrics.replace_job_counts(job_counts)
            # Oldest queued work an operator could actually run now.  A job
            # deferred by a future ``observation_due_at`` is an intentional wait,
            # not starvation, and must not age into the alert.
            metrics.replace_runnable_job_ages(
                runnable_job_ages(
                    (
                        (row.kind, row.created_at, row.result)
                        for row in session.execute(
                            select(Job.kind, Job.created_at, Job.result).where(
                                Job.state == "queued"
                            )
                        )
                    ),
                    now,
                ).items()
            )
        backup_marker = settings.state_path / "last-successful-backup.epoch"
        backup_completed_at: int | None = None
        if backup_marker.is_file() and not backup_marker.is_symlink():
            try:
                backup_completed_at = int(backup_marker.read_text().strip())
                if backup_completed_at < 0:
                    backup_completed_at = None
            except (OSError, ValueError):
                pass
        metrics.set_backup_successful(backup_completed_at is not None)
        metrics.set_backup_age(
            None
            if backup_completed_at is None
            else max(0, int(time.time()) - backup_completed_at)
        )
        restore_marker = settings.state_path / "last-backup-restore-verification.epoch"
        restore_completed_at: int | None = None
        if restore_marker.is_file() and not restore_marker.is_symlink():
            try:
                restore_completed_at = int(restore_marker.read_text().strip())
                if restore_completed_at < 0:
                    restore_completed_at = None
            except (OSError, ValueError):
                pass
        metrics.set_backup_restore_verified(restore_completed_at is not None)
        metrics.set_backup_restore_verification_age(
            None
            if restore_completed_at is None
            else max(0, int(time.time()) - restore_completed_at)
        )

    recipe_library = RecipePackageClient(
        cache_root=settings.state_path / "recipe-library-packages",
        api_url=RECIPE_LIBRARY_API_URL,
        asset_url=RECIPE_LIBRARY_ASSET_URL,
        release=settings.recipe_library_release,
    )
    catalog_service = CatalogService(
        sessions,
        clock=clock,
        cursors=cursor_codec,
        source_bundles=database_bundles,
    )
    managed_catalog_sync = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=catalog_service,
        reader=recipe_library,
        clock=clock,
    )
    recipe_image_production = build_recipe_image_availability(
        sessions,
        settings=settings,
        managed_catalog_sync=managed_catalog_sync,
        recipe_builds=recipe_builds,
        recipe_operations=recipe_operations,
        model_cache=model_cache,
        clock=clock,
        max_parallel=RECIPE_IMAGE_PARALLEL_PREPARATIONS,
    )
    # A load asks for the preparation it needs instead of stopping at its absence.
    fleet_profiles.bind_preparation_starter(
        recipe_image_production.service.ensure_preparation
    )
    fleet_profiles.bind_preparation_canceller(
        recipe_image_production.service.cancel_profile_preparation
    )

    automatic_sync_task: asyncio.Task[None] | None = None
    automatic_sync_stop = asyncio.Event()

    gateway_keys = GatewayKeyService()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal automatic_sync_task
        automatic_sync_task = asyncio.create_task(
            run_automatic_sync(
                managed_catalog_sync,
                automatic_sync_stop,
                interval_seconds=RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS,
            )
        )
        default_key_task = asyncio.create_task(
            keep_default_key(gateway_keys, automatic_sync_stop)
        )
        try:
            yield
        finally:
            automatic_sync_stop.set()
            await default_key_task
            if automatic_sync_task is not None:
                await automatic_sync_task
            model_cache.close()
            recipe_image_production.close()
            recipe_library.close()
            agent_upgrades.close()

    app = create_app(
        jobs=job_service,
        tokens=token_codec,
        fleet_projection=visual_fleet,
        fleet_stream=visual_fleet_stream,
        library_projection=visual_library,
        metrics=metrics,
        metrics_token=settings.metrics_token,
        metrics_refresh=refresh_metrics,
        agent=(agent_services if settings.agent_runtime_enabled else None),
        trusted_agent_proxy_auth=settings.agent_proxy_auth,
        operations=register_model_cache_operation_provider(
            durable_operation_services(
                sessions,
                Path("/routes"),
                clock=clock,
                cursors=cursor_codec,
                resume_agent_upgrade=agent_upgrades.resume,
                operation_providers=(
                    fleet_profiles.operation_provider(),
                    run_switch_operations.activity_provider(),
                    recipe_image_production.service.update_activity_provider(),
                ),
                profile_endpoint_intent=fleet_profiles.endpoint_intent,
            ),
            model_cache,
        ),
        catalog=catalog_service,
        recipe_library=recipe_library,
        managed_catalog_sync=managed_catalog_sync,
        browser_auth=BrowserAuthService(
            sessions,
            token_signing_key=settings.token_signing_key,
            clock=clock,
        ),
        recipe_operations=recipe_operations,
        run_switch_operations=run_switch_operations,
        artifact_jobs=artifact_jobs,
        fleet_profiles=fleet_profiles,
        fleet_services=build_fleet_operator_services(
            agent_services=(agent_services if settings.agent_runtime_enabled else None),
            upgrades=agent_upgrades,
            sessions=sessions,
        ),
        failure_evidence=FailureEvidenceService(sessions),
        agent_upgrades=agent_upgrades,
        model_cache=model_cache,
        recipe_image_availability=recipe_image_production.service,
        gateway_keys=gateway_keys,
        lifespan=lifespan,
    )
    web_root = Path(__file__).resolve().parent / "web"
    if web_root.is_dir():
        app.mount("/", SpaFiles(directory=web_root, html=True), name="admin-web")

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(production_app(), host="0.0.0.0", port=8000, access_log=False)
