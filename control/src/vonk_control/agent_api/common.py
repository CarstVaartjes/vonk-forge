"""Agent api: common concerns."""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import hashlib
import logging
import math
import os
import re
import stat
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Any

from fastapi import HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentEvidenceCode,
    SignedHostHelperGrant,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.agent_words import RecipeRunDispositionValue
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import PAYLOAD_MODELS
from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_agent_protocol.host_helper import (
    ContainerRuntimeActionName,
    RecipeReconciliationIdentity,
)
from vonk_agent_protocol.http_failure import HttpRefusalReason
from vonk_agent_protocol.optional_evidence import OptionalEvidenceModel
from vonk_agent_protocol.package_upgrade import PackageActivationReceipt
from vonk_agent_protocol.state_machines import EnrollmentPurpose

from vonk_control.http_errors import SecurityHTTPError

from ..agent_jobs import AgentJobService
from ..auth import (
    AgentIdentity,
    AgentSource,
    agent_identity_from_scope,
    agent_source_from_scope,
)
from ..distribution import DistributionService
from ..enrollment import EnrollmentService
from ..enrollment.responses import (  # noqa: F401 -- shared package exports
    _issued_response,
    _json_response,
    _now,
    _wire,
)
from ..enrollment_bootstrap import EnrollmentBootstrapConfig, InstallerUrl
from ..enrollment_contract import EnrollmentId
from ..host_helper_authority import HostRuntimeAuthorityService
from ..integer_domains import MAX_DATABASE_BIGINT
from ..logging import log_event
from ..models import AgentCertificate, AgentNode, AgentOperation
from ..presence import AgentPresenceService, ManagementAddressPolicy, PresenceError
from ..source_bundles import SourceBundleStoreProtocol
from ..strict_json import StrictJSONModel, read_stored_model
from ..telemetry import TelemetrySampleInput

"""mTLS-authenticated machine agent API routes."""


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


_TELEMETRY_FIELDS = tuple(
    item.name
    for item in dataclasses.fields(TelemetrySampleInput)
    if item.name != "boot_id"
)


RECIPE_RUN_DISPOSITION_HEADER = "x-vonk-recipe-run-disposition"


RECIPE_RUN_UNOWNED = RecipeRunDispositionValue.UNOWNED.value


RECIPE_RUN_GENERATION_HEADER = "x-vonk-recipe-run-generation"


_UUID4_TEXT = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


_CANONICAL_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)


_IDENTIFIER_TEXT = r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$"


_LIVE_OPERATION_STATES = frozenset({"queued", "running"})


_MAX_ENROLLMENT_BODY_BYTES = 64 * 1024


RANK_UNREADY_GRACE = timedelta(seconds=120)


_LOGGER = logging.getLogger("vonk_control.agent_api")


def _log_evidence_dropped(
    code: AgentEvidenceCode, *, endpoint: str, node_id: str
) -> None:
    """Name optional agent evidence that was dropped so its core report was kept."""

    log_event(
        _LOGGER,
        "agent.evidence_dropped",
        service="control-api",
        code=code.value,
        endpoint=endpoint,
        node_id=node_id,
    )


def _log_evidence_warnings(
    body: OptionalEvidenceModel, *, endpoint: str, node_id: str
) -> None:
    for code in body.evidence_warnings:
        _log_evidence_dropped(code, endpoint=endpoint, node_id=node_id)


_MAX_ENROLLMENT_TOKEN_PREFIX_BYTES = 2 * 1024


_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


MAX_RECIPE_IMAGE_BYTES = 16 * 1024**4


_MAX_RANGE_BYTES = 64 * 1024 * 1024


_SERVED_ROOT = Path("/state")


_SERVED_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")


_DISTRIBUTION_ERROR_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")


