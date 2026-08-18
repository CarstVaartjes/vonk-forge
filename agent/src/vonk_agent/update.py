"""Signed inactive-slot acquisition and supervisor activation requests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import struct
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from tuf.ngclient import FetcherInterface

from cluster_profiles.platform_release import PlatformRelease, PlatformReleaseError
from cluster_profiles.update_trust import UpdateTrust, UpdateTrustError

from .deadlines import DeadlineBindingError, MonotonicDeadline
from .oci import OCIError, ORASClient
from .releases import ReleaseDescriptor

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PREFIXED_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_SIGNATURE = re.compile(r"[0-9a-f]{128}\Z")
_TUF_METADATA_NAME = re.compile(
    r"(?:[1-9][0-9]*\.root|timestamp|snapshot|targets|"
    r"[a-z0-9][a-z0-9._-]{0,126})\.json\Z"
)
_MACHINE = {"linux-x86_64": 62, "linux-arm64": 183}
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_UPDATE_TIMEOUT_SECONDS = 300
_PLATFORM_RELEASE_TARGET = re.compile(
    r"platform/releases/"
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))/"
    r"(?P<sha256>[0-9a-f]{64})\.json\Z"
)
_SUPERVISOR_STATE_FIELDS = {
    "activation_deadline",
    "active_slot",
    "boot_attempts",
    "expected_sha256",
    "generation",
    "previous_slot",
    "rollback_performed",
    "schema_version",
    "slot_sha256",
    "status",
}


class AgentUpdateError(RuntimeError):
    """An agent update cannot proceed safely."""


def _platform_target_matches(name: object, version: object, sha256: object) -> bool:
    if not all(isinstance(value, str) for value in (name, version, sha256)):
        return False
    match = _PLATFORM_RELEASE_TARGET.fullmatch(name)
    return bool(
        match
        and match.group("version") == version
        and match.group("sha256") == sha256
    )


@dataclass(frozen=True)
class AgentArtifact:
    architecture: str
    oci_manifest_digest: str
    payload_name: str
    payload_sha256: str
    payload_size: int

    def __post_init__(self) -> None:
        if self.architecture not in _MACHINE:
            raise AgentUpdateError("agent artifact architecture is invalid")
        if _PREFIXED_DIGEST.fullmatch(self.oci_manifest_digest) is None:
            raise AgentUpdateError("agent artifact OCI digest is invalid")
        if _TOKEN.fullmatch(self.payload_name) is None:
            raise AgentUpdateError("agent payload name is invalid")
        if _DIGEST.fullmatch(self.payload_sha256) is None:
            raise AgentUpdateError("agent payload digest is invalid")
        if (
            isinstance(self.payload_size, bool)
            or not isinstance(self.payload_size, int)
            or not 64 <= self.payload_size <= _MAX_ARTIFACT_BYTES
        ):
            raise AgentUpdateError("agent payload size is invalid")


@dataclass(frozen=True)
class AgentReleaseIdentity:
    platform_version: str
    build_digest: str
    protocol_minimum: int
    protocol_maximum: int

    def __post_init__(self) -> None:
        if _SEMVER.fullmatch(self.platform_version) is None:
            raise AgentUpdateError("platform version is invalid")
        if _PREFIXED_DIGEST.fullmatch(self.build_digest) is None:
            raise AgentUpdateError("platform build digest is invalid")
        if (
            isinstance(self.protocol_minimum, bool)
            or isinstance(self.protocol_maximum, bool)
            or not isinstance(self.protocol_minimum, int)
            or not isinstance(self.protocol_maximum, int)
            or not 1 <= self.protocol_minimum <= self.protocol_maximum <= 65535
        ):
            raise AgentUpdateError("agent protocol range is invalid")


@dataclass(frozen=True)
class PlatformAuthorizationEvidence:
    target_sha256: str
    targets_version: int

    def __post_init__(self) -> None:
        if (
            _DIGEST.fullmatch(self.target_sha256) is None
            or isinstance(self.targets_version, bool)
            or not isinstance(self.targets_version, int)
            or not 1 <= self.targets_version <= 2_147_483_647
        ):
            raise AgentUpdateError("platform authorization evidence is invalid")


@dataclass(frozen=True)
class ActivationAuthorization:
    architecture: str
    oci_manifest_digest: str
    payload_name: str
    payload_sha256: str
    payload_size: int
    platform_version: str
    build_digest: str
    platform_target_name: str
    platform_target_sha256: str
    tuf_targets_version: int
    previous_slot: str
    previous_sha256: str
    target_slot: str
    node_id: str
    attempt: int
    claim_deadline: int
    previous_generation: int
    operation_id: str
    fence: str
    expires_at: int

    def __post_init__(self) -> None:
        if (
            self.architecture not in _MACHINE
            or _PREFIXED_DIGEST.fullmatch(self.oci_manifest_digest) is None
            or _TOKEN.fullmatch(self.payload_name) is None
            or _DIGEST.fullmatch(self.payload_sha256) is None
            or isinstance(self.payload_size, bool)
            or not isinstance(self.payload_size, int)
            or not 64 <= self.payload_size <= _MAX_ARTIFACT_BYTES
            or _SEMVER.fullmatch(self.platform_version) is None
            or _PREFIXED_DIGEST.fullmatch(self.build_digest) is None
            or _DIGEST.fullmatch(self.platform_target_sha256) is None
            or not _platform_target_matches(
                self.platform_target_name,
                self.platform_version,
                self.platform_target_sha256,
            )
            or isinstance(self.tuf_targets_version, bool)
            or not isinstance(self.tuf_targets_version, int)
            or not 1 <= self.tuf_targets_version <= 2_147_483_647
            or self.previous_slot not in {"A", "B"}
            or self.target_slot not in {"A", "B"}
            or self.previous_slot == self.target_slot
            or _DIGEST.fullmatch(self.previous_sha256) is None
            or re.fullmatch(r"spk_[0-9a-f]{32}", self.node_id) is None
            or isinstance(self.attempt, bool)
            or self.attempt != 1
            or isinstance(self.claim_deadline, bool)
            or not isinstance(self.claim_deadline, int)
            or isinstance(self.previous_generation, bool)
            or not isinstance(self.previous_generation, int)
            or not 1 <= self.previous_generation <= 999_999_999
            or self.claim_deadline != self.expires_at
            or _UUID.fullmatch(self.operation_id) is None
            or _UUID.fullmatch(self.fence) is None
            or isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, int)
            or not 1 <= self.expires_at <= 9_007_199_254_740_991
        ):
            raise AgentUpdateError("activation authorization is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "architecture": self.architecture,
            "attempt": self.attempt,
            "build_digest": self.build_digest,
            "claim_deadline": self.claim_deadline,
            "expires_at": self.expires_at,
            "fence": self.fence,
            "node_id": self.node_id,
            "oci_manifest_digest": self.oci_manifest_digest,
            "operation_id": self.operation_id,
            "payload_name": self.payload_name,
            "platform_target_name": self.platform_target_name,
            "platform_target_sha256": self.platform_target_sha256,
            "platform_version": self.platform_version,
            "previous_sha256": self.previous_sha256,
            "previous_generation": self.previous_generation,
            "previous_slot": self.previous_slot,
            "sha256": self.payload_sha256,
            "size": self.payload_size,
            "target_slot": self.target_slot,
            "tuf_targets_version": self.tuf_targets_version,
        }


@dataclass(frozen=True)
class AuthorizationSignature:
    key_id: str
    value: str
    algorithm: str = "ed25519"

    def __post_init__(self) -> None:
        if (
            self.algorithm != "ed25519"
            or _DIGEST.fullmatch(self.key_id) is None
            or _SIGNATURE.fullmatch(self.value) is None
        ):
            raise AgentUpdateError("activation authorization signature is invalid")

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SupervisorSlotState:
    active_slot: str
    previous_slot: str | None
    status: str
    slot_sha256: Mapping[str, str | None]

    def __post_init__(self) -> None:
        if self.active_slot not in {"A", "B"}:
            raise AgentUpdateError("supervisor active slot is invalid")
        if self.previous_slot is not None and self.previous_slot not in {"A", "B"}:
            raise AgentUpdateError("supervisor previous slot is invalid")
        if self.status not in {"stable", "pending"}:
            raise AgentUpdateError("supervisor status is invalid")
        if set(self.slot_sha256) != {"A", "B"}:
            raise AgentUpdateError("supervisor slot digests are invalid")
        for digest in self.slot_sha256.values():
            if digest is not None and _DIGEST.fullmatch(digest) is None:
                raise AgentUpdateError("supervisor slot digest is invalid")


@dataclass(frozen=True)
class UpdatePlan:
    artifact: AgentArtifact
    release: AgentReleaseIdentity
    previous_slot: str
    target_slot: str
    plan_digest: str
    authorization: ActivationAuthorization
    signature: AuthorizationSignature


@dataclass(frozen=True)
class PendingActivation:
    previous_slot: str
    target_slot: str
    artifact_sha256: str
    platform_version: str
    build_digest: str
    status: str = "pending-activation"

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "build_digest": self.build_digest,
            "platform_version": self.platform_version,
            "previous_slot": self.previous_slot,
            "status": self.status,
            "target_slot": self.target_slot,
        }


@dataclass(frozen=True)
class SupervisorActivationRequest:
    authorization: ActivationAuthorization
    signature: AuthorizationSignature

    def __post_init__(self) -> None:
        if type(self.authorization) is not ActivationAuthorization or type(
            self.signature
        ) is not AuthorizationSignature:
            raise AgentUpdateError("supervisor activation request is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "authorization": self.authorization.to_mapping(),
            "schema_version": 2,
            "signature": self.signature.to_mapping(),
        }


@dataclass(frozen=True)
class RollbackAuthorization:
    action: str
    node_id: str
    attempt: int
    claim_deadline: int
    current_generation: int
    current_slot: str
    current_sha256: str
    operation_id: str
    fence: str
    expires_at: int

    def __post_init__(self) -> None:
        if (
            self.action != "operator-rollback"
            or re.fullmatch(r"spk_[0-9a-f]{32}", self.node_id) is None
            or isinstance(self.attempt, bool)
            or self.attempt != 1
            or isinstance(self.claim_deadline, bool)
            or not isinstance(self.claim_deadline, int)
            or self.claim_deadline != self.expires_at
            or isinstance(self.current_generation, bool)
            or not isinstance(self.current_generation, int)
            or not 1 <= self.current_generation <= 999_999_999
            or self.current_slot not in {"A", "B"}
            or _DIGEST.fullmatch(self.current_sha256) is None
            or _UUID.fullmatch(self.operation_id) is None
            or _UUID.fullmatch(self.fence) is None
            or isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, int)
            or not 1 <= self.expires_at <= 9_007_199_254_740_991
        ):
            raise AgentUpdateError("rollback authorization is invalid")

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SupervisorRollbackRequest:
    authorization: RollbackAuthorization
    signature: AuthorizationSignature

    def to_mapping(self) -> dict[str, object]:
        return {
            "authorization": self.authorization.to_mapping(),
            "schema_version": 2,
            "signature": self.signature.to_mapping(),
        }


@dataclass(frozen=True)
class AgentUpdateCommand:
    artifact: AgentArtifact
    release: AgentReleaseIdentity
    authorization: ActivationAuthorization
    signature: AuthorizationSignature

    @classmethod
    def parse(cls, value: Mapping[str, object]) -> AgentUpdateCommand:
        if not isinstance(value, Mapping) or set(value) != {
            "artifact",
            "receipt",
            "release",
            "signature",
        }:
            raise AgentUpdateError("agent update payload fields are invalid")
        artifact = value["artifact"]
        authorization = value["receipt"]
        release = value["release"]
        signature = value["signature"]
        if not isinstance(artifact, Mapping) or set(artifact) != {
            "architecture",
            "oci_manifest_digest",
            "payload_name",
            "payload_sha256",
            "payload_size",
        }:
            raise AgentUpdateError("agent artifact payload is invalid")
        if not isinstance(release, Mapping) or set(release) != {
            "build_digest",
            "platform_version",
            "protocol_maximum",
            "protocol_minimum",
        }:
            raise AgentUpdateError("agent release payload is invalid")
        if not isinstance(authorization, Mapping) or set(authorization) != {
            "architecture",
            "attempt",
            "build_digest",
            "claim_deadline",
            "expires_at",
            "fence",
            "node_id",
            "oci_manifest_digest",
            "operation_id",
            "payload_name",
            "platform_target_name",
            "platform_target_sha256",
            "platform_version",
            "previous_sha256",
            "previous_generation",
            "previous_slot",
            "sha256",
            "size",
            "target_slot",
            "tuf_targets_version",
        }:
            raise AgentUpdateError("agent activation authorization is invalid")
        if not isinstance(signature, Mapping) or set(signature) != {
            "algorithm",
            "key_id",
            "value",
        }:
            raise AgentUpdateError("agent activation signature is invalid")
        try:
            return cls(
                artifact=AgentArtifact(**dict(artifact)),
                release=AgentReleaseIdentity(**dict(release)),
                authorization=ActivationAuthorization(
                    architecture=authorization["architecture"],
                    oci_manifest_digest=authorization["oci_manifest_digest"],
                    payload_name=authorization["payload_name"],
                    payload_sha256=authorization["sha256"],
                    payload_size=authorization["size"],
                    platform_version=authorization["platform_version"],
                    build_digest=authorization["build_digest"],
                    platform_target_name=authorization["platform_target_name"],
                    platform_target_sha256=authorization["platform_target_sha256"],
                    tuf_targets_version=authorization["tuf_targets_version"],
                    previous_slot=authorization["previous_slot"],
                    previous_sha256=authorization["previous_sha256"],
                    target_slot=authorization["target_slot"],
                    node_id=authorization["node_id"],
                    attempt=authorization["attempt"],
                    claim_deadline=authorization["claim_deadline"],
                    previous_generation=authorization["previous_generation"],
                    operation_id=authorization["operation_id"],
                    fence=authorization["fence"],
                    expires_at=authorization["expires_at"],
                ),
                signature=AuthorizationSignature(**dict(signature)),
            )
        except TypeError as error:
            raise AgentUpdateError("agent update payload types are invalid") from error


@dataclass(frozen=True)
class AgentRollbackCommand:
    authorization: RollbackAuthorization
    signature: AuthorizationSignature

    @classmethod
    def parse(cls, value: Mapping[str, object]) -> AgentRollbackCommand:
        if not isinstance(value, Mapping) or set(value) != {"receipt", "signature"}:
            raise AgentUpdateError("agent rollback payload fields are invalid")
        receipt = value["receipt"]
        signature = value["signature"]
        if not isinstance(receipt, Mapping) or set(receipt) != {
            "action",
            "attempt",
            "claim_deadline",
            "current_generation",
            "current_sha256",
            "current_slot",
            "expires_at",
            "fence",
            "node_id",
            "operation_id",
        }:
            raise AgentUpdateError("agent rollback authorization is invalid")
        if not isinstance(signature, Mapping) or set(signature) != {
            "algorithm",
            "key_id",
            "value",
        }:
            raise AgentUpdateError("agent rollback signature is invalid")
        try:
            return cls(
                authorization=RollbackAuthorization(**dict(receipt)),
                signature=AuthorizationSignature(**dict(signature)),
            )
        except TypeError as error:
            raise AgentUpdateError("agent rollback payload types are invalid") from error


class UpdateTrustBoundary(Protocol):
    def authorize(
        self,
        artifact: AgentArtifact,
        release: AgentReleaseIdentity,
        platform_target_name: str,
        deadline: MonotonicDeadline,
    ) -> PlatformAuthorizationEvidence: ...


class UpdateTransportBoundary(Protocol):
    def fetch(
        self,
        artifact: AgentArtifact,
        destination: Path,
        deadline: MonotonicDeadline,
    ) -> None: ...


class UpdateDeadlineBoundary(Protocol):
    def set_deadline(self, deadline: MonotonicDeadline) -> None: ...


class AgentTUFRouteBoundary(UpdateDeadlineBoundary, Protocol):
    def fetch(self, url: str) -> Iterator[bytes]: ...


class PlatformTUFRouteFetcher(FetcherInterface):
    """Map platform-TUF URLs onto the fixed authenticated agent routes."""

    def __init__(
        self, delegate: AgentTUFRouteBoundary, *, control_origin: str
    ) -> None:
        parsed = urlsplit(control_origin)
        try:
            port = parsed.port
        except ValueError as error:
            raise AgentUpdateError("platform TUF control origin is invalid") from error
        origin = f"https://{parsed.hostname or ''}"
        if port is not None:
            origin += f":{port}"
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or origin != control_origin
        ):
            raise AgentUpdateError("platform TUF control origin is invalid")
        self._delegate = delegate
        self._origin = origin

    def set_deadline(self, deadline: MonotonicDeadline) -> None:
        self._delegate.set_deadline(deadline)

    def _fetch(self, url: str) -> Iterator[bytes]:
        parsed = urlsplit(url)
        if (
            f"{parsed.scheme}://{parsed.netloc}" != self._origin
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise AgentUpdateError("platform TUF route is invalid")
        routes = {
            "/platform/metadata/": (
                "/agent/v1/tuf/metadata/",
                _TUF_METADATA_NAME,
            ),
            "/platform/targets/": (
                "/agent/v1/tuf/targets/",
                _PLATFORM_RELEASE_TARGET,
            ),
        }
        for source, (target, pattern) in routes.items():
            if parsed.path.startswith(source):
                name = parsed.path[len(source) :]
                if pattern.fullmatch(name) is None:
                    break
                yield from self._delegate.fetch(self._origin + target + name)
                return
        raise AgentUpdateError("platform TUF route is invalid")


class PlatformAgentTrust:
    """Authorize an agent artifact through one immutable platform TUF target."""

    def __init__(
        self, trust: UpdateTrust, deadline_setter: UpdateDeadlineBoundary
    ) -> None:
        self._trust = trust
        self._deadline_setter = deadline_setter

    def authorize(
        self,
        artifact: AgentArtifact,
        release: AgentReleaseIdentity,
        platform_target_name: str,
        deadline: MonotonicDeadline,
    ) -> PlatformAuthorizationEvidence:
        target_match = (
            _PLATFORM_RELEASE_TARGET.fullmatch(platform_target_name)
            if isinstance(platform_target_name, str)
            else None
        )
        if (
            target_match is None
            or target_match.group("version") != release.platform_version
        ):
            raise AgentUpdateError("platform release target identity is invalid")
        try:
            deadline.check()
            self._deadline_setter.set_deadline(deadline)
            target, targets_version = self._trust.refresh_and_trusted_target(
                platform_target_name
            )
            deadline.check()
            if (
                target.name != platform_target_name
                or target.length != len(target.data)
                or target.sha256 != hashlib.sha256(target.data).hexdigest()
                or target.sha256 != target_match.group("sha256")
            ):
                raise AgentUpdateError("platform release target is inconsistent")
            manifest = PlatformRelease.from_bytes(target.data)
            published = manifest.agent_for(artifact.architecture)
        except AgentUpdateError:
            raise
        except (
            DeadlineBindingError,
            UpdateTrustError,
            PlatformReleaseError,
            RuntimeError,
            ValueError,
        ) as error:
            raise AgentUpdateError("platform release authorization failed") from error
        reference_digest = published.artifact.reference.rsplit("@", 1)[-1]
        protocol = published.protocol
        if (
            manifest.platform_version != release.platform_version
            or manifest.build_digest != release.build_digest
            or reference_digest != artifact.oci_manifest_digest
            or published.payload_name != artifact.payload_name
            or published.payload_sha256 != artifact.payload_sha256
            or published.payload_size != artifact.payload_size
            or protocol is None
            or protocol.minimum != release.protocol_minimum
            or protocol.maximum != release.protocol_maximum
        ):
            raise AgentUpdateError("agent artifact disagrees with signed platform release")
        return PlatformAuthorizationEvidence(target.sha256, targets_version)


class ORASAgentTransport:
    """Pull one platform-agent OCI artifact through the reviewed ORAS boundary."""

    def __init__(
        self,
        client: ORASClient,
        *,
        registry_origin: str,
        repository: str,
        architecture: str,
    ) -> None:
        self._client = client
        self._registry_origin = registry_origin
        self._repository = repository
        self._architecture = architecture

    def fetch(
        self,
        artifact: AgentArtifact,
        destination: Path,
        deadline: MonotonicDeadline,
    ) -> None:
        root = Path(
            tempfile.mkdtemp(prefix=".agent-oci-", dir=Path(destination).parent)
        )
        descriptor = ReleaseDescriptor(
            schema_version=1,
            target_name=artifact.payload_name,
            target_digest=artifact.payload_sha256,
            target_length=artifact.payload_size,
            registry_origin=self._registry_origin,
            repository=self._repository,
            oci_manifest_digest=artifact.oci_manifest_digest,
            provenance_digest="0" * 64,
            adapter_id="platform-agent",
            adapter_version="1.0.0",
            architecture=self._architecture,
            agent_min_version="0.1.0",
            agent_max_version="0.1.0",
            protocol_min_version=1,
            protocol_max_version=1,
            members=(),
        )
        try:
            deadline.check()
            self._client.pull(descriptor, root, deadline)
            deadline.check()
            members = list(root.iterdir())
            payload = root / artifact.payload_name
            if (
                len(members) != 1
                or members[0] != payload
                or payload.is_symlink()
                or not payload.is_file()
            ):
                raise AgentUpdateError("agent OCI artifact layout is invalid")
            os.replace(payload, destination)
        except AgentUpdateError:
            raise
        except (DeadlineBindingError, OCIError, OSError) as error:
            raise AgentUpdateError("agent OCI artifact pull failed") from error
        finally:
            shutil.rmtree(root, ignore_errors=True)


class SupervisorBoundary(Protocol):
    def inspect(self) -> SupervisorSlotState: ...

    def request_activation(self, request: SupervisorActivationRequest) -> None: ...

    def request_rollback(
        self, request: SupervisorRollbackRequest
    ) -> PendingActivation: ...


class AgentUpdater:
    def __init__(
        self,
        *,
        architecture: str,
        protocol_version: int,
        staging_root: Path,
        trust: UpdateTrustBoundary,
        transport: UpdateTransportBoundary,
        supervisor: SupervisorBoundary,
        available_bytes: Callable[[], int],
    ) -> None:
        if architecture not in _MACHINE:
            raise AgentUpdateError("local architecture is unsupported")
        if isinstance(protocol_version, bool) or not 1 <= protocol_version <= 65535:
            raise AgentUpdateError("local protocol version is invalid")
        self._architecture = architecture
        self._protocol_version = protocol_version
        self._staging_root = Path(staging_root)
        self._trust = trust
        self._transport = transport
        self._supervisor = supervisor
        self._available_bytes = available_bytes

    def plan(
        self,
        artifact: AgentArtifact,
        release: AgentReleaseIdentity,
        authorization: ActivationAuthorization,
        signature: AuthorizationSignature,
        deadline: datetime | MonotonicDeadline | None = None,
    ) -> UpdatePlan:
        fixed_deadline = _update_deadline(deadline)
        fixed_deadline.check()
        if artifact.architecture != self._architecture:
            raise AgentUpdateError("agent artifact architecture is incompatible")
        if not release.protocol_minimum <= self._protocol_version <= release.protocol_maximum:
            raise AgentUpdateError("agent protocol is incompatible")
        state = self._supervisor.inspect()
        if state.status != "stable":
            raise AgentUpdateError("supervisor must be stable before update")
        active_digest = state.slot_sha256[state.active_slot]
        if active_digest is None:
            raise AgentUpdateError("active supervisor slot is unavailable")
        available = self._available_bytes()
        if (
            isinstance(available, bool)
            or not isinstance(available, int)
            or available < artifact.payload_size * 2
        ):
            raise AgentUpdateError("insufficient disk space for agent update")
        evidence = self._trust.authorize(
            artifact,
            release,
            authorization.platform_target_name,
            fixed_deadline,
        )
        fixed_deadline.check()
        target = "B" if state.active_slot == "A" else "A"
        if (
            authorization.architecture != artifact.architecture
            or authorization.oci_manifest_digest != artifact.oci_manifest_digest
            or authorization.payload_name != artifact.payload_name
            or authorization.payload_sha256 != artifact.payload_sha256
            or authorization.payload_size != artifact.payload_size
            or authorization.platform_version != release.platform_version
            or authorization.build_digest != release.build_digest
            or authorization.platform_target_sha256 != evidence.target_sha256
            or authorization.tuf_targets_version != evidence.targets_version
            or authorization.previous_slot != state.active_slot
            or authorization.previous_sha256 != active_digest
            or authorization.target_slot != target
        ):
            raise AgentUpdateError(
                "activation authorization disagrees with signed update plan"
            )
        if authorization.expires_at <= int(datetime.now(UTC).timestamp()):
            raise AgentUpdateError("activation authorization has expired")
        content = {
            "artifact": asdict(artifact),
            "authorization": authorization.to_mapping(),
            "previous_slot": state.active_slot,
            "release": asdict(release),
            "signature": signature.to_mapping(),
            "target_slot": target,
        }
        digest = hashlib.sha256(_canonical(content)).hexdigest()
        return UpdatePlan(
            artifact,
            release,
            state.active_slot,
            target,
            f"sha256:{digest}",
            authorization,
            signature,
        )

    def apply(
        self,
        plan: UpdatePlan,
        deadline: datetime | MonotonicDeadline | None = None,
    ) -> PendingActivation:
        fixed_deadline = _update_deadline(deadline)
        if plan != self.plan(
            plan.artifact,
            plan.release,
            plan.authorization,
            plan.signature,
            fixed_deadline,
        ):
            raise AgentUpdateError("agent update plan is stale")
        _secure_directory(self._staging_root)
        final = self._staging_root / f"{plan.artifact.payload_sha256}.agent"
        temporary = self._staging_root / f".{plan.artifact.payload_sha256}.{secrets.token_hex(8)}.partial"
        try:
            if not final.exists():
                self._transport.fetch(plan.artifact, temporary, fixed_deadline)
                fixed_deadline.check()
                _verify_artifact(temporary, plan.artifact, self._architecture)
                os.chmod(temporary, 0o500)
                descriptor = os.open(temporary, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(temporary, final)
                _fsync_directory(self._staging_root)
            else:
                _verify_artifact(final, plan.artifact, self._architecture)
            fixed_deadline.check()
            request = SupervisorActivationRequest(
                authorization=plan.authorization,
                signature=plan.signature,
            )
            self._supervisor.request_activation(request)
            return PendingActivation(
                previous_slot=plan.previous_slot,
                target_slot=plan.target_slot,
                artifact_sha256=plan.artifact.payload_sha256,
                platform_version=plan.release.platform_version,
                build_digest=plan.release.build_digest,
            )
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def execute(
        self,
        command: AgentUpdateCommand,
        deadline: MonotonicDeadline,
        operation_id: str,
        fence: str,
    ) -> dict[str, object]:
        if (
            command.authorization.operation_id != operation_id
            or command.authorization.fence != fence
        ):
            raise AgentUpdateError(
                "activation authorization does not match the claimed operation fence"
            )
        return self.apply(
            self.plan(
                command.artifact,
                command.release,
                command.authorization,
                command.signature,
                deadline,
            ),
            deadline,
        ).to_mapping()

    def rollback(
        self,
        command: AgentRollbackCommand,
        deadline: MonotonicDeadline,
        operation_id: str,
        fence: str,
    ) -> dict[str, object]:
        if (
            command.authorization.operation_id != operation_id
            or command.authorization.fence != fence
        ):
            raise AgentUpdateError(
                "rollback authorization does not match the claimed operation fence"
            )
        fixed_deadline = _update_deadline(deadline)
        fixed_deadline.check()
        pending = self._supervisor.request_rollback(
            SupervisorRollbackRequest(command.authorization, command.signature)
        )
        fixed_deadline.check()
        return pending.to_mapping()


class LocalSupervisor:
    """Unprivileged typed boundary to the stable root-owned supervisor."""

    def __init__(
        self,
        *,
        state_path: Path = Path(
            "/var/lib/vonk-forge-agent-supervisor/state.json"
        ),
        runtime_root: Path = Path("/run/vonk-forge-agent"),
        slot_root: Path = Path("/opt/vonk-forge/agent-slots"),
    ) -> None:
        self._state_path = Path(state_path)
        self._runtime_root = Path(runtime_root)
        self._slot_root = Path(slot_root)
        if not all(
            path.is_absolute()
            for path in (self._state_path, self._runtime_root, self._slot_root)
        ):
            raise AgentUpdateError("supervisor paths must be absolute")

    def inspect(self) -> SupervisorSlotState:
        document = _read_supervisor_json(
            self._state_path, mode=0o644, owner=None, maximum=16 * 1024
        )
        if set(document) != _SUPERVISOR_STATE_FIELDS:
            raise AgentUpdateError("supervisor state fields are invalid")
        try:
            return SupervisorSlotState(
                active_slot=document["active_slot"],
                previous_slot=document["previous_slot"],
                status=document["status"],
                slot_sha256=document["slot_sha256"],
            )
        except (KeyError, TypeError) as error:
            raise AgentUpdateError("supervisor state is invalid") from error

    def request_activation(self, request: SupervisorActivationRequest) -> None:
        if type(request) is not SupervisorActivationRequest:
            raise AgentUpdateError("activation request type is invalid")
        _secure_directory(self._runtime_root)
        _write_atomic(
            self._runtime_root / "activation-request.json",
            _canonical(request.to_mapping()),
        )

    def request_rollback(
        self, request: SupervisorRollbackRequest
    ) -> PendingActivation:
        if type(request) is not SupervisorRollbackRequest:
            raise AgentUpdateError("rollback request type is invalid")
        state = self.inspect()
        previous = state.previous_slot
        if (
            state.status != "stable"
            or previous not in {"A", "B"}
            or previous == state.active_slot
        ):
            raise AgentUpdateError("no stable previous agent slot is available")
        current_digest = state.slot_sha256[state.active_slot]
        previous_digest = state.slot_sha256[previous]
        if current_digest is None or previous_digest is None:
            raise AgentUpdateError("rollback slot digest is unavailable")
        if (
            request.authorization.current_slot != state.active_slot
            or request.authorization.current_sha256 != current_digest
        ):
            raise AgentUpdateError(
                "rollback authorization does not match supervisor state"
            )
        identity_path = self._slot_root / previous / "identity.json"
        identity = _read_supervisor_json(
            identity_path, mode=0o444, owner=None, maximum=4096
        )
        if (
            set(identity)
            != {"build_digest", "platform_version", "schema_version", "sha256"}
            or identity.get("schema_version") != 1
            or identity.get("sha256") != previous_digest
            or not isinstance(identity.get("platform_version"), str)
            or _SEMVER.fullmatch(identity["platform_version"]) is None
            or not isinstance(identity.get("build_digest"), str)
            or _PREFIXED_DIGEST.fullmatch(identity["build_digest"]) is None
        ):
            raise AgentUpdateError("rollback slot identity is invalid")
        _secure_directory(self._runtime_root)
        _write_atomic(
            self._runtime_root / "rollback-request.json",
            _canonical(request.to_mapping()),
        )
        return PendingActivation(
            previous_slot=state.active_slot,
            target_slot=previous,
            artifact_sha256=previous_digest,
            platform_version=identity["platform_version"],
            build_digest=identity["build_digest"],
            status="pending-rollback",
        )


def _verify_artifact(path: Path, artifact: AgentArtifact, architecture: str) -> None:
    try:
        if path.is_symlink():
            raise AgentUpdateError("agent artifact staging path is unsafe")
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != artifact.payload_size
        ):
            raise AgentUpdateError("agent artifact size or type is invalid")
        content = path.read_bytes()
    except OSError as error:
        raise AgentUpdateError("agent artifact is unavailable") from error
    if hashlib.sha256(content).hexdigest() != artifact.payload_sha256:
        raise AgentUpdateError("agent artifact digest is invalid")
    if len(content) < 20 or content[:7] != b"\x7fELF\x02\x01\x01":
        raise AgentUpdateError("agent artifact is not a supported ELF")
    elf_type, machine = struct.unpack_from("<HH", content, 16)
    if elf_type not in {2, 3} or machine != _MACHINE[architecture]:
        raise AgentUpdateError("agent artifact ELF architecture is incompatible")


def _update_deadline(
    value: datetime | MonotonicDeadline | None,
) -> MonotonicDeadline:
    candidate = (
        datetime.now(UTC) + timedelta(seconds=_UPDATE_TIMEOUT_SECONDS)
        if value is None
        else value
    )
    try:
        deadline = MonotonicDeadline.bind(candidate)
        deadline.check()
        return deadline
    except DeadlineBindingError as error:
        raise AgentUpdateError("agent update deadline has elapsed") from error


def _read_supervisor_json(
    path: Path, *, mode: int, owner: int | None, maximum: int
) -> dict[str, object]:
    descriptor = -1
    try:
        if path.is_symlink():
            raise AgentUpdateError("supervisor metadata path is unsafe")
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        metadata = os.fstat(descriptor)
        allowed_owners = {0, os.geteuid()} if owner is None else {owner}
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in allowed_owners
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size > maximum
        ):
            raise AgentUpdateError("supervisor metadata path is unsafe")
        raw = os.read(descriptor, maximum + 1)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json)
        if not isinstance(value, dict) or _canonical(value) != raw:
            raise AgentUpdateError("supervisor metadata is not canonical")
        return value
    except AgentUpdateError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentUpdateError("supervisor metadata is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unique_json(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AgentUpdateError("supervisor metadata contains duplicate fields")
        result[key] = value
    return result


def _secure_directory(path: Path) -> None:
    if not path.is_absolute():
        raise AgentUpdateError("agent update path must be absolute")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise AgentUpdateError("agent update directory is unsafe") from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise AgentUpdateError("agent update directory is unsafe")
    os.chmod(path, 0o700)


def _write_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.new")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
