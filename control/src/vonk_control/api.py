"""Versioned authenticated control API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, StreamingResponse
from vonk_agent_protocol import canonical_message

from .agent_api import (
    MAX_RECIPE_IMAGE_BYTES,
    AgentApiServices,
    EnrollmentRateLimiter,
    activation_agent_identity,
    active_agent_identity,
    install_agent_routes,
)
from .artifact_blob_store import ArtifactBlobStore
from .artifact_job_api import install_artifact_job_routes
from .artifact_jobs import ArtifactJobService
from .audit import AuditRecord, IdentityHistoryRecord
from .auth import (
    MUTATION_ROLES,
    Actor,
    AuthError,
    TokenCodec,
    TrustedProxyAgentIdentityMiddleware,
)
from .browser_auth import BrowserAuthenticationError, BrowserAuthService
from .catalog_api import install_catalog_routes
from .catalog_service import CatalogError, CatalogService
from .catalog_sync import CatalogSyncError, ManagedRecipeCatalogSyncService
from .cluster_mappings import ClusterMappingService
from .database_authority import (
    AuthorityChange,
)
from .fleet_profile_api import install_fleet_profile_routes
from .fleet_projection import (
    FleetNodeIdentity,
    FleetSnapshot,
    TelemetryHistoryResponse,
)
from .fleet_stream import parse_last_event_id
from .global_catalog import GlobalCatalogClient
from .library_api import install_library_routes
from .library_placement_api import install_library_placement_routes
from .metrics import MetricsRegistry
from .model_cache_api import (
    install_model_cache_routes,
    register_model_cache_operation_provider,
)
from .operation_api import (
    AgentsResponse,
    EndpointResponse,
    FleetStatusResponse,
    JobDetailResponse,
    JobLogsResponse,
    JobProgress,
    JobResumeResponse,
    JobsResponse,
    OperationApiServices,
    OperationDetailResponse,
    OperationPage,
    OperationsResponse,
    _global_get_operation,
    _global_list_operations,
    bounded_error_responses,
    decode_offset,
    fleet_response,
    job_response,
    operation_detail_response,
)
from .recipe_api import install_recipe_operation_routes
from .recipe_builds import RecipeBuildService
from .recipe_library import RecipeLibraryClient, RecipeLibraryError
from .recipe_operations import RecipeOperationService
from .run_switch_api import install_run_switch_routes
from .run_switch_operations import RunSwitchOperationService
from .source_bundles import DatabaseSourceBundleStore
from .telemetry import TelemetryResolution
from .workload_run_api import install_workload_run_routes
from .workload_run_workflow import WorkloadRunWorkflow

_LOGGER = logging.getLogger(__name__)

_RECIPE_IMAGE_UPLOAD = re.compile(
    r"/agent/v1/recipe-builds/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/image\Z"
)
_LOGIN_PATH = "/api/v1/auth/login"
_TELEMETRY_PATH = "/agent/v1/telemetry"
_MAX_TELEMETRY_BODY_BYTES = 64 * 1024
_ARTIFACT_INPUT_UPLOAD = re.compile(
    r"/api/v1/artifact-jobs/[0-9a-f-]{36}/inputs/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z"
)
_ARTIFACT_OUTPUT_UPLOAD = re.compile(
    r"/agent/v1/recipe-jobs/[0-9a-f-]{36}/outputs/[0-9a-f]{64}\Z"
)


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


async def _bounded_request_body(request: Request, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise _RequestBodyTooLarge
        body.extend(chunk)
    bounded = bytes(body)
    request._body = bounded
    return bounded


@dataclass(frozen=True)
class AdminServices:
    authority: Any
    proposals: Any
    changes: Any | None


def build_agent_services(
    settings: Any,
    sessions: Any,
    clock: Callable[[], Any],
    *,
    revision_eligible: Callable[[str], bool] | None = None,
    current_revision: Callable[[], str] | None = None,
    distribution: Any | None = None,
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
    from .workload_helper_authority import (
        WorkloadHelperAuthorityService,
        WorkloadHelperGrantIssuer,
        WorkloadObjectReceiptIssuer,
    )

    if distribution is not None:
        attach_sessions = getattr(distribution, "attach_sessions", None)
        if callable(attach_sessions):
            attach_sessions(sessions)

    if settings.agent_runtime != "enabled":
        # Local development still needs the durable operation queue and fleet
        # presence service, but deliberately has no enrollment or certificate
        # authority.  Agent HTTP routes stay disabled by production_app.
        operations = AgentJobService(
            sessions,
            clock=clock,
            revision_eligible=revision_eligible,
            current_revision=current_revision,
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
            workload_tuf_metadata_root=settings.workload_tuf_metadata_root,
            workload_tuf_target_root=settings.workload_tuf_target_root,
            distribution=distribution,
        )

    if settings.agent_intermediate_certificate_path is None:
        raise RuntimeError("agent intermediate certificate path is unavailable")
    if settings.controller_ca_path is None:
        raise RuntimeError("controller CA path is unavailable")
    bootstrap = EnrollmentBootstrapConfig.from_paths(
        controller_endpoint=settings.agent_controller_origin,
        enrollment_endpoint=settings.agent_enrollment_origin,
        controller_ca_path=settings.controller_ca_path,
        controller_address=settings.agent_controller_address,
        service_hostnames=settings.agent_service_hostnames,
        installer_url=(
            "https://install.vonkforge.ai/dev/spark"
            if getattr(settings, "install_channel", "stable") == "dev"
            else "https://install.vonkforge.ai/spark"
        ),
    )
    if (
        settings.agent_ca_root_path is None
        or settings.agent_ca_credential_path is None
        or settings.agent_ca_provisioner_public_jwk_path is None
    ):
        raise RuntimeError("step-ca provider files are unavailable")
    authority = StepCertificateAuthority(
        ca_url=settings.agent_ca_url,
        root_certificate_path=settings.agent_ca_root_path,
        intermediate_certificate_path=settings.agent_intermediate_certificate_path,
        provisioner_name=settings.agent_ca_provisioner_name,
        provisioner_kid=settings.agent_ca_provisioner_kid,
        credential_path=settings.agent_ca_credential_path,
        provisioner_public_jwk_path=settings.agent_ca_provisioner_public_jwk_path,
        timeout_seconds=settings.agent_ca_timeout_seconds,
        max_response_bytes=settings.agent_ca_max_response_bytes,
        certificate_lifetime_seconds=settings.agent_ca_certificate_lifetime_seconds,
    )
    workload_tuf_metadata_root = getattr(
        settings,
        "workload_tuf_metadata_root",
        settings.agent_artifact_root.parent / "workload-tuf/metadata",
    )
    workload_tuf_target_root = getattr(
        settings,
        "workload_tuf_target_root",
        settings.agent_artifact_root.parent / "workload-tuf/targets",
    )
    for root in (
        settings.agent_artifact_root,
        workload_tuf_metadata_root,
        workload_tuf_target_root,
    ):
        root.mkdir(mode=0o750, parents=True, exist_ok=True)
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
        revision_eligible=revision_eligible,
        current_revision=current_revision,
    )
    operations.set_contact_consumer(presence.observe_in_session)
    helper_authority = None
    host_runtime_authority = None
    grant_key_path = getattr(settings, "package_helper_grant_private_key_path", None)
    receipt_key_path = getattr(
        settings, "package_helper_receipt_private_key_path", None
    )
    if getattr(settings, "deployment_mode", "") == "production" and (
        grant_key_path is None or receipt_key_path is None
    ):
        raise RuntimeError("workload helper authority keys are unavailable")
    if grant_key_path is not None and receipt_key_path is not None:
        helper_authority = WorkloadHelperAuthorityService(
            sessions,
            WorkloadHelperGrantIssuer.from_private_key_file(
                grant_key_path, clock=clock
            ),
            WorkloadObjectReceiptIssuer.from_private_key_file(receipt_key_path),
            workload_target_root=workload_tuf_target_root,
            clock=clock,
        )
    host_runtime_key_path = getattr(
        settings, "host_runtime_grant_private_key_path", None
    )
    if getattr(settings, "deployment_mode", "") == "production" and (
        host_runtime_key_path is None
    ):
        raise RuntimeError("host runtime authority key is unavailable")
    if host_runtime_key_path is not None:
        host_runtime_authority = HostRuntimeAuthorityService(
            sessions,
            HostHelperGrantIssuer.from_private_key_file(
                host_runtime_key_path, clock=clock
            ),
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
        workload_tuf_metadata_root=workload_tuf_metadata_root,
        workload_tuf_target_root=workload_tuf_target_root,
        distribution=distribution,
        workload_helper_authority=helper_authority,
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
            if error.status_code == 404 and "." not in path:
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
    def list(self, *, limit: int = 100) -> list[Any]: ...
    def list_page(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        status: str | None = None,
        target: str | None = None,
    ) -> tuple[list[Any], str | None, int]: ...


class AuditSink(Protocol):
    def append(self, event: AuditRecord) -> None: ...
    def list(self, *, limit: int = 100) -> list[AuditRecord]: ...
    def identity_history(self, *, limit: int = 100) -> list[IdentityHistoryRecord]: ...


def refresh_fleet_metrics(
    metrics: MetricsRegistry,
    fleet_state: Mapping[str, object],
) -> None:
    """Refresh bounded fleet series while omitting unknown probe ages."""

    nodes = fleet_state.get("nodes")
    if not isinstance(nodes, Sequence):
        raise TypeError("fleet metrics nodes are invalid")
    for node in nodes:
        if not isinstance(node, Mapping):
            raise TypeError("fleet metrics node is invalid")
        probe_age = node.get("probe_age_seconds")
        metrics.update_node(
            str(node["id"]),
            ready=node.get("healthy") is True,
            memory_available_bytes=int(node["memory_available_bytes"]),
            disk_available_bytes=int(node["disk_available_bytes"]),
            probe_age_seconds=(None if probe_age is None else float(probe_age)),
        )


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=80)
    authority_revision: str = Field(min_length=1, max_length=128)
    targets: list[str] = Field(max_length=64)
    payload: dict[str, object]


class JobResponse(BaseModel):
    id: str
    state: str


class ProposalChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)
    document: dict[str, object]


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: list[ProposalChangeRequest] = Field(min_length=1, max_length=32)


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class NodeProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    display_name: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    )


def create_app(
    *,
    jobs: JobQueue,
    tokens: TokenCodec,
    audits: AuditSink,
    fleet: Callable[[], Mapping[str, object]],
    fleet_projection: Any | None = None,
    fleet_stream: Any | None = None,
    library_projection: Any | None = None,
    now: Callable[[], int] = lambda: int(time.time()),
    admin: AdminServices | None = None,
    metrics: MetricsRegistry | None = None,
    metrics_token: str | None = None,
    metrics_refresh: Callable[[], None] | None = None,
    job_logs=None,
    agent: AgentApiServices | None = None,
    trusted_agent_proxy_auth: bytes = b"",
    enrollment_rate_limiter: EnrollmentRateLimiter | None = None,
    worker_authority: Any | None = None,
    worker_api_token: bytes = b"",
    generic_jobs_enabled: bool = False,
    operations: OperationApiServices | None = None,
    catalog: CatalogService | None = None,
    global_catalog: Any | None = None,
    recipe_library: Any | None = None,
    managed_catalog_sync: Any | None = None,
    workload_run: WorkloadRunWorkflow | None = None,
    recipe_operations: RecipeOperationService | None = None,
    run_switch_operations: RunSwitchOperationService | None = None,
    artifact_jobs: ArtifactJobService | None = None,
    fleet_profiles: Any | None = None,
    library_placements: Any | None = None,
    agent_upgrades: Any | None = None,
    browser_auth: BrowserAuthService | None = None,
    model_cache: Any | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Vonk Forge Control", version="1.0", docs_url=None, redoc_url=None
    )
    cursor_codec = tokens.cursor_codec()

    @app.exception_handler(StarletteHTTPException)
    async def canonical_agent_http_error(
        request: Request, error: StarletteHTTPException
    ) -> Response:
        if not request.url.path.startswith("/agent/v1/"):
            return await http_exception_handler(request, error)
        return Response(
            content=canonical_message({"detail": jsonable_encoder(error.detail)}),
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
                content=canonical_message({"detail": "login request is invalid"}),
                status_code=422,
                media_type="application/json",
            )
        if request.url.path.startswith("/api/v1/catalog/"):
            return Response(
                content=canonical_message(
                    {
                        "code": "catalog.invalid_request",
                        "detail": "catalog request is invalid",
                        "request_id": request.state.request_id,
                    }
                ),
                status_code=422,
                media_type="application/json",
            )
        if not request.url.path.startswith("/agent/v1/"):
            return await request_validation_exception_handler(request, error)
        return Response(
            content=canonical_message({"detail": jsonable_encoder(error.errors())}),
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
                content=canonical_message({"detail": "telemetry request is invalid"}),
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
            else 1_048_576
        )
        if telemetry_ingest:
            response = await call_next(request)
        elif (
            length and int(length) > maximum and request.url.path != "/agent/v1/enroll"
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
                except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey):
                    invalid_login_document = True
            if body_too_large:
                response = Response(status_code=413)
            elif invalid_login_document:
                response = Response(
                    content=canonical_message({"detail": "login request is invalid"}),
                    status_code=422,
                    media_type="application/json",
                )
            else:
                response = await call_next(request)
        response.headers["x-request-id"] = request_id
        response.headers["x-content-type-options"] = "nosniff"
        if request.url.path.startswith("/api/v1/auth/"):
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

    def browser_session_actor(request: Request) -> Actor:
        encoded = request.cookies.get("vonk_session", "")
        if not encoded:
            raise HTTPException(status_code=401, detail="authentication required")
        if browser_auth is None:
            raise HTTPException(status_code=401, detail="authentication failed")
        try:
            return browser_auth.resolve(encoded).actor
        except BrowserAuthenticationError:
            raise HTTPException(
                status_code=401, detail="authentication failed"
            ) from None

    install_agent_routes(
        app,
        actor_dependency=actor,
        audits=audits,
        services=agent,
        upgrades=agent_upgrades,
        enrollment_rate_limiter=enrollment_rate_limiter,
    )
    if worker_authority is not None:
        from .worker_authority import install_worker_authority_routes

        install_worker_authority_routes(
            app,
            worker_authority,
            token=worker_api_token,
        )
    authenticated_actor = Depends(actor)
    authenticated_browser_actor = Depends(browser_session_actor)

    if browser_auth is not None:
        from .auth_api import install_auth_routes

        install_auth_routes(app, browser_auth, audits, authenticated_actor)

    install_catalog_routes(
        app,
        actor_dependency=authenticated_actor,
        audits=audits,
        service=catalog,
        global_catalog=global_catalog,
        recipe_library=recipe_library,
        managed_sync=managed_catalog_sync,
    )
    install_library_routes(
        app,
        actor_dependency=authenticated_actor,
        projection=library_projection,
    )
    install_library_placement_routes(
        app,
        actor_dependency=authenticated_actor,
        service=library_placements,
        audits=audits,
    )
    install_fleet_profile_routes(
        app,
        actor_dependency=authenticated_actor,
        profiles=fleet_profiles,
        audits=audits,
    )
    install_workload_run_routes(
        app, actor_dependency=authenticated_actor, audits=audits, workflow=workload_run
    )
    install_recipe_operation_routes(
        app,
        actor_dependency=authenticated_actor,
        audits=audits,
        service=recipe_operations,
    )
    install_run_switch_routes(
        app,
        actor_dependency=authenticated_actor,
        audits=audits,
        service=run_switch_operations,
    )
    install_artifact_job_routes(
        app,
        actor_dependency=authenticated_actor,
        service=artifact_jobs,
    )
    install_model_cache_routes(
        app,
        actor_dependency=authenticated_actor,
        service=model_cache,
        audits=audits,
        cursors=cursor_codec,
    )

    @app.get("/api/v1/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/readyz")
    def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
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
        "/api/v1/fleet",
        response_model=FleetSnapshot,
        responses=bounded_error_responses(401, 503),
        operation_id="getFleetStatus",
    )
    def fleet_view(_actor: Actor = authenticated_actor) -> FleetSnapshot:
        if fleet_projection is None:
            raise HTTPException(status_code=503, detail="Fleet projection unavailable")
        try:
            return fleet_projection.read()
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet projection unavailable"
            ) from None

    @app.get(
        "/api/v1/fleet/stream",
        response_class=StreamingResponse,
        responses={
            200: {
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
                "description": "Durable Fleet event stream",
            },
            **bounded_error_responses(400, 401, 503),
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
        _actor: Actor = authenticated_browser_actor,
    ) -> StreamingResponse:
        try:
            last_event_id = parse_last_event_id(
                request.headers.getlist("last-event-id")
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        if fleet_stream is None:
            raise HTTPException(status_code=503, detail="Fleet stream unavailable")
        return StreamingResponse(
            fleet_stream.events(last_event_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get(
        "/api/v1/nodes/status",
        response_model=FleetStatusResponse,
        responses=bounded_error_responses(401),
        operation_id="getNodeStatuses",
        summary="Read explicit node health-probe evidence",
        description=(
            "Returns the legacy node health-probe projection. Its stale fields refer "
            "only to explicit node.probe compute-gate evidence, not aggregate Fleet "
            "readiness. Use /api/v1/fleet for live connection, inventory, and "
            "telemetry readiness."
        ),
    )
    def node_status_view(_actor: Actor = authenticated_actor) -> FleetStatusResponse:
        return fleet_response(fleet())

    @app.patch(
        "/api/v1/nodes/{node_id}/profile",
        response_model=FleetNodeIdentity,
        responses=bounded_error_responses(401, 403, 404, 422, 503),
        operation_id="updateNodeProfile",
    )
    def update_node_profile(
        node_id: Annotated[str, ApiPath(pattern=r"^spk_[0-9a-f]{32}$")],
        body: NodeProfileUpdateRequest,
        request: Request,
        authenticated: Actor = authenticated_actor,
    ) -> FleetNodeIdentity:
        route = "/api/v1/nodes/{node_id}/profile"
        require_mutation_role(authenticated, route, "PATCH")
        if fleet_projection is None:
            raise HTTPException(status_code=503, detail="Fleet projection unavailable")
        try:
            identity = fleet_projection.update_display_name(node_id, body.display_name)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet node not found"
            ) from None
        except (OSError, RuntimeError, SQLAlchemyError, TypeError):
            raise HTTPException(
                status_code=503, detail="Fleet profile update unavailable"
            ) from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                "fleet.node.rename",
                None,
                (node_id,),
            )
        )
        return identity

    @app.get(
        "/api/v1/nodes/{node_id}/telemetry",
        response_model=TelemetryHistoryResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getNodeTelemetryHistory",
    )
    def node_telemetry_history(
        node_id: Annotated[str, ApiPath(pattern=r"^spk_[0-9a-f]{32}$")],
        start: Annotated[datetime, Query()],
        end: Annotated[datetime, Query()],
        resolution: Annotated[TelemetryResolution, Query()],
        maximum_points: Annotated[int, Query(ge=1, le=3_000)] = 1_500,
        _actor: Actor = authenticated_actor,
    ) -> TelemetryHistoryResponse:
        if fleet_projection is None:
            raise HTTPException(status_code=503, detail="Fleet projection unavailable")
        try:
            return fleet_projection.telemetry_history(
                node_id,
                start=start,
                end=end,
                maximum_points=maximum_points,
                resolution=resolution,
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet node not found"
            ) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except (OSError, RuntimeError, TypeError):
            raise HTTPException(
                status_code=503, detail="Telemetry history unavailable"
            ) from None

    @app.get(
        "/api/v1/endpoints/{alias}",
        response_model=EndpointResponse,
        responses=bounded_error_responses(401, 404, 503),
        operation_id="getPublishedEndpoint",
    )
    def endpoint_view(
        alias: str = ApiPath(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$"),
        _actor: Actor = authenticated_actor,
    ) -> Mapping[str, object]:
        if operations is None:
            raise HTTPException(
                status_code=503, detail="endpoint publication unavailable"
            )
        try:
            return operations.endpoint(alias)
        except KeyError:
            raise HTTPException(status_code=404, detail="endpoint not found") from None
        except RuntimeError:
            raise HTTPException(
                status_code=503, detail="endpoint publication unavailable"
            ) from None

    @app.get(
        "/api/v1/agents",
        response_model=AgentsResponse,
        responses=bounded_error_responses(401, 503),
        operation_id="listAgents",
    )
    def agent_list(_actor: Actor = authenticated_actor) -> dict[str, object]:
        if operations is None:
            raise HTTPException(status_code=503, detail="agent projection unavailable")
        try:
            return {"agents": list(operations.agents())}
        except RuntimeError:
            raise HTTPException(
                status_code=503, detail="agent projection unavailable"
            ) from None

    @app.get("/api/v1/authority")
    def authority_view(
        revision: str | None = None, _actor: Actor = authenticated_actor
    ) -> dict[str, object]:
        if admin is None:
            raise HTTPException(status_code=503, detail="authority unavailable")
        resolved = revision or admin.authority.head()
        snapshot = admin.authority.inspect(resolved)
        return {
            "revision": snapshot.revision,
            "documents": dict(snapshot.documents),
            "dependencies": dict(snapshot.dependencies),
        }

    @app.post("/api/v1/proposals")
    def proposal_preview(
        body: ProposalRequest, authenticated: Actor = authenticated_actor
    ) -> dict[str, object]:
        require_mutation_role(authenticated, "/api/v1/proposals")
        if admin is None:
            raise HTTPException(status_code=503, detail="authority unavailable")
        preview = admin.proposals.preview(
            authenticated.subject,
            body.base_revision,
            [AuthorityChange(change.path, change.document) for change in body.changes],
        )
        return {
            "base_revision": preview.base_revision,
            "digest": preview.digest,
            "patch": base64.b64encode(preview.patch).decode(),
            "affected_documents": list(preview.affected_documents),
            "validation_results": list(preview.validation_results),
        }

    @app.post("/api/v1/changes", status_code=status.HTTP_202_ACCEPTED)
    def submit_change(
        body: ChangeRequest,
        request: Request,
        authenticated: Actor = authenticated_actor,
    ) -> dict[str, object]:
        require_mutation_role(authenticated, "/api/v1/changes")
        if admin is None or admin.changes is None:
            raise HTTPException(status_code=503, detail="change submission unavailable")
        result = admin.changes.submit(
            body.proposal_digest, authenticated.subject, request.state.request_id
        )
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                "authority.change.submit",
                None,
                (),
            )
        )
        return dict(result)

    @app.post(
        "/api/v1/jobs",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        include_in_schema=False,
    )
    def enqueue(
        body: JobRequest, request: Request, authenticated: Actor = authenticated_actor
    ) -> JobResponse:
        require_mutation_role(authenticated, "/api/v1/jobs")
        if not generic_jobs_enabled:
            raise HTTPException(
                status_code=422,
                detail="generic jobs are disabled; use an immutable reconciliation plan",
            )
        if body.kind == "reconcile":
            raise HTTPException(
                status_code=422,
                detail="reconciliations require an accepted immutable plan",
            )
        job = jobs.enqueue(
            body.kind,
            authenticated.subject,
            body.authority_revision,
            body.targets,
            body.payload,
            request_id=request.state.request_id,
        )
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                f"job.enqueue:{body.kind}",
                body.authority_revision,
                tuple(body.targets),
            )
        )
        return JobResponse(id=str(job.id), state=str(job.state))

    @app.get(
        "/api/v1/jobs",
        response_model=JobsResponse,
        responses=bounded_error_responses(401, 422),
        operation_id="listJobs",
    )
    def jobs_view(
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=20, ge=1, le=100),
        job_status: str | None = Query(
            default=None, alias="status", pattern=r"^[a-z][a-z0-9-]{0,31}$"
        ),
        target: str | None = Query(default=None, pattern=r"^spk_[0-9a-f]{32}$"),
        _actor: Actor = authenticated_actor,
    ) -> dict[str, object]:
        try:
            page, next_cursor, total = jobs.list_page(
                limit=limit,
                cursor=cursor,
                status=job_status,
                target=target,
            )
        except ValueError:
            raise HTTPException(
                status_code=422, detail="job cursor is invalid"
            ) from None
        return {
            "jobs": [
                {
                    "id": str(job.id),
                    "state": str(job.state),
                    "kind": str(job.kind),
                    "created_at": (
                        job.created_at.replace(tzinfo=UTC)
                        if job.created_at.tzinfo is None
                        else job.created_at.astimezone(UTC)
                    ),
                }
                for job in page
            ],
            "next_cursor": next_cursor,
            "total": total,
        }

    @app.get(
        "/api/v1/operations",
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
        _actor: Actor = authenticated_actor,
    ) -> OperationsResponse:
        if operations is None:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            )
        try:
            page = _global_list_operations(
                operations, cursor, limit, operation_state, node_id
            )
        except ValueError:
            raise HTTPException(
                status_code=422, detail="operation cursor is invalid"
            ) from None
        except RuntimeError:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None
        return OperationsResponse(
            operations=[operation_detail_response(item) for item in page.items],
            next_cursor=page.next_cursor,
            total=page.total,
        )

    @app.get(
        "/api/v1/operations/{operation_id}",
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
        except RuntimeError:
            raise HTTPException(
                status_code=503, detail="operation projection unavailable"
            ) from None
        return operation_detail_response(item)

    @app.get("/api/v1/audit")
    def audit_view(_actor: Actor = authenticated_actor) -> dict[str, object]:
        return {
            "events": [
                {
                    "request_id": event.request_id,
                    "actor": event.actor,
                    "action": event.action,
                    "authority_revision": event.authority_revision,
                    "targets": list(event.targets),
                    "occurred_at": event.occurred_at.isoformat()
                    if event.occurred_at is not None
                    else None,
                }
                for event in audits.list()
            ]
        }

    @app.get("/api/v1/identity-history", operation_id="listIdentityHistory")
    def identity_history_view(_actor: Actor = authenticated_actor) -> dict[str, object]:
        return {
            "identities": [
                {
                    "node_id": record.node_id,
                    "agent_state": record.agent_state,
                    "certificate_serial": record.certificate_serial,
                    "certificate_fingerprint": record.certificate_fingerprint,
                    "certificate_generation": record.certificate_generation,
                    "enrolled_at": record.enrolled_at,
                    "revoked_at": record.revoked_at,
                }
                for record in audits.identity_history()
            ]
        }

    @app.get(
        "/api/v1/jobs/{job_id}",
        response_model=JobDetailResponse,
        responses=bounded_error_responses(401, 404, 422),
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
            )
        except ValueError:
            raise HTTPException(
                status_code=422, detail="job cursor is invalid"
            ) from None

    @app.post(
        "/api/v1/jobs/{job_id}/resume",
        response_model=JobResumeResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="resumeJob",
    )
    def resume_job(
        request: Request,
        job_id: str,
        body: None = Body(default=None),
        authenticated: Actor = authenticated_actor,
    ) -> JobResumeResponse:
        del body
        route = "/api/v1/jobs/{job_id}/resume"
        require_mutation_role(authenticated, route)
        if operations is None:
            raise HTTPException(status_code=503, detail="job resume unavailable")
        try:
            operations.resume_job(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found") from None
        except ValueError:
            raise HTTPException(
                status_code=409, detail="job is not waiting for operator"
            ) from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                "job.resume",
                None,
                (),
            )
        )
        return JobResumeResponse(id=job_id, state="queued")

    @app.get(
        "/api/v1/jobs/{job_id}/logs",
        response_model=JobLogsResponse,
        responses=bounded_error_responses(401, 403, 404, 503),
        operation_id="listJobLogs",
    )
    def job_log_list(
        job_id: str, authenticated: Actor = authenticated_actor
    ) -> dict[str, object]:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")
        if job_logs is None:
            raise HTTPException(status_code=503, detail="job logs unavailable")
        try:
            jobs.get(job_id)
            return {"job_id": job_id, "digests": list(job_logs.list(job_id))}
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="job not found") from None

    @app.get(
        "/api/v1/jobs/{job_id}/logs/{digest}",
        responses=bounded_error_responses(401, 403, 404, 503),
    )
    def job_log_content(
        job_id: str, digest: str, authenticated: Actor = authenticated_actor
    ) -> Response:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")
        if job_logs is None:
            raise HTTPException(status_code=503, detail="job logs unavailable")
        try:
            jobs.get(job_id)
            return Response(
                job_logs.read(job_id, digest), media_type="text/plain; charset=utf-8"
            )
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="job log not found") from None

    return app


def production_app() -> FastAPI:
    from sqlalchemy import func, select

    from .agent_reconciliation import (
        bind_reconciliation_result_consumer,
        load_reconciliation_authority_input,
    )
    from .agent_upgrades import AgentUpgradeService
    from .artifact_sizes import DeclaredArtifactSizeResolver
    from .audit import SqlAuditStore
    from .catalog_seeds import seed_builtin_harnesses
    from .dashboard import DashboardService
    from .database_authority import (
        DatabaseAuthorityService,
        DatabaseChangeService,
        DatabaseProposalService,
    )
    from .db import build_engine, session_factory
    from .fleet_events import FleetEventRepository
    from .fleet_projection import FleetProjection
    from .fleet_stream import FleetStream
    from .install_admission import InstallAdmissionService
    from .jobs import JobService
    from .library_projection import LibraryProjection
    from .logging import DatabaseJobLogStore
    from .metrics import MetricsRegistry, OperationalMetricsCollector
    from .model_cache import ModelCacheService
    from .models import Job
    from .operation_api import durable_operation_services
    from .presence import ManagementAddressPolicy
    from .recipe_routes import AtomicRecipeRoutePublisher, RecipeRouteService
    from .route_runtime import AtomicRouteBundlePublisher, FileSupervisorAcknowledger
    from .run_admission import RunAdmissionService
    from .settings import Settings
    from .telemetry import TelemetryRepository
    from .worker_authority import WorkerAuthorityService

    settings = Settings.from_env_and_secrets()
    sessions = session_factory(build_engine(settings.database_url))
    clock = lambda: datetime.now(UTC)
    with sessions.begin() as session:
        seed_builtin_harnesses(session, clock())

    token_codec = TokenCodec(settings.token_signing_key)
    cursor_codec = token_codec.cursor_codec()
    job_service = JobService(sessions, clock=clock, cursors=cursor_codec)
    authority = DatabaseAuthorityService(sessions, clock=clock)
    authority.ensure_initialized()
    proposals = DatabaseProposalService(authority)
    changes = DatabaseChangeService(authority, proposals)
    database_bundles = DatabaseSourceBundleStore(sessions)
    dashboard = DashboardService(authority, sessions)
    telemetry_repository = TelemetryRepository(sessions, clock=clock)
    fleet_event_repository = FleetEventRepository(sessions, clock=clock)
    visual_fleet = FleetProjection(
        authority,
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
    visual_library = LibraryProjection(
        sessions,
        cursors=cursor_codec,
        clock=clock,
        inventory_fresh_seconds=300,
        telemetry_live_seconds=6,
        telemetry_delayed_seconds=20,
        disk_floor_bytes=10_000_000_000,
        memory_floor_bytes=4_000_000_000,
    )
    metrics = MetricsRegistry()
    operational_metrics = OperationalMetricsCollector(
        metrics,
        sessions,
        clock=clock,
    )
    revision_eligible = lambda revision: revision == authority.head()
    current_revision = authority.head
    agent_services = build_agent_services(
        settings,
        sessions,
        clock,
        revision_eligible=revision_eligible,
        current_revision=current_revision,
    )

    def reconciliation_authority_input(
        reconciliation_id: str,
    ) -> tuple[str, str, tuple[Any, ...], str]:
        def endpoint(session: Any, node_id: str) -> tuple[str, Any]:
            observation = agent_services.presence.latest_in_session(
                session,
                node_id,
                maximum_age_seconds=300,
            )
            return observation.address, observation.observed_at

        with sessions() as session:
            snapshot = load_reconciliation_authority_input(
                session,
                reconciliation_id,
                endpoint,
            )
        return (
            snapshot.authority_revision,
            snapshot.plan_digest,
            snapshot.routes,
            snapshot.fleet_evidence_digest,
        )

    worker_authority = WorkerAuthorityService(
        current_revision=current_revision,
        revision_eligible=revision_eligible,
        reconciliation_input=reconciliation_authority_input,
        current_fleet_evidence=lambda: (
            fleet_response(dashboard.fleet()).evidence_digest
        ),
    )
    recipe_route_runtime = AtomicRouteBundlePublisher(
        Path("/routes"),
        management_policy=ManagementAddressPolicy.parse(
            settings.management_cidrs,
            forbidden_cidrs=settings.direct_fabric_cidrs,
        ),
        clock=clock,
        maximum_lease_seconds=300,
        await_supervisor_ack=FileSupervisorAcknowledger(
            Path("/supervisor/ack.json"), clock=clock
        ),
    )
    recipe_routes = RecipeRouteService(
        sessions,
        publisher=AtomicRecipeRoutePublisher(recipe_route_runtime, clock=clock),
        management_policy=ManagementAddressPolicy.parse(
            settings.management_cidrs,
            forbidden_cidrs=settings.direct_fabric_cidrs,
        ),
        clock=clock,
        maximum_age_seconds=30,
    )
    recipe_operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(
            sessions,
            sizes=DeclaredArtifactSizeResolver(),
            inventory_max_age=300,
            disk_floor_bytes=10_000_000_000,
            operator_jurisdiction=settings.operator_jurisdiction,
        ),
        run_admission=RunAdmissionService(
            sessions,
            inventory_max_age=300,
            memory_floor_bytes=4_000_000_000,
            operator_jurisdiction=settings.operator_jurisdiction,
        ),
        agent_jobs=agent_services.operations,
        clock=clock,
        route_publications=recipe_routes,
        builds=RecipeBuildService(
            sessions,
            bundles=database_bundles,
            inventory_max_age=300,
        ),
        mappings=ClusterMappingService(sessions),
    )
    run_switch_operations = RunSwitchOperationService(
        sessions,
        lifecycle=recipe_operations,
        clock=clock,
        mappings=ClusterMappingService(sessions),
    )
    artifact_jobs = ArtifactJobService(
        sessions,
        recipe_operations=recipe_operations,
        blob_store=ArtifactBlobStore(
            settings.state_path / "artifact-jobs" / "blobs",
            max_stored_bytes=settings.artifact_job_storage_max_bytes,
        ),
        clock=clock,
        retention_seconds=settings.artifact_job_retention_seconds,
    )
    artifact_jobs.reconcile_storage()
    from .fleet_profiles import FleetProfileService

    fleet_profiles = FleetProfileService(
        sessions,
        clock=clock,
        recipe_operations=recipe_operations,
    )
    from .library_placements import LibraryPlacementService

    library_placements = LibraryPlacementService(visual_library, fleet_profiles)
    agent_upgrades = AgentUpgradeService(
        sessions,
        agent_services.operations,
        clock=clock,
        current_revision=current_revision,
        channel=(
            "dev"
            if agent_services.bootstrap is not None
            and "/dev/" in agent_services.bootstrap.installer_url
            else "stable"
        ),
        release_api_url=settings.agent_release_api_url,
    )

    def consume_agent_result(session, operation, attempt, message) -> None:
        artifact_jobs.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)
        agent_upgrades.consume_agent_result(session, operation, attempt, message)

    bind_reconciliation_result_consumer(
        sessions,
        operations=agent_services.operations,
        presence=agent_services.presence,
        clock=clock,
        revision_eligible=revision_eligible,
        current_revision=current_revision,
        additional_result_consumer=consume_agent_result,
    )

    def refresh_metrics() -> None:
        operational_metrics.refresh()
        fleet_state = dashboard.fleet()
        refresh_fleet_metrics(metrics, fleet_state)
        with sessions() as session:
            for kind, state, count in session.execute(
                select(Job.kind, Job.state, func.count()).group_by(Job.kind, Job.state)
            ):
                metrics.set_job_count(kind, state, count)
        backup_marker = settings.state_path / "last-successful-backup.epoch"
        if backup_marker.is_file() and not backup_marker.is_symlink():
            try:
                completed_at = int(backup_marker.read_text().strip())
                metrics.set_backup_age(max(0, int(time.time()) - completed_at))
            except (OSError, ValueError):
                pass

    global_catalog = GlobalCatalogClient(settings.global_catalog_url)
    recipe_library = RecipeLibraryClient(base_url=settings.recipe_library_api_url)
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
    model_cache = ModelCacheService(
        sessions,
        settings.model_cache_root,
        reserve_bytes=settings.model_cache_reserve_bytes,
        clock=clock,
    )
    model_cache.resume_operations()
    audits_store = SqlAuditStore(sessions, clock)
    app = create_app(
        jobs=job_service,
        tokens=token_codec,
        audits=audits_store,
        fleet=dashboard.fleet,
        fleet_projection=visual_fleet,
        fleet_stream=visual_fleet_stream,
        library_projection=visual_library,
        admin=AdminServices(
            authority,
            proposals,
            changes,
        ),
        metrics=metrics,
        metrics_token=settings.metrics_token,
        metrics_refresh=refresh_metrics,
        job_logs=DatabaseJobLogStore(sessions, clock=clock),
        agent=(agent_services if settings.agent_runtime == "enabled" else None),
        trusted_agent_proxy_auth=settings.agent_proxy_auth,
        worker_authority=(
            worker_authority if settings.agent_runtime == "enabled" else None
        ),
        worker_api_token=(
            settings.worker_api_token if settings.agent_runtime == "enabled" else b""
        ),
        operations=register_model_cache_operation_provider(
            durable_operation_services(
                sessions,
                Path("/routes"),
                clock=clock,
                cursors=cursor_codec,
                resume_agent_upgrade=agent_upgrades.resume,
                operation_providers=(fleet_profiles.operation_provider(),),
            ),
            model_cache,
        ),
        catalog=catalog_service,
        global_catalog=global_catalog,
        recipe_library=recipe_library,
        managed_catalog_sync=managed_catalog_sync,
        workload_run=WorkloadRunWorkflow(
            sessions,
            clock=clock,
            bundles=database_bundles,
            recipe_resolver=lambda document, actor: (
                catalog_service.resolve_recipe_revision(document, actor=actor)
            ),
        ),
        browser_auth=BrowserAuthService(
            sessions,
            token_signing_key=settings.token_signing_key,
            clock=clock,
        ),
        recipe_operations=recipe_operations,
        run_switch_operations=run_switch_operations,
        artifact_jobs=artifact_jobs,
        fleet_profiles=fleet_profiles,
        library_placements=library_placements,
        agent_upgrades=agent_upgrades,
        model_cache=model_cache,
    )
    web_root = Path(__file__).resolve().parent / "web"
    if web_root.is_dir():
        app.mount("/", SpaFiles(directory=web_root, html=True), name="admin-web")

    automatic_sync_task: asyncio.Task[None] | None = None
    automatic_sync_stop = asyncio.Event()

    async def run_automatic_catalog_sync() -> None:
        # Let migrations, health checks, and the local relay settle before the
        # first network-bound refresh. The durable ledger remains authoritative.
        try:
            await asyncio.wait_for(automatic_sync_stop.wait(), timeout=10)
            return
        except TimeoutError:
            pass
        while not automatic_sync_stop.is_set():
            try:
                await asyncio.to_thread(managed_catalog_sync.automatic)
            except (
                CatalogError,
                CatalogSyncError,
                RecipeLibraryError,
                OSError,
            ) as error:
                _LOGGER.warning(
                    "automatic managed recipe catalog sync failed: %s",
                    type(error).__name__,
                )
            try:
                await asyncio.wait_for(
                    automatic_sync_stop.wait(),
                    timeout=settings.recipe_library_sync_interval_seconds,
                )
            except TimeoutError:
                continue

    @app.on_event("startup")
    async def start_automatic_catalog_sync() -> None:
        nonlocal automatic_sync_task
        automatic_sync_task = asyncio.create_task(run_automatic_catalog_sync())

    @app.on_event("shutdown")
    async def close_global_catalog() -> None:
        automatic_sync_stop.set()
        if automatic_sync_task is not None:
            await automatic_sync_task
        global_catalog.close()
        recipe_library.close()
        agent_upgrades.close()

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(production_app(), host="0.0.0.0", port=8000, access_log=False)