def _strict_json_datetime(value: object) -> object:
    """Decode the JSON datetime representation before strict validation.

    FastAPI hands Pydantic an already-decoded Python mapping, whereas
    ``model_validate_json(..., strict=True)`` still accepts ISO datetime text.
    Decode that one documented wire representation explicitly so strict route
    models behave the same in both entry points.
    """

    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        # Pydantic turns ValueError into the stable request validation response.
        raise ValueError(  # noqa: TRY004
            "observed time must be an RFC 3339 string"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("observed time must be an RFC 3339 string") from error
    if "T" not in value and "t" not in value:
        raise ValueError("observed time must be an RFC 3339 string")
    return parsed


@dataclass(frozen=True)
class AgentApiServices:
    enrollment: EnrollmentService | None
    operations: AgentJobService
    sessions: sessionmaker[Session]
    clock: Callable[[], datetime]
    presence: AgentPresenceService
    artifact_root: Path
    source_bundles: SourceBundleStoreProtocol
    max_artifact_bytes: int = _MAX_ARTIFACT_BYTES
    max_recipe_image_bytes: int = MAX_RECIPE_IMAGE_BYTES
    max_range_bytes: int = _MAX_RANGE_BYTES
    served_root: Path = _SERVED_ROOT
    host_runtime_authority: HostRuntimeAuthorityService | None = None
    fabric_policy: ManagementAddressPolicy | None = None
    bootstrap: EnrollmentBootstrapConfig | None = None
    # Optional production adapter. The run/profile worker registers exact
    # assignments; the source itself remains owned by the NAS cache worker and
    # recipe image store.
    distribution: DistributionService | None = None


class EnrollmentRateLimiter:
    """Fixed global admission limit for unauthenticated enrollment bodies.

    The limiter intentionally has no client-keyed state: before enrollment a
    caller is unauthenticated, so attacker-chosen client addresses must not
    allocate unbounded memory. It is process-local; the deployment runs one
    control API instance behind the sole Caddy ingress boundary.
    """

    def __init__(
        self,
        *,
        maximum: int = 20,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum < 1 or window_seconds <= 0:
            raise ValueError("enrollment rate limit must be positive")
        self._maximum = maximum
        self._window_seconds = window_seconds
        self._clock = clock
        self._admitted: deque[float] = deque()
        self._lock = Lock()

    def admit(self) -> bool:
        now = self._clock()
        with self._lock:
            cutoff = now - self._window_seconds
            while self._admitted and self._admitted[0] <= cutoff:
                self._admitted.popleft()
            if len(self._admitted) >= self._maximum:
                return False
            self._admitted.append(now)
            return True

    def retry_after_seconds(self) -> int:
        """The next capacity observation, without retaining request ownership."""
        with self._lock:
            if not self._admitted:
                return 1
            return max(
                1,
                math.ceil(self._admitted[0] + self._window_seconds - self._clock()),
            )


class EnrollmentGrantResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: EnrollmentId
    expires_at: str = Field(min_length=1, max_length=64)
    purpose: EnrollmentPurpose
    token: str = Field(min_length=43, max_length=64)
    controller_endpoint: str = Field(min_length=1, max_length=2048)
    enrollment_endpoint: str = Field(min_length=1, max_length=2048)
    ca_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    controller_address: str | None = None
    service_hostnames: list[str] = Field(default_factory=list, max_length=16)
    installer_url: InstallerUrl


class AgentGrantRequest(StrictJSONModel):
    """A helper grant for the attempt the fence names; mTLS names the node."""

    model_config = ConfigDict(extra="forbid", strict=True)
    fence: str = Field(pattern=_UUID4_TEXT)
    expires_in_seconds: int = Field(ge=1, le=300)


class HostRuntimeGrantRequest(AgentGrantRequest):
    installation_intent_nonce: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    action: ContainerRuntimeActionName
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stop_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    run_generation: (
        Annotated[int, Field(ge=1, le=MAX_DATABASE_BIGINT, strict=True)] | None
    ) = None
    runtime_run_id: str | None = Field(default=None, pattern=_UUID4_TEXT)
    runtime_target_id: str | None = Field(default=None, pattern=_UUID4_TEXT)
    runtime_installation_id: str | None = Field(default=None, pattern=_UUID4_TEXT)
    installation_id: str | None = Field(default=None, pattern=_UUID4_TEXT)
    reconciliation_identity: RecipeReconciliationIdentity | None = None

    @model_validator(mode="after")
    def runtime_binding(self) -> HostRuntimeGrantRequest:
        if (self.installation_id is not None) != (
            self.action == "installation-cleanup"
        ):
            raise ValueError("host runtime installation binding is invalid")
        has_runtime_binding = any(
            value is not None
            for value in (
                self.start_plan_sha256,
                self.stop_plan_sha256,
                self.run_generation,
                self.runtime_run_id,
                self.runtime_target_id,
                self.runtime_installation_id,
            )
        )
        if self.action in {"start", "stop"}:
            expected_plan = (
                self.start_plan_sha256
                if self.action == "start"
                else self.stop_plan_sha256
            )
            unexpected_plan = (
                self.stop_plan_sha256
                if self.action == "start"
                else self.start_plan_sha256
            )
            if (
                expected_plan is None
                or unexpected_plan is not None
                or self.run_generation is None
                or self.runtime_run_id is None
                or self.runtime_target_id is None
                or self.runtime_installation_id is None
            ):
                raise ValueError("host runtime plan binding is invalid")
        elif has_runtime_binding:
            raise ValueError("host runtime plan binding does not match the action")
        if self.reconciliation_identity is not None and (
            self.action != "installation-cleanup"
            or self.reconciliation_identity.installation_id != self.installation_id
        ):
            raise ValueError("host runtime reconciliation binding is invalid")
        return self


class PackageActivationGrantRequest(StrictJSONModel):
    receipt: PackageActivationReceipt
    runtime_identity: AgentRuntimeIdentity


class AgentUpgradeGrantRequest(AgentGrantRequest):
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_signature: str = Field(pattern=r"^[0-9a-f]{128}$")


class HostHelperGrantResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grant: SignedHostHelperGrant


def _host_grant_response(grant: SignedHostHelperGrant) -> HostHelperGrantResponse:
    return HostHelperGrantResponse(grant=grant)


def _require_services(services: AgentApiServices | None) -> AgentApiServices:
    if services is None:
        raise HTTPException(status_code=503, detail="agent API is unavailable")
    return services


def _require_enrollment(services: AgentApiServices) -> EnrollmentService:
    if services.enrollment is None:
        raise HTTPException(status_code=503, detail="agent enrollment is unavailable")
    return services.enrollment


def _scope_identity(request: Request) -> AgentIdentity:
    identity = agent_identity_from_scope(dict(request.scope))
    if identity is None:
        raise SecurityHTTPError(
            reason=HttpRefusalReason.AUTHENTICATION_REQUIRED,
            status_code=401,
            detail="verified agent identity required",
        )
    return identity


def active_agent_identity(
    services: AgentApiServices, identity: AgentIdentity | None
) -> bool:
    return _agent_identity_state(services, identity) == "active"


def activation_agent_identity(
    services: AgentApiServices, identity: AgentIdentity | None
) -> bool:
    return _agent_identity_state(services, identity) in {"active", "staged"}


def _agent_identity_state(
    services: AgentApiServices, identity: AgentIdentity | None
) -> str | None:
    if identity is None:
        return None
    now = _now(services.clock())
    with services.sessions() as session:
        valid = session.scalar(
            select(AgentCertificate.state)
            .join(AgentNode, AgentNode.node_id == AgentCertificate.node_id)
            .where(
                AgentCertificate.serial == identity.certificate_serial,
                AgentCertificate.node_id == identity.node_id,
                AgentCertificate.fingerprint == identity.certificate_fingerprint,
                AgentCertificate.revoked_at.is_(None),
                AgentCertificate.not_before <= now,
                AgentCertificate.not_after > now,
                AgentNode.state == "active",
                AgentNode.revoked_at.is_(None),
            )
        )
    return valid


def _authenticated_identity(
    request: Request, services: AgentApiServices
) -> AgentIdentity:
    identity = _scope_identity(request)
    if not active_agent_identity(services, identity):
        raise SecurityHTTPError(
            reason=HttpRefusalReason.AUTHENTICATION_REQUIRED,
            status_code=401,
            detail="agent certificate is not active",
        )
    return identity


def _authenticated_activation_identity(
    request: Request, services: AgentApiServices
) -> AgentIdentity:
    identity = _scope_identity(request)
    if not activation_agent_identity(services, identity):
        raise SecurityHTTPError(
            reason=HttpRefusalReason.AUTHENTICATION_REQUIRED,
            status_code=401,
            detail="agent certificate cannot activate",
        )
    return identity


def _body_node_matches(value: str, identity: AgentIdentity) -> None:
    if value != identity.node_id:
        raise SecurityHTTPError(
            reason=HttpRefusalReason.AUTHORITY_DENIED,
            status_code=403,
            detail="authenticated node identity cannot be overridden",
        )


def _validated_authenticated_source(
    request: Request,
    services: AgentApiServices,
    identity: AgentIdentity,
) -> AgentSource:
    source = agent_source_from_scope(dict(request.scope))
    if source is None or source.identity != identity:
        raise SecurityHTTPError(
            reason=HttpRefusalReason.AUTHENTICATION_REQUIRED,
            status_code=401,
            detail="verified agent source required",
        )
    try:
        return services.presence.validate(source)
    except PresenceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


_ENROLLMENT_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


_JSON_WHITESPACE = frozenset(b" \t\r\n")


@dataclass(frozen=True)
class _EnrollmentGrantScan:
    tokens: tuple[str, ...]
    top_level_keys: int


def _references_digest(value: object, digest: str) -> bool:
    if isinstance(value, str):
        return value == digest
    if isinstance(value, Mapping):
        return any(_references_digest(item, digest) for item in value.values())
    if isinstance(value, list):
        return any(_references_digest(item, digest) for item in value)
    return False


def _sha256_path(path: Path, expected_bytes: int) -> str:
    """Hash an uploaded archive once, where it enters the Controller store."""
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            read += len(chunk)
            if read > expected_bytes:
                raise HTTPException(
                    status_code=409, detail="recipe image storage conflicts"
                )
            digest.update(chunk)
    if read != expected_bytes:
        raise HTTPException(status_code=409, detail="recipe image storage conflicts")
    return digest.hexdigest()


class RecipeImageUploadHeaders(BaseModel):
    """Identity and cursor for one authenticated resumable archive transfer."""

    model_config = ConfigDict(extra="forbid")
    layout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    image_bytes: int = Field(gt=0)
    offset: int = Field(default=0, ge=0)


class RecipeImageUploadStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    offset: int = Field(ge=0)
    complete: bool

    def response(self) -> Response:
        return Response(
            headers={
                "x-vonk-upload-offset": str(self.offset),
                "x-vonk-upload-complete": "true" if self.complete else "false",
            }
        )


def _prepare_recipe_image_upload(
    artifact_root: Path, identity: str
) -> tuple[int, Path]:
    artifact_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    temporary = artifact_root / f".{identity}.upload"
    try:
        metadata = temporary.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and (
        not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
    ):
        temporary.rename(
            temporary.with_name(f"{temporary.name}.damaged-{uuid.uuid4().hex}")
        )
    try:
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
    except OSError as error:
        if error.errno == errno.ELOOP:
            # Preserve the link itself; never follow it or modify its target.
            temporary.rename(
                temporary.with_name(f"{temporary.name}.damaged-{uuid.uuid4().hex}")
            )
            descriptor = os.open(
                temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
        else:
            raise UnknownOutcomeError(
                "upload storage observation is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from None
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        temporary.rename(
            temporary.with_name(f"{temporary.name}.damaged-{uuid.uuid4().hex}")
        )
        raise UnknownOutcomeError(
            "upload checkpoint was isolated",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise UnknownOutcomeError(
            "recipe image upload ownership is busy",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from None
    return descriptor, temporary


def _flush_and_sync(stream: Any) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def _commit_recipe_image_upload(
    temporary: Path,
    destination: Path,
    *,
    expected_bytes: int,
) -> None:
    # Different requests for the same content share one nonblocking publisher.
    lock = destination.with_name(f".{destination.name}.publish")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        if isinstance(error, PermissionError) or error.errno == errno.ELOOP:
            raise HTTPException(
                status_code=503, detail="publication storage observation unavailable"
            ) from None
        raise UnknownOutcomeError(
            "publication storage observation is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UnknownOutcomeError(
                "recipe image publication ownership is busy",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from None
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None and not stat.S_ISREG(metadata.st_mode):
            destination.rename(
                destination.with_name(f".{destination.name}.damaged-{uuid.uuid4().hex}")
            )
            metadata = None
        # A freshly verified replacement supersedes even a same-sized damaged
        # object. Publication never adopts old bytes based on their size alone.
        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except PermissionError:
        raise HTTPException(
            status_code=503, detail="image storage observation unavailable"
        ) from None
    except OSError:
        raise UnknownOutcomeError(
            "image publication storage observation is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from None
    finally:
        os.close(descriptor)


def _unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _owned_artifact(
    services: AgentApiServices, identity: AgentIdentity, digest: str
) -> tuple[Path, int]:
    """Return the stored artifact this node's live operation names, and its size."""
    if _DIGEST.fullmatch(digest) is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    for delay in (0.0, 0.05, 0.1, 0.2):
        if delay:
            time.sleep(delay)
        with services.sessions() as session:
            operations = list(
                session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.node_id == identity.node_id,
                        AgentOperation.state.in_(_LIVE_OPERATION_STATES),
                    )
                )
            )
        missing_evidence = False
        owned = False
        for operation in operations:
            try:
                payload = read_stored_model(
                    PAYLOAD_MODELS[OperationKind(operation.kind)], operation.payload
                )
            except (KeyError, TypeError, ValueError):
                missing_evidence = True
                continue
            if _references_digest(payload.model_dump(mode="json"), digest):
                owned = True
                break
        if owned:
            break
        if not missing_evidence:
            raise HTTPException(status_code=404, detail="artifact not assigned")
    else:
        raise UnknownOutcomeError(
            "artifact assignment observation is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    maximum = services.max_artifact_bytes
    path = services.artifact_root / digest
    for delay in (0.0, 0.05, 0.1, 0.2):
        if delay:
            time.sleep(delay)
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except PermissionError:
            raise HTTPException(
                status_code=503, detail="artifact observation unavailable"
            ) from None
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise HTTPException(status_code=503, detail="unsafe artifact destination")
        if metadata.st_size > maximum:
            continue
        return path, metadata.st_size
    raise UnknownOutcomeError(
        "authorized artifact storage observation is unavailable",
        reason=WaitReason.OBSERVATION_UNAVAILABLE,
    )


def _served_from_edge(services: AgentApiServices, path: Path, etag: str) -> Response:
    """Authorize-only answer: the edge reads the named file and serves the range.

    The file name comes from Controller storage, never from the request, and it
    must sit under the fixed root the edge mounts read-only. The edge replaces
    this response with the file; the header never reaches an agent.
    """
    try:
        relative = path.relative_to(services.served_root).as_posix()
    except ValueError:
        relative = ""
    if _SERVED_FILE.fullmatch(relative) is None or ".." in relative.split("/"):
        raise HTTPException(status_code=503, detail="object is unavailable")
    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            "X-Vonk-File": relative,
            "ETag": etag,
            "Cache-Control": "no-store",
            "Content-Type": "application/octet-stream",
        },
    )


def _range(value: str | None, total: int, maximum: int) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"bytes=(\d+)-(\d+)", value)
    if match is None:
        raise HTTPException(status_code=416, detail="range is invalid")
    if any(len(part) > 19 for part in match.groups()):
        raise HTTPException(status_code=416, detail="range is invalid")
    try:
        start, end = (int(part) for part in match.groups())
    except ValueError:
        raise HTTPException(status_code=416, detail="range is invalid") from None
    if start > end or start >= total or end >= total or end - start + 1 > maximum:
        raise HTTPException(status_code=416, detail="range is invalid")
    return start, end
