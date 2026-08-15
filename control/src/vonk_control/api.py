"""Versioned authenticated control API."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
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
from .audit import AuditRecord
from .auth import (
    MUTATION_ROLES,
    Actor,
    AuthError,
    TokenCodec,
    TrustedProxyAgentIdentityMiddleware,
)
from .browser_auth import BrowserAuthenticationError, BrowserAuthService
from .catalog_api import install_catalog_routes
from .catalog_service import CatalogService
from .cluster_mappings import ClusterMappingService
from .fleet_projection import FleetSnapshot, TelemetryHistoryResponse
from .fleet_stream import parse_last_event_id
from .global_catalog import GlobalCatalogClient
from .library_api import install_library_routes
from .metrics import MetricsRegistry
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
    OperationPage,
    ReconciliationAcceptedResponse,
    ReconciliationPlanResponse,
    bounded_error_responses,
    decode_offset,
    fleet_response,
    job_response,
    plan_response,
)
from .package_api import PackageApiServices, install_package_routes
from .proposals import DocumentChange
from .recipe_api import install_recipe_operation_routes
from .recipe_builds import RecipeBuildService
from .recipe_operations import RecipeOperationService
from .reconcile import IneligibleCommit, StaleFleetEvidence
from .repository import RepositoryPolicyError
from .settings import StartupMode
from .source_bundles import SourceBundleStore
from .telemetry import TelemetryResolution
from .workload_run_api import install_workload_run_routes
from .workload_run_workflow import WorkloadRunWorkflow

_CONTROL_GENERATION = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_CONTROL_OPERATION = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_CONTROL_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONTROL_IMAGE = re.compile(r"[^\s]{1,1900}@sha256:[0-9a-f]{64}\Z")
_CONTROL_START_NONCE = re.compile(r"[0-9a-f]{64}\Z")
_RECIPE_IMAGE_UPLOAD = re.compile(
    r"/agent/v1/recipe-builds/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/image\Z"
)
_MAX_IDENTITY_PROJECTION_BYTES = 64 * 1024
_LOGIN_PATH = "/api/v1/auth/login"
_TELEMETRY_PATH = "/agent/v1/telemetry"
_MAX_TELEMETRY_BODY_BYTES = 64 * 1024


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


class GenerationReadinessError(RuntimeError):
    """A control generation has not established exact readiness."""


@dataclass(frozen=True)
class GenerationProcessIdentity:
    """Immutable identity injected into one control process at startup."""

    startup_mode: StartupMode
    operation_id: str | None
    generation_id: str
    release_digest: str
    build_digest: str
    platform_version: str
    process_image: str
    database_revision: str
    start_nonce: str

    def __post_init__(self) -> None:
        if not isinstance(self.startup_mode, StartupMode):
            raise TypeError("startup mode is invalid")
        if _CONTROL_GENERATION.fullmatch(self.generation_id) is None:
            raise ValueError("generation ID is invalid")
        if _CONTROL_GENERATION.fullmatch(self.database_revision) is None:
            raise ValueError("database revision is invalid")
        if _CONTROL_DIGEST.fullmatch(self.release_digest) is None:
            raise ValueError("release digest is invalid")
        if _CONTROL_DIGEST.fullmatch(self.build_digest) is None:
            raise ValueError("build digest is invalid")
        if (
            re.fullmatch(
                r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
                self.platform_version,
            )
            is None
        ):
            raise ValueError("platform version is invalid")
        if _CONTROL_IMAGE.fullmatch(self.process_image) is None:
            raise ValueError("process image is invalid")
        if _CONTROL_START_NONCE.fullmatch(self.start_nonce) is None:
            raise ValueError("process start nonce is invalid")
        if self.startup_mode is StartupMode.PRESELECTION:
            if (
                self.operation_id is None
                or _CONTROL_OPERATION.fullmatch(self.operation_id) is None
            ):
                raise ValueError("preselection operation ID is invalid")
        elif self.operation_id is not None:
            raise ValueError("selected identity cannot carry an operation ID")


class IdentityProjectionSource(Protocol):
    """Narrow read-only projection interface supplied by host state."""

    def load_candidate(self, operation_id: str) -> object | None: ...

    def load_active(self) -> object | None: ...


class DirectoryIdentityProjectionSource:
    """Safely reopen immutable projections from a read-only directory mount."""

    def __init__(self, identity_root: Path, *, expected_owner: int = 0) -> None:
        self._root = Path(identity_root)
        if not self._root.is_absolute():
            raise GenerationReadinessError("identity projection root must be absolute")
        self._expected_owner = expected_owner

    def load_candidate(self, operation_id: str) -> Mapping[str, object] | None:
        if _CONTROL_OPERATION.fullmatch(operation_id) is None:
            raise GenerationReadinessError("candidate operation ID is invalid")
        return self._read(("candidates",), f"{operation_id}.json")

    def load_active(self) -> Mapping[str, object] | None:
        return self._read((), "active.json")

    def _read(
        self, directory_parts: tuple[str, ...], filename: str
    ) -> Mapping[str, object] | None:
        descriptors: list[int] = []
        try:
            try:
                descriptor = os.open(
                    self._root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
            except FileNotFoundError:
                return None
            except OSError as error:
                raise GenerationReadinessError(
                    "identity projection root is unsafe"
                ) from error
            descriptors.append(descriptor)
            self._require_directory(descriptor)
            for part in directory_parts:
                try:
                    descriptor = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    return None
                except OSError as error:
                    raise GenerationReadinessError(
                        "identity projection directory is unsafe"
                    ) from error
                descriptors.append(descriptor)
                self._require_directory(descriptor)
            try:
                projection_fd = os.open(
                    filename,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                return None
            except OSError as error:
                raise GenerationReadinessError(
                    "identity projection file is unsafe"
                ) from error
            descriptors.append(projection_fd)
            before = os.fstat(projection_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o444
                or before.st_uid != self._expected_owner
                or before.st_nlink != 1
                or not 0 < before.st_size <= _MAX_IDENTITY_PROJECTION_BYTES
            ):
                raise GenerationReadinessError("identity projection file is unsafe")
            chunks: list[bytes] = []
            remaining = _MAX_IDENTITY_PROJECTION_BYTES + 1
            while remaining:
                chunk = os.read(projection_fd, min(remaining, 16 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(projection_fd)
            stable_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(
                getattr(before, field) != getattr(after, field)
                for field in stable_fields
            ):
                raise GenerationReadinessError(
                    "identity projection changed while being read"
                )
            if len(raw) > _MAX_IDENTITY_PROJECTION_BYTES:
                raise GenerationReadinessError("identity projection file is unsafe")
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise GenerationReadinessError(
                    "identity projection is invalid"
                ) from error
            if not isinstance(value, dict) or not all(
                isinstance(key, str) for key in value
            ):
                raise GenerationReadinessError("identity projection is invalid")
            canonical = (
                json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            if raw != canonical:
                raise GenerationReadinessError("identity projection is not canonical")
            self._require_exact_document(value)
            return value
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _require_directory(self, descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self._expected_owner
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise GenerationReadinessError("identity projection directory is unsafe")

    @staticmethod
    def _require_exact_document(document: Mapping[str, object]) -> None:
        from .host_state import (
            HostOperationPlan,
            HostStateConflict,
            SelectionReceipt,
        )

        kind = document.get("projection_kind")
        content = dict(document)
        content.pop("projection_kind", None)
        try:
            if kind == "candidate":
                HostOperationPlan.from_document(content)
                return
            if kind != "active":
                raise GenerationReadinessError("identity projection kind is invalid")
            if (
                set(document)
                != {
                    "generation_receipt_sha256",
                    "projection_kind",
                    "projection_sequence",
                    "schema_version",
                    "selection",
                    "selection_receipt_sha256",
                }
                or document.get("schema_version") != 1
            ):
                raise GenerationReadinessError("active projection is invalid")
            sequence = document.get("projection_sequence")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 1
            ):
                raise GenerationReadinessError("active projection is invalid")
            selection_document = document.get("selection")
            if not isinstance(selection_document, dict):
                raise GenerationReadinessError("active projection is invalid")
            receipt = SelectionReceipt.from_document(selection_document)
            canonical_generation = (
                json.dumps(
                    receipt.generation.document(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            canonical_selection = (
                json.dumps(
                    receipt.document(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            generation_sha256 = hashlib.sha256(canonical_generation).hexdigest()
            selection_sha256 = hashlib.sha256(canonical_selection).hexdigest()
            if not secrets.compare_digest(
                str(document.get("generation_receipt_sha256")),
                generation_sha256,
            ):
                raise GenerationReadinessError(
                    "active projection receipt digest does not match"
                )
            if not secrets.compare_digest(
                str(document.get("selection_receipt_sha256")),
                selection_sha256,
            ):
                raise GenerationReadinessError(
                    "active projection selection digest does not match"
                )
        except (HostStateConflict, TypeError, ValueError) as error:
            raise GenerationReadinessError(
                "identity projection fields are invalid"
            ) from error


class GenerationReadinessService:
    """Prove candidate identity or selected API plus one real worker DB loop."""

    def __init__(
        self,
        sessions: Any,
        identity: GenerationProcessIdentity,
        projections: IdentityProjectionSource,
        *,
        clock: Callable[[], datetime],
        database_revision: Callable[[], str],
        heartbeat_maximum_age_seconds: int = 15,
    ) -> None:
        if not 1 <= heartbeat_maximum_age_seconds <= 90:
            raise ValueError("heartbeat maximum age must be between one and 90 seconds")
        self.sessions = sessions
        self._identity = identity
        self._projections = projections
        self._clock = clock
        self._database_revision = database_revision
        self._maximum_age = heartbeat_maximum_age_seconds

    def candidate(self, generation_id: str, start_nonce: str) -> Mapping[str, object]:
        identity = self._require_call_identity(
            StartupMode.PRESELECTION, generation_id, start_nonce
        )
        assert identity.operation_id is not None
        projection = self._projections.load_candidate(identity.operation_id)
        if projection is None:
            raise GenerationReadinessError("candidate projection is unavailable")
        self._require_projection(
            projection,
            kind="candidate",
            operation_id=identity.operation_id,
        )
        self._require_database_revision()
        return {
            "build_digest": identity.build_digest,
            "database_revision": identity.database_revision,
            "generation_id": identity.generation_id,
            "mode": StartupMode.PRESELECTION.value,
            "operation_id": identity.operation_id,
            "release_digest": identity.release_digest,
            "start_nonce": identity.start_nonce,
            "status": "ready",
        }

    def selected(self, generation_id: str, start_nonce: str) -> Mapping[str, object]:
        from sqlalchemy import select

        from .models import ControlProcessHeartbeat

        identity = self._require_call_identity(
            StartupMode.SELECTED, generation_id, start_nonce
        )
        projection = self._projections.load_active()
        if projection is None:
            raise GenerationReadinessError("active projection is unavailable")
        self._require_projection(projection, kind="active", operation_id=None)
        self._require_database_revision()
        with self.sessions() as session:
            heartbeat = session.scalar(
                select(ControlProcessHeartbeat).where(
                    ControlProcessHeartbeat.process_kind == "worker",
                    ControlProcessHeartbeat.generation_id == identity.generation_id,
                    ControlProcessHeartbeat.release_digest == identity.release_digest,
                    ControlProcessHeartbeat.build_digest == identity.build_digest,
                    ControlProcessHeartbeat.start_nonce == identity.start_nonce,
                )
            )
        if heartbeat is None or heartbeat.loop_sequence < 1:
            raise GenerationReadinessError("worker heartbeat is unavailable")
        completed_at = _aware_utc(heartbeat.completed_at)
        now = _aware_utc(self._clock())
        age = (now - completed_at).total_seconds()
        if not 0 <= age <= self._maximum_age:
            raise GenerationReadinessError("worker heartbeat is stale")
        return {
            "build_digest": identity.build_digest,
            "database_revision": identity.database_revision,
            "generation_id": identity.generation_id,
            "mode": StartupMode.SELECTED.value,
            "release_digest": identity.release_digest,
            "start_nonce": identity.start_nonce,
            "status": "ready",
            "worker_loop_sequence": heartbeat.loop_sequence,
        }

    def _require_call_identity(
        self, mode: StartupMode, generation_id: str, start_nonce: str
    ) -> GenerationProcessIdentity:
        identity = self._identity
        if identity.startup_mode is not mode:
            raise GenerationReadinessError(f"{mode.value} mode is not active")
        if generation_id != identity.generation_id:
            raise GenerationReadinessError(
                "requested generation does not match process"
            )
        if start_nonce != identity.start_nonce:
            raise GenerationReadinessError(
                "requested start nonce does not match process"
            )
        return identity

    def _require_projection(
        self,
        projection: object,
        *,
        kind: str,
        operation_id: str | None,
    ) -> None:
        identity = self._identity
        actual_kind = _projection_field(projection, "projection_kind")
        if actual_kind != kind:
            raise GenerationReadinessError(f"{kind} projection kind is invalid")
        expected: tuple[tuple[str, object, str], ...] = (
            ("generation_id", identity.generation_id, "generation"),
            ("release_digest", identity.release_digest, "release"),
            ("build_digest", identity.build_digest, "build"),
            ("platform_version", identity.platform_version, "platform version"),
            ("api_image", identity.process_image, "image"),
            ("database_revision", identity.database_revision, "database revision"),
        )
        if operation_id is not None:
            expected += (("operation_id", operation_id, "operation"),)
        for field, wanted, label in expected:
            if _projection_field(projection, field) != wanted:
                raise GenerationReadinessError(
                    f"{kind} projection {label} does not match process"
                )
        if kind == "active":
            sequence = _projection_field(projection, "projection_sequence")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 1
            ):
                raise GenerationReadinessError("active projection sequence is invalid")

    def _require_database_revision(self) -> None:
        try:
            revision = self._database_revision()
        except Exception as error:
            raise GenerationReadinessError(
                "database revision is unavailable"
            ) from error
        if revision != self._identity.database_revision:
            raise GenerationReadinessError(
                "database revision does not match generation"
            )


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise GenerationReadinessError("worker heartbeat timestamp is invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    if value.utcoffset() is None:
        raise GenerationReadinessError("worker heartbeat timestamp is invalid")
    return value.astimezone(UTC)


def _projection_field(projection: object, field: str) -> object:
    if isinstance(projection, Mapping):
        if field in projection:
            return projection[field]
        selection = projection.get("selection")
        if not isinstance(selection, Mapping):
            return None
        if field in {"operation_id", "plan_digest", "previous_generation"}:
            return selection.get(field)
        generation = selection.get("generation")
        if isinstance(generation, Mapping):
            return generation.get(field)
        return None
    return getattr(projection, field, None)


def _generation_readiness_route(
    app: FastAPI, service: GenerationReadinessService, mode: StartupMode
) -> None:
    @app.get("/internal/v1/generation/readiness", include_in_schema=False)
    def generation_readiness() -> Mapping[str, object]:
        identity = service._identity
        try:
            if mode is StartupMode.PRESELECTION:
                return service.candidate(identity.generation_id, identity.start_nonce)
            return service.selected(identity.generation_id, identity.start_nonce)
        except GenerationReadinessError:
            raise HTTPException(
                status_code=503, detail="generation readiness unavailable"
            ) from None


def create_preselection_app(service: GenerationReadinessService) -> FastAPI:
    """Return an inert route-only candidate app with no production registration."""

    app = FastAPI(
        title="Vonk Forge Control Preselection",
        version="1.0",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    _generation_readiness_route(app, service, StartupMode.PRESELECTION)
    return app


def install_selected_generation_readiness(
    app: FastAPI, service: GenerationReadinessService
) -> None:
    """Install the host-only final gate on an already-selected production app."""

    _generation_readiness_route(app, service, StartupMode.SELECTED)


@dataclass(frozen=True)
class AdminServices:
    repository: Any
    proposals: Any
    changes: Any | None
    reconciler: Any | None
    cancellations: Any | None = None


def build_agent_services(
    settings: Any,
    sessions: Any,
    clock: Callable[[], Any],
    *,
    commit_eligible: Callable[[str], bool] | None = None,
    current_commit: Callable[[], str] | None = None,
) -> AgentApiServices:
    """Construct the fail-closed production agent runtime from one provider."""
    from .agent_jobs import AgentJobService
    from .enrollment import EnrollmentService
    from .host_helper_authority import (
        HostHelperGrantIssuer,
        HostRuntimeAuthorityService,
    )
    from .package_helper_authority import (
        PackageHelperAuthorityService,
        PackageHelperGrantIssuer,
        PackageObjectReceiptIssuer,
    )
    from .pki import BuiltinCertificateAuthority
    from .presence import AgentPresenceService, ManagementAddressPolicy
    from .step_ca import StepCertificateAuthority

    if settings.agent_runtime != "enabled":
        # Local development still needs the durable operation queue and fleet
        # presence service, but deliberately has no enrollment or certificate
        # authority.  Agent HTTP routes stay disabled by production_app.
        operations = AgentJobService(
            sessions,
            clock=clock,
            commit_eligible=commit_eligible,
            current_commit=current_commit,
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
            source_bundles=SourceBundleStore(
                getattr(settings, "state_path", settings.agent_artifact_root.parent)
                / "source-bundles"
            ),
            tuf_metadata_root=settings.agent_tuf_metadata_root,
            tuf_target_root=settings.agent_tuf_target_root,
            workload_tuf_metadata_root=settings.workload_tuf_metadata_root,
            workload_tuf_target_root=settings.workload_tuf_target_root,
        )

    if settings.agent_intermediate_certificate_path is None:
        raise RuntimeError("agent intermediate certificate path is unavailable")
    if settings.agent_ca_provider == "step-ca":
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
        )
        authority.check_health()
    elif settings.agent_ca_provider == "builtin":
        if settings.agent_intermediate_key_path is None:
            raise RuntimeError("built-in intermediate key path is unavailable")
        authority = BuiltinCertificateAuthority(
            settings.agent_intermediate_key_path,
            settings.agent_intermediate_certificate_path,
        )
    else:
        raise RuntimeError("agent CA provider is unavailable")
    tuf_metadata_root = getattr(
        settings,
        "agent_tuf_metadata_root",
        settings.agent_artifact_root.parent / "agent-tuf/metadata",
    )
    tuf_target_root = getattr(
        settings,
        "agent_tuf_target_root",
        settings.agent_artifact_root.parent / "agent-tuf/targets",
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
        tuf_metadata_root,
        tuf_target_root,
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
        commit_eligible=commit_eligible,
        current_commit=current_commit,
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
        raise RuntimeError("package helper authority keys are unavailable")
    if grant_key_path is not None and receipt_key_path is not None:
        helper_authority = PackageHelperAuthorityService(
            sessions,
            PackageHelperGrantIssuer.from_private_key_file(grant_key_path, clock=clock),
            PackageObjectReceiptIssuer.from_private_key_file(receipt_key_path),
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
        source_bundles=SourceBundleStore(
            getattr(settings, "state_path", settings.agent_artifact_root.parent)
            / "source-bundles"
        ),
        tuf_metadata_root=tuf_metadata_root,
        tuf_target_root=tuf_target_root,
        workload_tuf_metadata_root=workload_tuf_metadata_root,
        workload_tuf_target_root=workload_tuf_target_root,
        package_helper_authority=helper_authority,
        host_runtime_authority=host_runtime_authority,
        fabric_policy=(
            ManagementAddressPolicy.parse(
                settings.direct_fabric_cidrs,
                forbidden_cidrs=settings.management_cidrs,
            )
            if settings.direct_fabric_cidrs
            else None
        ),
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
        base_commit: str,
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
    base_commit: str = Field(min_length=1, max_length=128)
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
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    changes: list[ProposalChangeRequest] = Field(min_length=1, max_length=32)


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReconciliationPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")


class ReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fleet_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReconciliationCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1024)


class UpdatePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release: str = Field(
        pattern=(
            r"^platform/releases/"
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)/"
            r"[0-9a-f]{64}\.json$"
        ),
        max_length=256,
    )


class UpdateApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class UpdateApproveResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(
        default="administrator approved update recovery",
        min_length=1,
        max_length=1024,
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
    updates: Any | None = None,
    packages: PackageApiServices | None = None,
    catalog: CatalogService | None = None,
    global_catalog: Any | None = None,
    workload_run: WorkloadRunWorkflow | None = None,
    recipe_operations: RecipeOperationService | None = None,
    browser_auth: BrowserAuthService | None = None,
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
        if request.url.path.startswith(
            "/api/v1/packages/"
        ) or request.url.path.startswith("/api/v1/deployments"):
            return Response(
                content=canonical_message({"detail": "package request is invalid"}),
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
        telemetry_ingest = (
            request.method == "POST" and request.url.path == _TELEMETRY_PATH
        )
        maximum = MAX_RECIPE_IMAGE_BYTES if recipe_image_upload else 1_048_576
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

    def require_mutation_role(authenticated: Actor, path: str) -> None:
        if authenticated.role not in MUTATION_ROLES[("POST", path)]:
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
        enrollment_rate_limiter=enrollment_rate_limiter,
    )
    if worker_authority is not None:
        from .worker_authority import install_worker_authority_routes

        install_worker_authority_routes(
            app,
            worker_authority,
            token=worker_api_token,
            update_grants=updates,
        )
    authenticated_actor = Depends(actor)
    authenticated_browser_actor = Depends(browser_session_actor)

    if browser_auth is not None:
        from .auth_api import install_auth_routes

        install_auth_routes(app, browser_auth, audits, authenticated_actor)

    install_package_routes(
        app,
        actor_dependency=authenticated_actor,
        audits=audits,
        services=packages,
    )
    install_catalog_routes(
        app,
        actor_dependency=authenticated_actor,
        audits=audits,
        service=catalog,
        global_catalog=global_catalog,
    )
    install_library_routes(
        app,
        actor_dependency=authenticated_actor,
        projection=library_projection,
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
    )
    def node_status_view(_actor: Actor = authenticated_actor) -> FleetStatusResponse:
        return fleet_response(fleet())

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
        maximum_points: Annotated[int, Query(ge=1, le=1_500)] = 1_500,
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

    @app.get("/api/v1/repository")
    def repository_view(
        commit: str | None = None, _actor: Actor = authenticated_actor
    ) -> dict[str, object]:
        if admin is None:
            raise HTTPException(
                status_code=503, detail="repository administration unavailable"
            )
        resolved = commit or admin.repository.head()
        snapshot = admin.repository.inspect(resolved)
        return {
            "commit": snapshot.commit,
            "documents": dict(snapshot.documents),
            "dependencies": dict(snapshot.dependencies),
        }

    @app.get("/api/v1/documents")
    def document_view(
        commit: str | None = None,
        path: str | None = None,
        kind: str | None = None,
        _actor: Actor = authenticated_actor,
    ) -> dict[str, object]:
        if admin is None:
            raise HTTPException(
                status_code=503, detail="repository administration unavailable"
            )
        resolved = commit or admin.repository.head()
        if path is None:
            snapshot = admin.repository.inspect(resolved)
            prefixes = {
                "models": ("config/workloads/", "locks/", "manifests/"),
                "profiles": ("config/cluster-profiles/",),
            }
            selected = prefixes.get(kind or "", ())
            if not selected:
                raise HTTPException(status_code=400, detail="document kind is invalid")
            return {
                "commit": resolved,
                "documents": [
                    name for name in snapshot.documents if name.startswith(selected)
                ],
            }
        document = admin.repository.read_document(resolved, path)
        return {
            "commit": document.commit,
            "path": document.path,
            "sha256": document.sha256,
            "document": document.parsed,
        }

    @app.post("/api/v1/proposals")
    def proposal_preview(
        body: ProposalRequest, authenticated: Actor = authenticated_actor
    ) -> dict[str, object]:
        require_mutation_role(authenticated, "/api/v1/proposals")
        if admin is None:
            raise HTTPException(
                status_code=503, detail="repository administration unavailable"
            )
        preview = admin.proposals.preview(
            authenticated.subject,
            body.base_commit,
            [DocumentChange(change.path, change.document) for change in body.changes],
        )
        return {
            "base_commit": preview.base_commit,
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
                "repository.change.submit",
                None,
                (),
            )
        )
        return dict(result)

    def planned_response(commit: str, profile_id: str) -> ReconciliationPlanResponse:
        if admin is None or admin.reconciler is None:
            raise HTTPException(status_code=503, detail="reconciliation unavailable")
        try:
            evidence = fleet_response(fleet())
            planned = admin.reconciler.plan(
                commit,
                profile_id,
                fleet_evidence_digest=evidence.evidence_digest,
            )
            return plan_response(
                planned,
                fleet_evidence_digest=evidence.evidence_digest,
            )
        except IneligibleCommit:
            raise HTTPException(
                status_code=409, detail="commit is not eligible"
            ) from None
        except RepositoryPolicyError:
            raise HTTPException(
                status_code=422, detail="repository desired state is invalid"
            ) from None
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="desired state cannot be planned"
            ) from None

    @app.post(
        "/api/v1/reconciliations/plan",
        response_model=ReconciliationPlanResponse,
        responses=bounded_error_responses(401, 403, 409, 503),
        operation_id="planReconciliation",
    )
    def reconcile_plan(
        body: ReconciliationPlanRequest, authenticated: Actor = authenticated_actor
    ) -> ReconciliationPlanResponse:
        require_mutation_role(authenticated, "/api/v1/reconciliations/plan")
        return planned_response(body.commit, body.profile_id)

    @app.post(
        "/api/v1/profiles/{profile_id}/plan",
        response_model=ReconciliationPlanResponse,
        responses=bounded_error_responses(401, 403, 409, 503),
        operation_id="planProfileReconciliation",
    )
    def profile_reconcile_plan(
        profile_id: str = ApiPath(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$"),
        body: None = Body(default=None),
        authenticated: Actor = authenticated_actor,
    ) -> ReconciliationPlanResponse:
        del body
        require_mutation_role(authenticated, "/api/v1/profiles/{profile_id}/plan")
        if admin is None:
            raise HTTPException(status_code=503, detail="reconciliation unavailable")
        return planned_response(admin.repository.head(), profile_id)

    @app.post(
        "/api/v1/reconciliations",
        response_model=ReconciliationAcceptedResponse,
        responses=bounded_error_responses(401, 403, 409, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="applyReconciliation",
    )
    def reconcile(
        body: ReconciliationRequest,
        request: Request,
        authenticated: Actor = authenticated_actor,
    ) -> dict[str, object]:
        require_mutation_role(authenticated, "/api/v1/reconciliations")
        if admin is None or admin.reconciler is None:
            raise HTTPException(status_code=503, detail="reconciliation unavailable")
        current_evidence = fleet_response(fleet()).evidence_digest
        if current_evidence != body.fleet_evidence_digest:
            raise HTTPException(
                status_code=409, detail="fleet acceptance evidence is stale"
            )
        try:
            result = dict(
                admin.reconciler.enqueue(
                    body.plan_digest,
                    authenticated.subject,
                    request.state.request_id,
                    fleet_evidence_digest=body.fleet_evidence_digest,
                    current_fleet_evidence=lambda: (
                        fleet_response(fleet()).evidence_digest
                    ),
                )
            )
            audits.append(
                AuditRecord(
                    request.state.request_id,
                    authenticated.subject,
                    "reconciliation.apply",
                    str(result.get("base_commit")),
                    (),
                )
            )
            return result
        except IneligibleCommit:
            raise HTTPException(
                status_code=409, detail="commit is not eligible"
            ) from None
        except StaleFleetEvidence:
            raise HTTPException(
                status_code=409, detail="fleet acceptance evidence is stale"
            ) from None
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=409, detail="reconciliation plan digest is stale"
            ) from None

    @app.post(
        "/api/v1/reconciliations/{reconciliation_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
        responses=bounded_error_responses(401, 403, 404, 409, 503),
    )
    def cancel_reconciliation(
        reconciliation_id: str,
        body: ReconciliationCancelRequest,
        request: Request,
        authenticated: Actor = authenticated_actor,
    ) -> dict[str, str]:
        route = "/api/v1/reconciliations/{reconciliation_id}/cancel"
        require_mutation_role(authenticated, route)
        if admin is None or admin.cancellations is None:
            raise HTTPException(
                status_code=503, detail="reconciliation cancellation unavailable"
            )
        try:
            cancellation = admin.cancellations.enqueue_cancel(
                reconciliation_id,
                body.reason,
                actor=authenticated.subject,
                request_id=request.state.request_id,
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="reconciliation is unavailable"
            ) from None
        except ValueError:
            raise HTTPException(
                status_code=409, detail="reconciliation cannot be cancelled"
            ) from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                "reconciliation.cancel.request",
                None,
                (),
            )
        )
        return {
            "reconciliation_id": cancellation.reconciliation_id,
            "state": cancellation.state,
        }

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
            body.base_commit,
            body.targets,
            body.payload,
            request_id=request.state.request_id,
        )
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                f"job.enqueue:{body.kind}",
                body.base_commit,
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
                {"id": str(job.id), "state": str(job.state), "kind": str(job.kind)}
                for job in page
            ],
            "next_cursor": next_cursor,
            "total": total,
        }

    @app.get("/api/v1/audit")
    def audit_view(_actor: Actor = authenticated_actor) -> dict[str, object]:
        return {
            "events": [
                {
                    "request_id": event.request_id,
                    "actor": event.actor,
                    "action": event.action,
                    "base_commit": event.base_commit,
                    "targets": list(event.targets),
                }
                for event in audits.list()
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

    def update_services() -> Any:
        if updates is None:
            raise HTTPException(
                status_code=503,
                detail="platform update administration unavailable",
            )
        return updates

    def require_update_operator(authenticated: Actor) -> None:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")

    def update_targets(result: Mapping[str, object]) -> tuple[str, ...]:
        nodes = result.get("nodes")
        if not isinstance(nodes, Sequence):
            return ()
        return tuple(
            str(node["node_id"])
            for node in nodes
            if isinstance(node, Mapping) and isinstance(node.get("node_id"), str)
        )

    @app.get("/api/v1/updates/skew")
    def update_skew(_actor: Actor = authenticated_actor) -> dict[str, object]:
        try:
            return dict(update_services().skew())
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503,
                detail="platform update evidence unavailable",
            ) from None

    @app.post("/api/v1/updates/plan")
    def update_plan(
        body: UpdatePlanRequest,
        authenticated: Actor = authenticated_actor,
    ) -> dict[str, object]:
        require_update_operator(authenticated)
        try:
            return dict(
                update_services().plan(
                    release=body.release,
                )
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except (OSError, RuntimeError, TypeError):
            raise HTTPException(
                status_code=503,
                detail="platform update planning unavailable",
            ) from None

    @app.post(
        "/api/v1/updates",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def apply_update(
        body: UpdateApplyRequest,
        request: Request,
        authenticated: Actor = authenticated_actor,
    ) -> dict[str, object]:
        require_update_operator(authenticated)
        try:
            result = dict(
                update_services().apply(
                    body.plan_digest,
                    authenticated.subject,
                    request.state.request_id,
                )
            )
        except KeyError:
            raise HTTPException(
                status_code=409,
                detail="platform update plan digest is stale",
            ) from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except (OSError, RuntimeError, TypeError):
            raise HTTPException(
                status_code=503,
                detail="platform update dispatch unavailable",
            ) from None
        if authenticated.role != "administrator":
            result["can_approve_resume"] = False
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                "platform.update.apply",
                None,
                update_targets(result),
            )
        )
        return result

    @app.get("/api/v1/updates/{rollout_id}")
    def update_status(
        rollout_id: str,
        authenticated: Actor = authenticated_actor,
    ) -> dict[str, object]:
        try:
            result = dict(update_services().status(rollout_id))
            if authenticated.role != "administrator":
                result["can_approve_resume"] = False
            return result
        except KeyError:
            raise HTTPException(
                status_code=404, detail="update rollout not found"
            ) from None
        except ValueError:
            raise HTTPException(
                status_code=422, detail="update rollout ID is invalid"
            ) from None
        except (OSError, RuntimeError, TypeError):
            raise HTTPException(
                status_code=503,
                detail="platform update status unavailable",
            ) from None

    @app.post(
        "/api/v1/updates/{rollout_id}/approve-resume",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def approve_update_resume(
        rollout_id: str,
        body: UpdateApproveResumeRequest,
        request: Request,
        authenticated: Actor = authenticated_actor,
    ) -> dict[str, object]:
        if authenticated.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")
        try:
            result = dict(
                update_services().approve_resume(
                    rollout_id,
                    authenticated.subject,
                    request.state.request_id,
                    body.reason,
                )
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="update rollout not found"
            ) from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except (OSError, RuntimeError, TypeError):
            raise HTTPException(
                status_code=503,
                detail="platform update approval unavailable",
            ) from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                "platform.update.approve-resume",
                None,
                update_targets(result),
            )
        )
        return result

    return app


def production_app() -> FastAPI:
    from sqlalchemy import func, select, text

    from .agent_reconciliation import (
        bind_reconciliation_result_consumer,
        load_reconciliation_authority_input,
    )
    from .artifact_sizes import DeclaredArtifactSizeResolver
    from .audit import SqlAuditStore
    from .catalog_seeds import seed_standard_families
    from .code_host import RepositoryCodeHost
    from .dashboard import DashboardService
    from .db import build_engine, session_factory
    from .desired_state import (
        DesiredStateResolver,
        durable_desired_state_observations,
    )
    from .fleet_events import FleetEventRepository
    from .fleet_projection import FleetProjection
    from .fleet_stream import FleetStream
    from .git_policy import GitPolicy, PolicyStore
    from .hermes_routes import RepositoryHermesRoutePolicy
    from .host_state import HostGenerationStore
    from .install_admission import InstallAdmissionService
    from .jobs import JobService
    from .library_projection import LibraryProjection
    from .logging import JobLogStore
    from .metrics import MetricsRegistry, OperationalMetricsCollector
    from .models import Job
    from .offline import OnlineLock
    from .operation_api import durable_operation_services
    from .orchestration import ReconciliationOrchestrator
    from .package_publication import PackagePublicationService
    from .package_services import ProductionPackageProjectionService
    from .package_validation_runner import PackageValidationRunner
    from .presence import ManagementAddressPolicy
    from .proposals import ProposalService
    from .recipe_routes import AtomicRecipeRoutePublisher, RecipeRouteService
    from .reconcile import ChangeService, Reconciler
    from .repository import RepositoryService
    from .route_runtime import AtomicRouteBundlePublisher, FileSupervisorAcknowledger
    from .run_admission import RunAdmissionService
    from .settings import GenerationStartupSettings, Settings
    from .telemetry import TelemetryRepository
    from .update_admin import (
        DurableUpdateGrantRefresher,
        PlatformUpdateAdminService,
        durable_agent_observations,
        durable_distributed_workloads,
        durable_route_impacts,
        durable_update_status,
        selected_platform_target,
        topology_exclusions_from_document,
    )
    from .update_grants import AdminActionGrantIssuer
    from .updates import UpdateOrchestrator
    from .worker_authority import RepositoryAuthorityService
    from .workload_signer import UnixWorkloadSignerClient
    from .workload_trust import WorkloadTrustDelivery

    generation = GenerationStartupSettings.from_env_and_secrets()
    sessions = session_factory(build_engine(generation.database_url))
    clock = lambda: datetime.now(UTC)

    def database_revision() -> str:
        with sessions() as session:
            revision = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        if not isinstance(revision, str):
            raise TypeError("database revision is invalid")
        return revision

    settings = Settings.from_env_and_secrets()
    if settings.database_url != generation.database_url:
        raise RuntimeError("generation and production database settings differ")
    generation_readiness = GenerationReadinessService(
        sessions,
        GenerationProcessIdentity(
            startup_mode=generation.startup_mode,
            operation_id=generation.operation_id,
            generation_id=generation.generation_id,
            release_digest=generation.release_digest,
            build_digest=generation.build_digest,
            platform_version=generation.platform_version,
            process_image=generation.process_image,
            database_revision=generation.database_revision,
            start_nonce=generation.start_nonce,
        ),
        DirectoryIdentityProjectionSource(generation.identity_root),
        clock=clock,
        database_revision=database_revision,
    )
    if generation.startup_mode is StartupMode.PRESELECTION:
        return create_preselection_app(generation_readiness)

    actual_revision = database_revision()
    if actual_revision != generation.database_revision:
        raise RuntimeError("selected database revision does not match generation")
    with sessions.begin() as session:
        seed_standard_families(session, clock())

    online_lock = OnlineLock(settings.state_path / "offline.lock")
    online_lock.__enter__()
    token_codec = TokenCodec(settings.token_signing_key)
    cursor_codec = token_codec.cursor_codec()
    job_service = JobService(sessions, clock=clock, cursors=cursor_codec)
    repository = RepositoryService(settings.repository_path)
    proposals = ProposalService(repository, head=repository.head)
    if settings.git_signing_key_path is None:
        raise RuntimeError("production Git signing key is unavailable")
    policy_store = PolicyStore(settings.state_path / "git-policy")
    code_host = RepositoryCodeHost(
        settings.repository_path,
        signing_key=settings.git_signing_key_path,
        lock_path=settings.state_path / "git-change.lock",
    )
    git_policy = GitPolicy(
        policy_store,
        code_host,
        protected_branch=settings.deployment_branch,
        required_checks=settings.required_checks,
    )
    changes = ChangeService(proposals, git_policy)
    reconciler = Reconciler(
        git_policy,
        DesiredStateResolver(repository, clock=clock),
        jobs=job_service,
        observations=lambda: durable_desired_state_observations(sessions),
        orchestrator=ReconciliationOrchestrator(sessions, clock=clock),
    )
    dashboard = DashboardService(
        repository,
        sessions,
        protocol_minimum=generation.protocol_minimum,
        protocol_maximum=generation.protocol_maximum,
    )
    telemetry_repository = TelemetryRepository(sessions, clock=clock)
    fleet_event_repository = FleetEventRepository(sessions, clock=clock)
    visual_fleet = FleetProjection(
        repository,
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
        protocol_minimum=generation.protocol_minimum,
        protocol_maximum=generation.protocol_maximum,
    )
    commit_eligible = lambda commit: git_policy.eligible(commit).ok
    current_commit = lambda: repository.head(settings.deployment_branch)
    agent_services = build_agent_services(
        settings,
        sessions,
        clock,
        commit_eligible=commit_eligible,
        current_commit=current_commit,
    )
    validation_runner = PackageValidationRunner(
        sessions,
        agent_services.operations,
        clock=clock,
    )
    package_services = ProductionPackageProjectionService(
        repository,
        sessions,
        fleet=dashboard.fleet,
        clock=clock,
        agent_jobs=agent_services.operations,
        package_trust=WorkloadTrustDelivery(
            metadata_root=settings.workload_tuf_metadata_root,
            target_root=settings.workload_tuf_target_root,
            clock=clock,
        ),
        validation_runner=validation_runner,
    )
    package_services.install_publication(
        PackagePublicationService(
            candidate_loader=package_services.publication_candidate,
            head=current_commit,
            commit_eligible=commit_eligible,
            publisher=UnixWorkloadSignerClient(settings.workload_signer_socket_path),
            clock=clock,
        )
    )
    if settings.admin_grant_private_key_path is None:
        raise RuntimeError("production admin grant private key is unavailable")
    admin_grant_issuer = AdminActionGrantIssuer.from_private_key_file(
        settings.admin_grant_private_key_path,
        clock=clock,
    )
    # API methods on this instance are deliberately limited to database-only
    # create/authorize/approve paths. Only the worker owns signer IPC and route
    # side effects, so these sentinels must never be consumed in this process.
    api_update_orchestrator = UpdateOrchestrator(
        sessions,
        object(),
        object(),
        clock=clock,
    )
    selected_generations = HostGenerationStore(
        settings.state_path,
        generation.identity_root,
    )

    def active_update_target():
        return selected_platform_target(
            projections=selected_generations,
            running_generation_id=generation.generation_id,
            running_platform_version=generation.platform_version,
            running_release_digest=generation.release_digest,
            running_build_digest=generation.build_digest,
            metadata_root=settings.agent_tuf_metadata_root,
            target_root=settings.agent_tuf_target_root,
            base_commit=current_commit(),
        )

    update_admin = PlatformUpdateAdminService(
        target_source=active_update_target,
        observation_source=lambda: durable_agent_observations(sessions, clock),
        workload_source=lambda: durable_distributed_workloads(sessions),
        orchestrator=api_update_orchestrator,
        grant_issuer=admin_grant_issuer,
        status_source=lambda identifier: durable_update_status(sessions, identifier),
        clock=clock,
        grant_refresher=DurableUpdateGrantRefresher(
            sessions,
            admin_grant_issuer,
            clock=clock,
        ),
        topology_source=lambda: topology_exclusions_from_document(
            repository.read_document(current_commit(), "inventory/topology.json").parsed
        ),
        route_source=lambda: durable_route_impacts(sessions),
    )

    def reconciliation_authority_input(
        reconciliation_id: str,
    ) -> tuple[str, str, tuple[Any, ...], str]:
        def endpoint(session, node_id: str) -> tuple[str, Any]:
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
            snapshot.base_commit,
            snapshot.plan_digest,
            snapshot.routes,
            snapshot.fleet_evidence_digest,
        )

    worker_authority = RepositoryAuthorityService(
        current_commit=current_commit,
        commit_eligible=commit_eligible,
        reconciliation_input=reconciliation_authority_input,
        current_fleet_evidence=lambda: (
            fleet_response(dashboard.fleet()).evidence_digest
        ),
        deployments=RepositoryHermesRoutePolicy(settings.repository_path).deployments,
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
        ),
        run_admission=RunAdmissionService(
            sessions,
            inventory_max_age=300,
            memory_floor_bytes=4_000_000_000,
        ),
        agent_jobs=agent_services.operations,
        clock=clock,
        route_publications=recipe_routes,
        builds=RecipeBuildService(
            sessions,
            bundles=SourceBundleStore(settings.state_path / "source-bundles"),
            inventory_max_age=300,
        ),
        mappings=ClusterMappingService(sessions),
    )
    reconciliation_cancellations = bind_reconciliation_result_consumer(
        sessions,
        operations=agent_services.operations,
        presence=agent_services.presence,
        clock=clock,
        commit_eligible=commit_eligible,
        current_commit=current_commit,
        additional_result_consumer=recipe_operations.consume_agent_result,
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
    app = create_app(
        jobs=job_service,
        tokens=token_codec,
        audits=SqlAuditStore(sessions, clock),
        fleet=dashboard.fleet,
        fleet_projection=visual_fleet,
        fleet_stream=visual_fleet_stream,
        library_projection=visual_library,
        admin=AdminServices(
            repository,
            proposals,
            changes,
            reconciler,
            reconciliation_cancellations,
        ),
        metrics=metrics,
        metrics_token=settings.metrics_token,
        metrics_refresh=refresh_metrics,
        job_logs=JobLogStore(settings.state_path / "job-logs"),
        agent=(agent_services if settings.agent_runtime == "enabled" else None),
        trusted_agent_proxy_auth=settings.agent_proxy_auth,
        worker_authority=(
            worker_authority if settings.agent_runtime == "enabled" else None
        ),
        worker_api_token=(
            settings.worker_api_token if settings.agent_runtime == "enabled" else b""
        ),
        operations=durable_operation_services(
            sessions,
            Path("/routes"),
            clock=clock,
            cursors=cursor_codec,
        ),
        updates=update_admin,
        packages=PackageApiServices.from_object(package_services),
        catalog=CatalogService(
            sessions,
            clock=clock,
            source_bundles=SourceBundleStore(settings.state_path / "source-bundles"),
        ),
        global_catalog=global_catalog,
        workload_run=WorkloadRunWorkflow(
            sessions,
            clock=clock,
            bundles=SourceBundleStore(settings.state_path / "source-bundles"),
        ),
        browser_auth=BrowserAuthService(
            sessions,
            token_signing_key=settings.token_signing_key,
            clock=clock,
        ),
        recipe_operations=recipe_operations,
    )
    install_selected_generation_readiness(app, generation_readiness)
    web_root = Path(__file__).resolve().parent / "web"
    if web_root.is_dir():
        app.mount("/", SpaFiles(directory=web_root, html=True), name="admin-web")

    @app.on_event("shutdown")
    def release_online_lock() -> None:
        global_catalog.close()
        online_lock.__exit__()

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(production_app(), host="0.0.0.0", port=8000, access_log=False)
