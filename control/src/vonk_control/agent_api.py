"""mTLS-authenticated machine agent API routes."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import StreamingResponse
from vonk_agent_protocol import (
    MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES,
    AgentClaim,
    AgentDirective,
    AgentProgress,
    AgentResult,
    ContainerRuntimeAction,
    DistributionAssignment,
    InventoryRequest,
    RecipeRunObservationsWire,
    SignedHostHelperGrant,
    canonical_message,
)
from vonk_agent_protocol.claims import ClaimRequest
from vonk_agent_protocol.enrollment import (
    ActivateRequest,
    EnrollmentBootstrapResponse,
    EnrollmentSubmitRequest,
    IssuedCertificateResponse,
    RenewRequest,
)
from vonk_agent_protocol.host_helper import (
    ContainerRuntimeActionName,
    RecipeReconciliationIdentity,
)
from vonk_agent_protocol.telemetry import TelemetryRequest

from .agent_jobs import CLAIM_LEASE_SECONDS, AgentJobService, StaleAgentAttempt
from .auth import (
    AgentIdentity,
    AgentSource,
    agent_identity_from_scope,
    agent_source_from_scope,
)
from .contract_graph import raw_json_body
from .distribution import DistributionError, DistributionService
from .download_contract import download_responses, upload_request_body
from .enrollment import (
    EnrollmentDenied,
    EnrollmentIssuanceUncertain,
    EnrollmentService,
    RenewalInProgress,
)
from .enrollment_bootstrap import EnrollmentBootstrapConfig, InstallerUrl
from .enrollment_contract import EnrollmentId
from .host_helper_authority import (
    HostHelperAuthorityError,
    HostRuntimeAuthorityService,
    recipe_run_known,
)
from .inventory_repository import (
    MAX_INVENTORY_FUTURE_SKEW,
    InventoryRepository,
    InventorySnapshotInput,
)
from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    ClusterMapping,
    RecipeBuild,
    RecipeRun,
    RecipeSourceBundle,
    RunNode,
)
from .operation_api import bounded_error_responses
from .pki import IssuedCertificate
from .presence import AgentPresenceService, ManagementAddressPolicy, PresenceError
from .recipe_operations import (
    prepare_exact_recipe_run_observation_nodes,
)
from .runtime_image_preparation import (
    IMAGE_CACHE_DIRECTORY,
)
from .source_bundles import SourceBundleError, SourceBundleStoreProtocol
from .strict_json import ControllerAPIRoute, StrictJSONModel
from .telemetry import TelemetryRepository, TelemetrySampleInput

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
#: Response header on a disposition lookup naming a run this Controller has
#: no record of; the agent then retires that run's local lifecycle.
RECIPE_RUN_DISPOSITION_HEADER = "x-vonk-recipe-run-disposition"
RECIPE_RUN_UNOWNED = "unowned"
_UUID4_TEXT = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CANONICAL_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_IDENTIFIER_TEXT = r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$"
_LIVE_OPERATION_STATES = frozenset({"queued", "running"})
_MAX_ENROLLMENT_BODY_BYTES = 64 * 1024
_MAX_ENROLLMENT_TOKEN_PREFIX_BYTES = 2 * 1024
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_RECIPE_IMAGE_BYTES = 16 * 1024**4
_MAX_RANGE_BYTES = 8 * 1024 * 1024
# A distribution refusal is returned as the agent's ``x-vonk-error-code`` so the
# denying check is attributable.  The vocabulary is internal, but the header is
# a wire surface, so it is validated before being reflected.
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


class EnrollmentGrantResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: EnrollmentId
    expires_at: str = Field(min_length=1, max_length=64)
    purpose: Literal["new-node", "re-enroll"]
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
    action: ContainerRuntimeActionName
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stop_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    run_generation: int | None = Field(default=None, ge=1, le=2**31 - 1, strict=True)
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


from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.package_upgrade import PackageActivationReceipt


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


def _wire(value: object) -> object:
    return json.loads(canonical_message(value))


def _now(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _issued_response(issued: IssuedCertificate) -> IssuedCertificateResponse:
    return IssuedCertificateResponse(
        node_id=issued.node_id,
        certificate_pem=issued.certificate_pem.decode("ascii"),
        chain_pem=issued.chain_pem.decode("ascii"),
        serial=issued.serial,
        fingerprint=issued.fingerprint,
        not_before=_now(issued.not_before).isoformat(),
        not_after=_now(issued.not_after).isoformat(),
        generation=issued.generation,
    )


def _json_response(value: object, *, status_code: int = 200) -> Response:
    return Response(
        content=canonical_message(value),
        status_code=status_code,
        media_type="application/json",
    )


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
        raise HTTPException(status_code=401, detail="verified agent identity required")
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
        raise HTTPException(status_code=401, detail="agent certificate is not active")
    return identity


def _authenticated_activation_identity(
    request: Request, services: AgentApiServices
) -> AgentIdentity:
    identity = _scope_identity(request)
    if not activation_agent_identity(services, identity):
        raise HTTPException(status_code=401, detail="agent certificate cannot activate")
    return identity


def _body_node_matches(value: str, identity: AgentIdentity) -> None:
    if value != identity.node_id:
        raise HTTPException(
            status_code=403, detail="authenticated node identity cannot be overridden"
        )


def _validated_authenticated_source(
    request: Request,
    services: AgentApiServices,
    identity: AgentIdentity,
) -> AgentSource:
    source = agent_source_from_scope(dict(request.scope))
    if source is None or source.identity != identity:
        raise HTTPException(status_code=401, detail="verified agent source required")
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


def _json_string_end(value: bytes | bytearray, start: int) -> int | None:
    """Return the exclusive end of one bounded JSON string literal."""
    index = start + 1
    while index < len(value):
        byte = value[index]
        if byte == ord('"'):
            return index + 1
        if byte == ord("\\"):
            index += 2
        else:
            index += 1
    return None


def _skip_json_whitespace(value: bytes | bytearray, start: int) -> int:
    while start < len(value) and value[start] in _JSON_WHITESPACE:
        start += 1
    return start


def _decode_bounded_json_string(
    value: bytes | bytearray,
    start: int,
    end: int,
    *,
    maximum_characters: int,
) -> str | None:
    # An ASCII target cannot require more than one six-byte \uXXXX escape per
    # character.  Reject longer candidates before making even a bounded copy.
    if end - start > 2 + (6 * maximum_characters):
        return None
    try:
        decoded = json.loads(bytes(value[start:end]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(decoded, str) or len(decoded) > maximum_characters:
        return None
    return decoded


def _scan_enrollment_grants(value: bytes | bytearray) -> _EnrollmentGrantScan:
    """Discover bounded grant strings without recursively parsing the body."""
    tokens: list[str] = []
    seen: set[str] = set()
    top_level_keys = 0
    root_container: int | None = None
    depth = 0
    index = 0
    while index < len(value):
        byte = value[index]
        if byte == ord('"'):
            end = _json_string_end(value, index)
            if end is None:
                break
            colon = _skip_json_whitespace(value, end)
            if colon < len(value) and value[colon] == ord(":"):
                key = _decode_bounded_json_string(
                    value, index, end, maximum_characters=len("grant_token")
                )
                if key == "grant_token":
                    if root_container == ord("{") and depth == 1:
                        top_level_keys += 1
                    token_start = _skip_json_whitespace(value, colon + 1)
                    if token_start < len(value) and value[token_start] == ord('"'):
                        token_end = _json_string_end(value, token_start)
                        if token_end is not None:
                            token = _decode_bounded_json_string(
                                value,
                                token_start,
                                token_end,
                                maximum_characters=43,
                            )
                            if (
                                token is not None
                                and _ENROLLMENT_TOKEN.fullmatch(token) is not None
                                and token not in seen
                            ):
                                seen.add(token)
                                tokens.append(token)
            index = end
            continue
        if byte in (ord("{"), ord("[")):
            if root_container is None and depth == 0:
                root_container = byte
            depth += 1
        elif byte in (ord("}"), ord("]")) and depth > 0:
            depth -= 1
        index += 1
    return _EnrollmentGrantScan(tuple(tokens), top_level_keys)


def _consume_enrollment_denial(
    services: AgentApiServices, tokens: tuple[str, ...]
) -> None:
    if not tokens:
        return
    enrollment = _require_enrollment(services)
    for token in tokens:
        try:
            enrollment.submit(token, b"", {})
        except EnrollmentDenied:
            pass


async def _bounded_enrollment_body(
    request: Request, services: AgentApiServices
) -> bytearray:
    buffered = bytearray()
    token_prefix = bytearray()
    async for chunk in request.stream():
        prefix_remaining = _MAX_ENROLLMENT_TOKEN_PREFIX_BYTES - len(token_prefix)
        if prefix_remaining > 0:
            token_prefix.extend(chunk[:prefix_remaining])
        remaining = _MAX_ENROLLMENT_BODY_BYTES - len(buffered)
        if len(chunk) > remaining:
            scan = _scan_enrollment_grants(token_prefix)
            _consume_enrollment_denial(services, scan.tokens)
            raise HTTPException(
                status_code=413, detail="enrollment request is too large"
            )
        buffered.extend(chunk)
    return buffered


def _references_digest(value: object, digest: str) -> bool:
    if isinstance(value, str):
        return value == digest
    if isinstance(value, Mapping):
        return any(_references_digest(item, digest) for item in value.values())
    if isinstance(value, list):
        return any(_references_digest(item, digest) for item in value)
    return False


def _sha256_path(path: Path, expected_bytes: int) -> str:
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
    descriptor = os.open(temporary, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise HTTPException(
            status_code=409, detail="recipe image upload is active"
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
    layout_sha256: str,
) -> None:
    if destination.exists():
        if (
            destination.stat().st_size != expected_bytes
            or _sha256_path(destination, expected_bytes) != layout_sha256
        ):
            raise HTTPException(
                status_code=409, detail="recipe image storage conflicts"
            )
        temporary.unlink()
        return
    os.chmod(temporary, 0o640)
    os.replace(temporary, destination)


def _unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _open_owned_artifact(
    services: AgentApiServices, identity: AgentIdentity, digest: str
) -> tuple[int, int, int, bool]:
    if _DIGEST.fullmatch(digest) is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    with services.sessions() as session:
        operations = list(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.node_id == identity.node_id,
                    AgentOperation.state.in_(_LIVE_OPERATION_STATES),
                )
            )
        )
    owners = [
        operation
        for operation in operations
        if _references_digest(operation.payload, digest)
    ]
    if not owners:
        raise HTTPException(status_code=404, detail="artifact not found")
    recipe_image = any(
        operation.kind == "recipe.image.import.v1" for operation in owners
    )
    maximum = (
        services.max_recipe_image_bytes if recipe_image else services.max_artifact_bytes
    )
    root_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(os.fspath(services.artifact_root), root_flags)
        try:
            if recipe_image:
                image_fd = os.open(IMAGE_CACHE_DIRECTORY, root_flags, dir_fd=root_fd)
                try:
                    descriptor = os.open(digest, file_flags, dir_fd=image_fd)
                finally:
                    os.close(image_fd)
            else:
                descriptor = os.open(digest, file_flags, dir_fd=root_fd)
        finally:
            os.close(root_fd)
    except OSError:
        raise HTTPException(status_code=404, detail="artifact not found") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise HTTPException(
                status_code=404 if not stat.S_ISREG(metadata.st_mode) else 413,
                detail="artifact not available",
            )
        return descriptor, metadata.st_size, maximum, recipe_image
    except Exception:
        os.close(descriptor)
        raise


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


def _read_chunks(descriptor: int, start: int, length: int):
    try:
        os.lseek(descriptor, start, os.SEEK_SET)
        remaining = length
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        os.close(descriptor)


def _sealed_snapshot(descriptor: int, size: int, maximum: int, digest: str):
    snapshot = None
    try:
        # Ownership transfers to _SnapshotResponse, which closes after send.
        snapshot = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
        copied = 0
        content_hash = hashlib.sha256()
        while copied < size:
            chunk = os.read(descriptor, min(64 * 1024, size - copied))
            if not chunk:
                raise HTTPException(
                    status_code=404, detail="artifact changed during read"
                )
            copied += len(chunk)
            if copied > maximum:
                raise HTTPException(status_code=413, detail="artifact not available")
            content_hash.update(chunk)
            snapshot.write(chunk)
        after = os.fstat(descriptor)
        if after.st_size != size or os.read(descriptor, 1):
            raise HTTPException(status_code=404, detail="artifact changed during read")
        if not hmac.compare_digest(content_hash.hexdigest(), digest):
            raise HTTPException(status_code=404, detail="artifact not found")
        snapshot.seek(0)
        return snapshot
    except Exception:
        if snapshot is not None:
            snapshot.close()
        raise
    finally:
        os.close(descriptor)


class _SnapshotResponse(StreamingResponse):
    def __init__(
        self,
        snapshot,
        start: int,
        length: int,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
    ) -> None:
        self._snapshot = snapshot
        super().__init__(
            self._chunks(start, length),
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )

    def _chunks(self, start: int, length: int):
        self._snapshot.seek(start)
        remaining = length
        while remaining:
            chunk = self._snapshot.read(min(64 * 1024, remaining))
            if not chunk:
                raise RuntimeError("sealed artifact snapshot was truncated")
            remaining -= len(chunk)
            yield chunk

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._snapshot.close()


def install_agent_routes(
    app: Any,
    *,
    services: AgentApiServices | None,
    enrollment_rate_limiter: EnrollmentRateLimiter | None = None,
) -> None:
    agent = APIRouter(prefix="/agent", route_class=ControllerAPIRoute)
    limiter = enrollment_rate_limiter or EnrollmentRateLimiter()

    @agent.get(
        "/bootstrap",
        response_model=EnrollmentBootstrapResponse,
        responses=bounded_error_responses(503),
    )
    def enrollment_bootstrap() -> Response:
        required = _require_services(services)
        if required.bootstrap is None:
            raise HTTPException(
                status_code=503,
                detail="agent enrollment bootstrap is unavailable",
            )
        if required.host_runtime_authority is None:
            raise HTTPException(
                status_code=503,
                detail="host runtime authority is unavailable",
            )
        helper_public_key = required.host_runtime_authority.public_key_document.get(
            "public_key"
        )
        if (
            not isinstance(helper_public_key, str)
            or re.fullmatch(r"[0-9a-f]{64}", helper_public_key) is None
        ):
            raise HTTPException(
                status_code=503,
                detail="host runtime authority is unavailable",
            )
        return _json_response(
            EnrollmentBootstrapResponse(
                controller_endpoint=required.bootstrap.controller_endpoint,
                enrollment_endpoint=required.bootstrap.enrollment_endpoint,
                ca_fingerprint=required.bootstrap.ca_fingerprint,
                ca_pem=required.bootstrap.ca_pem,
                controller_address=required.bootstrap.controller_address,
                service_hostnames=list(required.bootstrap.service_hostnames),
                host_helper_authority_public_key=helper_public_key,
            )
        )

    @agent.post("/enroll", response_model=IssuedCertificateResponse)
    @raw_json_body(EnrollmentSubmitRequest)
    async def enroll(request: Request) -> Response:
        required = _require_services(services)
        if not limiter.admit():
            raise HTTPException(
                status_code=429, detail="enrollment rate limit exceeded"
            )
        raw = await _bounded_enrollment_body(request, required)
        scan = _scan_enrollment_grants(raw)
        content_type = request.headers.get("content-type", "")
        if (
            re.fullmatch(
                r"application/json(?:\s*;\s*charset=(?:utf-8|utf8))?",
                content_type,
                re.IGNORECASE,
            )
            is None
        ):
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(
                status_code=415,
                detail="enrollment content type must be application/json",
            )
        try:
            body = json.loads(raw.decode("utf-8"))
        except (TypeError, UnicodeDecodeError, ValueError, RecursionError):
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(
                status_code=422, detail="enrollment request must be JSON"
            ) from None
        if not isinstance(body, dict):
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(
                status_code=422, detail="enrollment request must be a JSON object"
            )
        if scan.top_level_keys != 1:
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(status_code=422, detail="enrollment grant is ambiguous")
        try:
            submitted = EnrollmentSubmitRequest.model_validate(body)
        except ValidationError:
            _consume_enrollment_denial(required, scan.tokens)
            if scan.tokens:
                # Keep the enrollment oracle closed: a discoverable grant is
                # consumed and reported as denied even when the request shape
                # is malformed.  The canonical model handles valid requests;
                # this branch preserves the bounded burn-on-invalid policy.
                raise HTTPException(
                    status_code=403, detail="enrollment denied"
                ) from None
            raise HTTPException(
                status_code=422, detail="enrollment request is invalid"
            ) from None
        try:
            csr_bytes = submitted.csr.encode("ascii")
        except UnicodeEncodeError:
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(
                status_code=422, detail="CSR must be ASCII PEM"
            ) from None
        try:
            outcome = _require_enrollment(required).submit(
                submitted.grant_token, csr_bytes, submitted.evidence.model_dump()
            )
        except EnrollmentIssuanceUncertain as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except EnrollmentDenied as error:
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(status_code=403, detail=str(error)) from None
        return _json_response(_issued_response(outcome))

    @agent.post(
        "/claim",
        response_model=AgentClaim,
        responses={204: {"description": "No work available"}},
    )
    def claim(request: Request, body: ClaimRequest) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        source = _validated_authenticated_source(request, required, identity)
        try:
            result = required.operations.claim(
                identity.node_id,
                identity.certificate_serial,
                body.wait_seconds,
                runtime_identity=body.runtime_identity.model_dump(),
                preflight_fingerprint=body.preflight_fingerprint,
                hostname=body.hostname,
                source=source,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        encoded_claim = canonical_message(AgentClaim.model_validate(result))
        if len(encoded_claim) > MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES:
            raise HTTPException(status_code=500, detail="agent claim is too large")
        return Response(content=encoded_claim, media_type="application/json")

    @agent.post("/inventory", status_code=status.HTTP_204_NO_CONTENT)
    def inventory(body: InventoryRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        if body.observed_at.tzinfo is None or body.observed_at.utcoffset() is None:
            raise HTTPException(
                status_code=422, detail="inventory time must be timezone-aware"
            )
        observed_at = body.observed_at.astimezone(UTC)
        now = _now(required.clock()).astimezone(UTC)
        if (
            observed_at > now + MAX_INVENTORY_FUTURE_SKEW
            or now - observed_at > timedelta(hours=24)
        ):
            raise HTTPException(
                status_code=422, detail="inventory time is outside the accepted window"
            )
        if body.fabric_address is not None:
            if required.fabric_policy is None:
                raise HTTPException(
                    status_code=422, detail="direct fabric is not configured"
                )
            try:
                required.fabric_policy.validate(body.fabric_address)
            except PresenceError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
        try:
            InventoryRepository(required.sessions, clock=required.clock).record(
                InventorySnapshotInput(
                    node_id=identity.node_id,
                    observed_at=observed_at,
                    disk_total_bytes=body.disk_total_bytes,
                    disk_free_bytes=body.disk_free_bytes,
                    host_memory_total_bytes=body.host_memory_total_bytes,
                    host_memory_free_bytes=body.host_memory_free_bytes,
                    gpu_memory_total_bytes=body.gpu_memory_total_bytes,
                    gpu_memory_free_bytes=body.gpu_memory_free_bytes,
                    gpu_count=body.gpu_count,
                    memory_pool=body.memory_pool,
                    artifact_store_read_only=body.artifact_store_read_only,
                    capabilities=tuple(body.capabilities),
                    fabric_address=body.fabric_address,
                    fabric_bandwidth_mbps=body.fabric_bandwidth_mbps,
                    nvidia_driver_version=body.nvidia_driver_version,
                    container_runtime_version=body.container_runtime_version,
                )
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.post("/telemetry", status_code=status.HTTP_204_NO_CONTENT)
    def telemetry(body: TelemetryRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        try:
            TelemetryRepository(required.sessions, clock=required.clock).record_batch(
                identity.node_id,
                tuple(
                    TelemetrySampleInput(
                        boot_id=uuid.UUID(sample.boot_id),
                        observed_at=sample.observed_at,
                        memory_total_bytes=sample.memory_total_bytes,
                        memory_available_bytes=sample.memory_available_bytes,
                        disk_total_bytes=sample.disk_total_bytes,
                        disk_free_bytes=sample.disk_free_bytes,
                        gpu_utilization_percent=sample.gpu_utilization_percent,
                        gpu_memory_total_bytes=sample.gpu_memory_total_bytes,
                        gpu_memory_free_bytes=sample.gpu_memory_free_bytes,
                    )
                    for sample in body.samples
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.post("/recipe-runs/observations", status_code=status.HTTP_204_NO_CONTENT)
    def recipe_run_observations(
        body: RecipeRunObservationsWire, request: Request
    ) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        observed_at = body.observed_at.astimezone(UTC)
        now = _now(required.clock()).astimezone(UTC)
        if observed_at > now + timedelta(seconds=30) or now - observed_at > timedelta(
            minutes=5
        ):
            raise HTTPException(
                status_code=422,
                detail="recipe run observation time is outside the accepted window",
            )
        # One stale or no-longer-assigned run must not discard the evidence
        # for every other run in the report: each run is judged on its own.
        rejected: list[str] = []
        accepted = 0
        by_run = {run.run_id: run for run in body.runs}
        try:
            with required.sessions.begin() as session:
                included = set(by_run)
                if included:
                    current = set(
                        session.scalars(
                            select(RunNode.run_id)
                            .join(RecipeRun, RecipeRun.id == RunNode.run_id)
                            .where(
                                RunNode.node_id == identity.node_id,
                                RecipeRun.state == "running",
                            )
                        )
                    )
                    rejected.extend(
                        f"recipe run {run_id} observation is not assigned"
                        for run_id in sorted(included - current)
                    )
                    included &= current
                if by_run and not included:
                    raise ValueError("; ".join(rejected[:4]))
                assigned = prepare_exact_recipe_run_observation_nodes(
                    session, identity.node_id, observed_at, included
                )
                for node in assigned:
                    evidence = by_run.get(node.run_id)
                    if evidence is None or node.run_id not in included:
                        continue
                    run = session.get(RecipeRun, node.run_id)
                    assert run is not None
                    if evidence.run_generation != run.run_generation:
                        rejected.append("recipe run observation generation is stale")
                        continue
                    if node.state not in {"running", "failed"} or (
                        node.state == "failed" and run.route_state != "withdrawn"
                    ):
                        # The start or recovery operation owns this rank now.
                        continue
                    if _now(node.updated_at).astimezone(UTC) > observed_at:
                        rejected.append("recipe run observation is stale")
                        continue
                    accepted += 1
                    # The grace period bounds the first observation of a
                    # generation, not every later one.
                    initial_observation_late = (
                        node.observed_run_generation != run.run_generation
                        and run.observation_deadline_at is not None
                        and observed_at > _now(run.observation_deadline_at)
                    )
                    mapping = session.get(ClusterMapping, run.mapping_id)
                    owner = (
                        mapping is not None
                        and mapping.endpoint_owner_node_id == identity.node_id
                    )
                    if initial_observation_late:
                        # Late first evidence cannot make the run routable, but
                        # its process result remains valid evidence for
                        # Controller-owned recovery.
                        node.state = "failed"
                    elif node.state != "failed":
                        node.state = (
                            "running"
                            if evidence.process_running
                            and (not owner or evidence.endpoint_ready is True)
                            else "failed"
                        )
                    node.observed_run_generation = run.run_generation
                    node.observation_process_running = evidence.process_running
                    node.observation_observed_at = observed_at
                    node.observation_endpoint_ready = (
                        evidence.endpoint_ready if owner else None
                    )
                    node.updated_at = observed_at
                    if (
                        run.route_state == "withdrawn"
                        and run.route_next_attempt_at is not None
                    ):
                        run.route_next_attempt_at = None
                        run.updated_at = max(_now(run.updated_at).astimezone(UTC), now)
                if rejected and not accepted:
                    # Nothing in this report was usable; report the cause so
                    # the agent's log names it.  No state changed.
                    raise ValueError("; ".join(rejected[:4]))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        if rejected:
            logging.getLogger(__name__).warning(
                "agent.recipe_run_observations.partial node_id=%s accepted=%d "
                "rejected=%d first_reason=%s",
                identity.node_id,
                accepted,
                len(rejected),
                rejected[0],
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.get(
        "/recipe-runs/{run_id}/disposition",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses={
            204: {
                "description": (
                    "The run is known unless the disposition header names it unowned"
                ),
                "headers": {
                    RECIPE_RUN_DISPOSITION_HEADER: {
                        "description": "Present only as `unowned`.",
                        "schema": {"type": "string", "enum": [RECIPE_RUN_UNOWNED]},
                    }
                },
            }
        },
    )
    def recipe_run_disposition(run_id: str, request: Request) -> Response:
        """Say whether this Controller has any record of one local run.

        A Spark can retain a run this Controller never owned (for example
        after its database was rebuilt).  No observation of it is ever
        accepted, so the agent asks once and retires the local lifecycle
        instead of reporting it forever.
        """

        helper_identity(request)
        if _CANONICAL_UUID.fullmatch(run_id) is None:
            raise HTTPException(status_code=422, detail="recipe run id is invalid")
        with _require_services(services).sessions() as session:
            known = recipe_run_known(session, run_id)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        if not known:
            response.headers[RECIPE_RUN_DISPOSITION_HEADER] = RECIPE_RUN_UNOWNED
        return response

    @agent.get(
        "/source-bundles/{source_sha256}",
        response_class=Response,
        responses=download_responses("application/vnd.vonk-forge.source-bundle.v1+tar"),
        openapi_extra={"x-vonk-streaming-transport": True},
    )
    def source_bundle(source_sha256: str, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        if _DIGEST.fullmatch(source_sha256) is None:
            raise HTTPException(status_code=404, detail="source bundle does not exist")
        with required.sessions() as session:
            stored = session.get(RecipeSourceBundle, source_sha256)
            authorized = session.scalar(
                select(RecipeBuild.id).where(
                    RecipeBuild.builder_node_id == identity.node_id,
                    RecipeBuild.source_bundle_sha256 == source_sha256,
                    RecipeBuild.state.in_(("planned", "building")),
                )
            )
            if stored is None or authorized is None:
                raise HTTPException(
                    status_code=404, detail="source bundle does not exist"
                )
        try:
            bundle = required.source_bundles.get(source_sha256)
        except SourceBundleError:
            raise HTTPException(
                status_code=409, detail="source bundle storage is inconsistent"
            ) from None
        return Response(
            content=bundle.archive,
            media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
            headers={
                "etag": f'"sha256:{source_sha256}"',
                "cache-control": "private, immutable, max-age=31536000",
                "x-content-type-options": "nosniff",
            },
        )

    def helper_identity(request: Request) -> AgentIdentity:
        _scope_identity(request)
        required = _require_services(services)
        return _authenticated_identity(request, required)

    def host_runtime_service() -> HostRuntimeAuthorityService:
        required = services.host_runtime_authority if services is not None else None
        if required is None:
            raise HTTPException(
                status_code=503, detail="host runtime authority unavailable"
            )
        return required

    @agent.post("/host-runtime/grant", response_model=HostHelperGrantResponse)
    def host_runtime_grant(body: HostRuntimeGrantRequest, request: Request) -> Response:
        identity = helper_identity(request)
        required = host_runtime_service()
        try:
            grant = required.issue_grant(
                node_id=identity.node_id,
                fence=body.fence,
                action=ContainerRuntimeAction(body.action),
                request_sha256=body.request_sha256,
                start_plan_sha256=body.start_plan_sha256,
                stop_plan_sha256=body.stop_plan_sha256,
                run_generation=body.run_generation,
                runtime_run_id=body.runtime_run_id,
                runtime_target_id=body.runtime_target_id,
                runtime_installation_id=body.runtime_installation_id,
                installation_id=body.installation_id,
                reconciliation_identity=body.reconciliation_identity,
                certificate_serial=identity.certificate_serial,
                expires_in_seconds=body.expires_in_seconds,
            )
            return _json_response(_host_grant_response(grant))
        except (KeyError, TypeError, ValueError, HostHelperAuthorityError):
            raise HTTPException(
                status_code=409, detail="host runtime authority rejected request"
            ) from None

    @agent.post(
        "/agent-upgrade/activation-grant", response_model=HostHelperGrantResponse
    )
    def package_activation_grant(
        body: PackageActivationGrantRequest, request: Request
    ) -> Response:
        identity = helper_identity(request)
        try:
            grant = host_runtime_service().issue_package_activation_grant(
                node_id=identity.node_id,
                receipt=body.receipt,
                runtime_identity=body.runtime_identity,
                certificate_serial=identity.certificate_serial,
            )
            return _json_response(_host_grant_response(grant))
        except (KeyError, TypeError, ValueError, HostHelperAuthorityError):
            raise HTTPException(
                status_code=409, detail="package activation authority rejected request"
            ) from None

    @agent.post("/agent-upgrade/grant", response_model=HostHelperGrantResponse)
    def agent_upgrade_grant(
        body: AgentUpgradeGrantRequest, request: Request
    ) -> Response:
        identity = helper_identity(request)
        required = host_runtime_service()
        try:
            grant = required.issue_agent_upgrade_grant(
                node_id=identity.node_id,
                fence=body.fence,
                package_sha256=body.package_sha256,
                package_signature=body.package_signature,
                certificate_serial=identity.certificate_serial,
                expires_in_seconds=body.expires_in_seconds,
            )
            return _json_response(_host_grant_response(grant))
        except (KeyError, TypeError, ValueError, HostHelperAuthorityError):
            raise HTTPException(
                status_code=409, detail="agent upgrade authority rejected request"
            ) from None

    @agent.post("/heartbeat", response_model=AgentDirective)
    def heartbeat(body: AgentProgress, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        message = body
        source = _validated_authenticated_source(request, required, identity)
        try:
            response = required.operations.heartbeat(
                message,
                message.progress,
                CLAIM_LEASE_SECONDS,
                source=source,
            )
        except StaleAgentAttempt as error:
            if required.operations.known_superseded_cancellation(
                message, source=source
            ):
                logging.getLogger(__name__).info(
                    "ignored heartbeat for superseded cancelled fence %s",
                    message.fence,
                )
                raise HTTPException(
                    status_code=409,
                    detail="superseded operation was cancelled",
                    headers={"x-vonk-error-code": "superseded_operation_cancelled"},
                ) from None
            required.operations.record_boundary_refusal(
                str(message.fence),
                boundary="heartbeat",
                check="stale-attempt",
            )
            raise HTTPException(status_code=409, detail=str(error)) from None
        except ValueError as error:
            required.operations.record_boundary_refusal(
                str(message.fence),
                boundary="heartbeat",
                check="invalid-progress",
            )
            raise HTTPException(status_code=409, detail=str(error)) from None
        return _json_response(AgentDirective.model_validate(response))

    @agent.post("/result", status_code=status.HTTP_204_NO_CONTENT)
    def result(body: AgentResult, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        message = body
        source = _validated_authenticated_source(request, required, identity)
        try:
            # The failed-result identity rule (a failed status plus a stable
            # error code) is part of the operation result contract and is
            # applied by ``validate_result_for_operation`` inside
            # ``record_result``.  Keeping no second copy here is what makes the
            # producer and the ingress enforce exactly one rule.
            required.operations.record_result(message, source=source)
        except StaleAgentAttempt as error:
            try:
                required.operations.record_late_result(message, source=source)
            except StaleAgentAttempt:
                required.operations.record_boundary_refusal(
                    str(message.fence),
                    boundary="result",
                    check="stale-attempt",
                )
                raise HTTPException(status_code=409, detail=str(error)) from None
            except ValueError as invalid:
                required.operations.record_boundary_refusal(
                    str(message.fence),
                    boundary="result",
                    check="late-result-invalid",
                )
                raise HTTPException(status_code=422, detail=str(invalid)) from None
            return Response(status_code=status.HTTP_202_ACCEPTED)
        except ValueError as error:
            required.operations.record_boundary_refusal(
                str(message.fence),
                boundary="result",
                check="invalid-result",
            )
            raise HTTPException(status_code=422, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.post("/renew", response_model=IssuedCertificateResponse)
    def renew(body: RenewRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        _body_node_matches(body.node_id, identity)
        try:
            issued = _require_enrollment(required).renew(
                identity.node_id, identity.certificate_serial, body.csr.encode("ascii")
            )
        except UnicodeEncodeError:
            raise HTTPException(
                status_code=422, detail="CSR must be ASCII PEM"
            ) from None
        except RenewalInProgress as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        return _json_response(_issued_response(issued))

    @agent.post("/renew/recover", response_model=IssuedCertificateResponse)
    def recover_renewal(body: RenewRequest, request: Request) -> Response:
        """Recover a staged certificate that was created for another CSR.

        The active source identity is deliberately used for admission.  The
        Controller retires and CA-revokes the conflicting staged identity
        before issuing the durable pending CSR.
        """
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        _body_node_matches(body.node_id, identity)
        try:
            issued = _require_enrollment(required).recover_rotation(
                identity.node_id, identity.certificate_serial, body.csr.encode("ascii")
            )
        except UnicodeEncodeError:
            raise HTTPException(
                status_code=422, detail="CSR must be ASCII PEM"
            ) from None
        except RenewalInProgress as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        return _json_response(_issued_response(issued))

    @agent.post("/renew/activate", status_code=status.HTTP_204_NO_CONTENT)
    def activate(body: ActivateRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_activation_identity(request, required)
        _body_node_matches(body.node_id, identity)
        try:
            _require_enrollment(required).activate(
                identity.node_id,
                identity.certificate_serial,
                body.generation,
            )
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    def image_upload_context(build_id: str, request: Request):
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        try:
            uuid.UUID(build_id)
            headers = RecipeImageUploadHeaders.model_validate(
                {
                    "layout_sha256": request.headers.get(
                        "x-vonk-oci-layout-sha256", ""
                    ),
                    "image_digest": request.headers.get("x-vonk-image-digest", ""),
                    "image_bytes": request.headers.get("x-vonk-image-bytes", ""),
                    "offset": request.headers.get("x-vonk-upload-offset", "0"),
                }
            )
        except (ValueError, ValidationError):
            raise HTTPException(
                status_code=422, detail="image upload identity is invalid"
            ) from None
        if (
            headers.image_bytes > required.max_recipe_image_bytes
            or headers.offset > headers.image_bytes
        ):
            raise HTTPException(status_code=422, detail="image upload size is invalid")
        with required.sessions() as session:
            build = session.get(RecipeBuild, build_id)
            if (
                build is None
                or build.builder_node_id != identity.node_id
                or build.state != "building"
            ):
                raise HTTPException(
                    status_code=404, detail="recipe build does not exist"
                )
        key = hashlib.sha256(
            f"{identity.node_id}:{build_id}:{headers.image_digest}:{headers.layout_sha256}:{headers.image_bytes}".encode()
        ).hexdigest()
        return required, identity, headers, key

    upload_header_names = {
        "layout_sha256": "x-vonk-oci-layout-sha256",
        "image_digest": "x-vonk-image-digest",
        "image_bytes": "x-vonk-image-bytes",
        "offset": "x-vonk-upload-offset",
    }
    upload_schema = RecipeImageUploadHeaders.model_json_schema()
    upload_parameters = [
        {
            "in": "header",
            "name": name,
            "required": field in upload_schema["required"],
            "schema": upload_schema["properties"][field],
        }
        for field, name in upload_header_names.items()
    ]

    @agent.head(
        "/recipe-builds/{build_id}/image",
        response_class=Response,
        openapi_extra={"parameters": upload_parameters},
        responses={
            200: {
                "description": "Accepted archive cursor",
                "headers": {
                    name: {
                        "schema": RecipeImageUploadStatus.model_json_schema()[
                            "properties"
                        ][field]
                    }
                    for field, name in {
                        "offset": "x-vonk-upload-offset",
                        "complete": "x-vonk-upload-complete",
                    }.items()
                },
            }
        },
    )
    async def recipe_image_upload_status(build_id: str, request: Request) -> Response:
        required, _, headers, key = image_upload_context(build_id, request)
        with required.sessions() as session:
            build = session.get(RecipeBuild, build_id)
            if build is None:
                raise HTTPException(
                    status_code=404, detail="recipe build does not exist"
                )
            complete = (
                build.image_digest == headers.image_digest
                and build.oci_layout_sha256 == headers.layout_sha256
                and build.image_bytes == headers.image_bytes
            )
        destination = (
            required.artifact_root / IMAGE_CACHE_DIRECTORY / headers.layout_sha256
        )
        if (
            complete
            and destination.is_file()
            and destination.stat().st_size == headers.image_bytes
        ):
            return RecipeImageUploadStatus(
                offset=headers.image_bytes, complete=True
            ).response()
        descriptor, _temporary = await asyncio.to_thread(
            _prepare_recipe_image_upload,
            required.artifact_root / IMAGE_CACHE_DIRECTORY,
            key,
        )
        try:
            offset = os.fstat(descriptor).st_size
            return RecipeImageUploadStatus(offset=offset, complete=False).response()
        finally:
            os.close(descriptor)

    @agent.put(
        "/recipe-builds/{build_id}/image",
        status_code=status.HTTP_204_NO_CONTENT,
        openapi_extra=upload_request_body("application/x-tar")
        | {"parameters": upload_parameters},
    )
    async def upload_recipe_image(build_id: str, request: Request) -> Response:
        required, identity, headers, key = image_upload_context(build_id, request)
        media_type = request.headers.get("content-type", "").partition(";")[0].strip()
        if media_type.lower() != "application/x-tar":
            raise HTTPException(
                status_code=415, detail="Docker image archive media type is required"
            )
        try:
            content_length = int(request.headers.get("content-length", ""))
        except ValueError:
            raise HTTPException(
                status_code=411, detail="image length is required"
            ) from None
        if content_length != headers.image_bytes - headers.offset:
            raise HTTPException(
                status_code=422, detail="image upload length does not match cursor"
            )
        descriptor, temporary = await asyncio.to_thread(
            _prepare_recipe_image_upload,
            required.artifact_root / IMAGE_CACHE_DIRECTORY,
            key,
        )
        stream = os.fdopen(descriptor, "r+b")
        try:
            if os.fstat(descriptor).st_size != headers.offset:
                raise HTTPException(
                    status_code=409, detail="image upload cursor changed"
                )
            stream.seek(headers.offset)
            received = headers.offset
            async for chunk in request.stream():
                received += len(chunk)
                if received > headers.image_bytes:
                    raise HTTPException(
                        status_code=413, detail="recipe image is too large"
                    )
                await asyncio.to_thread(stream.write, chunk)
            # A truncated/interrupted transfer remains available to the next HEAD/PUT.
            await asyncio.to_thread(_flush_and_sync, stream)
            if received != headers.image_bytes:
                raise HTTPException(
                    status_code=422, detail="image upload is incomplete"
                )
            if (
                await asyncio.to_thread(_sha256_path, temporary, headers.image_bytes)
                != headers.layout_sha256
            ):
                await asyncio.to_thread(stream.truncate, 0)
                raise HTTPException(
                    status_code=422, detail="recipe image digest changed"
                )
            destination = (
                required.artifact_root / IMAGE_CACHE_DIRECTORY / headers.layout_sha256
            )
            with required.sessions.begin() as session:
                build = session.get(RecipeBuild, build_id, with_for_update=True)
                if (
                    build is None
                    or build.builder_node_id != identity.node_id
                    or build.state != "building"
                ):
                    raise HTTPException(
                        status_code=409, detail="recipe build authority changed"
                    )
                await asyncio.to_thread(
                    _commit_recipe_image_upload,
                    temporary,
                    destination,
                    expected_bytes=headers.image_bytes,
                    layout_sha256=headers.layout_sha256,
                )
                build.image_digest = headers.image_digest
                build.oci_layout_sha256 = headers.layout_sha256
                build.image_bytes = headers.image_bytes
                build.updated_at = _now(required.clock())
        finally:
            await asyncio.to_thread(stream.close)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.get(
        "/artifacts/{sha256}",
        response_class=Response,
        responses=download_responses("application/octet-stream", partial=True),
        openapi_extra={"x-vonk-streaming-transport": True},
    )
    def artifact(sha256: str, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        descriptor, size, maximum, recipe_image = _open_owned_artifact(
            required, identity, sha256
        )
        try:
            requested = _range(
                request.headers.get("range"), size, required.max_range_bytes
            )
        except Exception:
            os.close(descriptor)
            raise
        if requested is None:
            start, end, code = 0, size - 1, status.HTTP_200_OK
        else:
            start, end, code = (
                requested[0],
                requested[1],
                status.HTTP_206_PARTIAL_CONTENT,
            )
        length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "ETag": f'"sha256:{sha256}"',
        }
        if code == status.HTTP_206_PARTIAL_CONTENT:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if recipe_image:
            # Recipe images are verified while they enter the content-addressed
            # store and rehashed by the agent before Docker sees them. Reading
            # only the requested range keeps multi-gigabyte images resumable;
            # snapshotting and hashing the complete archive for every 8 MiB
            # request is quadratic and can exceed the agent's HTTP deadline
            # before the first response byte.
            return StreamingResponse(
                _read_chunks(descriptor, start, length),
                status_code=code,
                headers=headers,
                media_type="application/octet-stream",
            )
        snapshot = _sealed_snapshot(descriptor, size, maximum, sha256)
        return _SnapshotResponse(
            snapshot,
            start,
            length,
            status_code=code,
            headers=headers,
            media_type="application/octet-stream",
        )

    def _distribution_error(error: DistributionError) -> HTTPException:
        # Name the refusing check on the wire.  Without it the generic 403
        # boundary code is all the agent can report, so an authority denial
        # cannot be attributed to an assignment, node or expiry.
        headers = (
            {"x-vonk-error-code": error.code}
            if _DISTRIBUTION_ERROR_CODE.fullmatch(error.code)
            else {}
        )
        if error.code in {
            "distribution.unassigned",
            "distribution.wrong_node",
            "distribution.expired",
        }:
            return HTTPException(status_code=403, detail=error.detail, headers=headers)
        if error.code == "distribution.object_invalid":
            return HTTPException(status_code=404, detail=error.detail, headers=headers)
        return HTTPException(status_code=503, detail=error.detail, headers=headers)

    @agent.get(
        "/distribution/manifests/{plan_digest}",
        operation_id="getAgentDistributionManifest",
        response_model=DistributionAssignment,
    )
    def distribution_manifest(
        plan_digest: str, request: Request, response: Response
    ) -> DistributionAssignment:
        """Return the exact model plus OCI object set authorized for this node."""
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        if required.distribution is None:
            raise HTTPException(
                status_code=503, detail="agent distribution is unavailable"
            )
        try:
            assignment = required.distribution.authorize(
                node_id=identity.node_id,
                plan_digest=plan_digest,
            )
        except DistributionError as error:
            raise _distribution_error(error) from None
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = f'"plan:{plan_digest}"'
        return assignment.wire()

    @agent.get(
        "/distribution/objects/{sha256}",
        operation_id="downloadAgentDistributionObject",
        response_class=Response,
        responses=download_responses("application/octet-stream", partial=True),
        openapi_extra={"x-vonk-streaming-transport": True},
    )
    def distribution_object(sha256: str, request: Request) -> Response:
        """Stream one assigned immutable object with safe single-range resume."""
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        if required.distribution is None:
            raise HTTPException(
                status_code=503, detail="agent distribution is unavailable"
            )
        plan_digest = request.query_params.get("plan_digest")
        if plan_digest is None:
            raise HTTPException(status_code=403, detail="assignment is required")
        try:
            _assignment, object_spec, opened = required.distribution.open_object(
                node_id=identity.node_id,
                plan_digest=plan_digest,
                digest=sha256,
            )
        except DistributionError as error:
            raise _distribution_error(error) from None
        etag = f'"sha256:{object_spec.sha256}"'
        if_range = request.headers.get("if-range")
        requested_range = request.headers.get("range")
        # A mismatched If-Range deliberately degrades to a complete response,
        # allowing a client with an old checkpoint to safely restart.
        if requested_range is not None and if_range not in {
            None,
            etag,
            f"sha256:{object_spec.sha256}",
        }:
            requested_range = None
        try:
            selected = _range(requested_range, opened.size, required.max_range_bytes)
        except HTTPException:
            opened.stream.close()
            raise
        if selected is None:
            start, length, code = 0, opened.size, status.HTTP_200_OK
        else:
            start, end = selected
            length, code = end - start + 1, status.HTTP_206_PARTIAL_CONTENT
        if start:
            opened.stream.seek(start)

        def chunks():
            remaining = length
            try:
                while remaining:
                    # Each synchronous yield crosses Starlette's thread pool.
                    # MiB blocks keep bulk model copies efficient while
                    # bounding memory and honoring the exact selected range.
                    chunk = opened.stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError(
                            "verified object was truncated during transfer"
                        )
                    remaining -= len(chunk)
                    yield chunk
            finally:
                opened.stream.close()

        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Content-Length": str(length),
            "ETag": etag,
        }
        if code == status.HTTP_206_PARTIAL_CONTENT:
            headers["Content-Range"] = (
                f"bytes {start}-{start + length - 1}/{opened.size}"
            )
        return StreamingResponse(
            chunks(),
            status_code=code,
            headers=headers,
            media_type="application/octet-stream",
        )

    app.include_router(agent)

    # Enrollment reads a bounded raw body before validation so an invalid
    # submission still consumes its identifiable one-use grant. Document that
    # input from the very same model used above; a Request parameter alone
    # would otherwise hide the request contract from OpenAPI consumers.
    standard_openapi = app.openapi

    def openapi_with_enrollment_contract() -> dict[str, object]:
        document = standard_openapi()
        request_schema = EnrollmentSubmitRequest.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components = document.setdefault("components", {}).setdefault("schemas", {})
        components.update(request_schema.pop("$defs", {}))
        components[EnrollmentSubmitRequest.__name__] = request_schema
        document["paths"]["/agent/enroll"]["post"]["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/EnrollmentSubmitRequest"}
                }
            },
        }
        return document

    app.openapi = openapi_with_enrollment_contract
