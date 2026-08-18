"""Recoverable host-local control-plane generation upgrades."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from cluster_profiles.platform_release import (
    OciDeploymentBundle,
    PlatformRelease,
    PlatformReleaseError,
)

from .host_state import (
    HostGenerationStore,
    HostOperationLock,
    HostStateConflict,
    JournalState,
    PhaseJournal,
)

_GENERATION = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_VERSIONED_TARGET = re.compile(
    r"platform/releases/"
    r"(?P<version>(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))/"
    r"(?P<sha256>[0-9a-f]{64})\.json\Z"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"[^\s]{1,1900}@sha256:[0-9a-f]{64}\Z")
_MAX_ACTIVE_POINTER = 256
_MAX_GENERATION_RECEIPT = 64 * 1024
_MAX_RENDERED_COMPOSE = 1024 * 1024


class UpgradeError(RuntimeError):
    """A control generation upgrade failed safely."""


class UpgradeConflict(UpgradeError):
    """The upgrade cannot start because its plan or host state is stale."""


class UpgradeReadinessError(UpgradeError):
    """The candidate control API did not become ready."""


class AmbiguousMigrationError(UpgradeError):
    """The database migration outcome cannot be determined automatically."""


class UpgradeRecoveryRequired(UpgradeError):
    """Automatic rollback is unsafe and an administrator must recover."""


class ProbeDisposition(StrEnum):
    """Result of independently observing one durable upgrade effect."""

    EXACT = "exact"
    ABSENT = "absent"
    PARTIAL = "partial"
    CONFLICT = "conflict"
    AMBIGUOUS = "ambiguous"


class UpgradePhase(StrEnum):
    """Canonical apply phases persisted in the root operation journal."""

    AUTHORIZED = "authorized"
    BUNDLE_IMAGES_ACQUIRED = "bundle-images-acquired"
    GENERATION_STAGED = "generation-staged"
    BACKUP_COMPLETED = "backup-completed"
    SERVICES_STOPPED_DATABASE_MIGRATED = "services-stopped-database-migrated"
    CANDIDATE_READY = "candidate-ready"
    GENERATION_COMMITTED = "generation-committed"
    GENERATION_SELECTED = "generation-selected"
    SERVICES_STARTED = "services-started"
    WORKER_READY = "worker-ready"
    COMPLETED = "completed"
    PREDECESSOR_VERIFIED = "predecessor-verified"
    SERVICES_STOPPED = "services-stopped"
    ROLLED_BACK = "rolled-back"
    COMPENSATION_STARTED = "compensation-started"
    COMPENSATION_SERVICES_STOPPED = "compensation-services-stopped"
    BACKUP_RESTORED = "backup-restored"
    PREDECESSOR_SELECTED = "predecessor-selected"
    PREDECESSOR_SERVICES_STARTED = "predecessor-services-started"
    PREDECESSOR_WORKER_READY = "predecessor-worker-ready"
    CANDIDATE_CLEANED = "candidate-cleaned"
    FAILED = "failed"


_APPLY_PHASES = (
    UpgradePhase.AUTHORIZED,
    UpgradePhase.BUNDLE_IMAGES_ACQUIRED,
    UpgradePhase.GENERATION_STAGED,
    UpgradePhase.BACKUP_COMPLETED,
    UpgradePhase.SERVICES_STOPPED_DATABASE_MIGRATED,
    UpgradePhase.CANDIDATE_READY,
    UpgradePhase.GENERATION_COMMITTED,
    UpgradePhase.GENERATION_SELECTED,
    UpgradePhase.SERVICES_STARTED,
    UpgradePhase.WORKER_READY,
    UpgradePhase.COMPLETED,
)

_ROLLBACK_PHASES = (
    UpgradePhase.AUTHORIZED,
    UpgradePhase.PREDECESSOR_VERIFIED,
    UpgradePhase.SERVICES_STOPPED,
    UpgradePhase.GENERATION_SELECTED,
    UpgradePhase.SERVICES_STARTED,
    UpgradePhase.WORKER_READY,
    UpgradePhase.ROLLED_BACK,
)

_DESTRUCTIVE_COMPENSATION_PHASES = (
    UpgradePhase.COMPENSATION_STARTED,
    UpgradePhase.COMPENSATION_SERVICES_STOPPED,
    UpgradePhase.BACKUP_RESTORED,
    UpgradePhase.PREDECESSOR_SELECTED,
    UpgradePhase.PREDECESSOR_SERVICES_STARTED,
    UpgradePhase.PREDECESSOR_WORKER_READY,
    UpgradePhase.CANDIDATE_CLEANED,
    UpgradePhase.FAILED,
)

_NONDESTRUCTIVE_COMPENSATION_PHASES = (
    UpgradePhase.COMPENSATION_STARTED,
    UpgradePhase.CANDIDATE_CLEANED,
    UpgradePhase.FAILED,
)

_COMPENSATION_PHASES = frozenset(_DESTRUCTIVE_COMPENSATION_PHASES)


@dataclass(frozen=True)
class PhaseObservation:
    disposition: ProbeDisposition
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, ProbeDisposition):
            raise TypeError("phase probe disposition is invalid")
        if not isinstance(self.evidence, Mapping):
            raise TypeError("phase probe evidence is invalid")


@dataclass(frozen=True)
class PhaseStep:
    phase: UpgradePhase
    probe: Callable[[], PhaseObservation]
    perform: Callable[[], None]
    recheck_on_resume: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.phase, UpgradePhase)
            or not callable(self.probe)
            or not callable(self.perform)
            or not isinstance(self.recheck_on_resume, bool)
        ):
            raise TypeError("upgrade phase step is invalid")


class PhaseDispatcher:
    """Adopt or perform exact phase effects, journaling only observations."""

    def __init__(self, journal: PhaseJournal) -> None:
        if not isinstance(journal, PhaseJournal):
            raise TypeError("phase journal is invalid")
        self._journal = journal

    def run(
        self,
        state: JournalState | None,
        steps: Sequence[PhaseStep],
    ) -> JournalState:
        if not isinstance(state, JournalState):
            raise UpgradeRecoveryRequired("pending operation journal is unavailable")
        program = tuple(steps)
        expected = tuple(step.phase.value for step in program)
        recorded = tuple(entry.phase for entry in state.entries)
        if recorded != expected[: len(recorded)]:
            raise UpgradeRecoveryRequired("operation journal phase order is invalid")
        if len(recorded) > len(program):
            raise UpgradeRecoveryRequired("operation journal has unexpected phases")
        for step in program[: len(recorded)]:
            if not step.recheck_on_resume:
                continue
            observation = step.probe()
            if observation.disposition is ProbeDisposition.CONFLICT:
                raise UpgradeConflict(f"{step.phase.value} exact probe conflicted")
            if observation.disposition is not ProbeDisposition.EXACT:
                raise UpgradeRecoveryRequired(
                    f"{step.phase.value} could not be revalidated"
                )
        for step in program[len(recorded) :]:
            observation = step.probe()
            if observation.disposition in {
                ProbeDisposition.ABSENT,
                ProbeDisposition.PARTIAL,
            }:
                step.perform()
                observation = step.probe()
            if observation.disposition is ProbeDisposition.AMBIGUOUS:
                raise UpgradeRecoveryRequired(
                    f"{step.phase.value} outcome requires operator recovery"
                )
            if observation.disposition is ProbeDisposition.CONFLICT:
                raise UpgradeConflict(f"{step.phase.value} exact probe conflicted")
            if observation.disposition is not ProbeDisposition.EXACT:
                raise UpgradeRecoveryRequired(
                    f"{step.phase.value} did not establish an exact effect"
                )
            state = self._journal.append(step.phase.value, observation.evidence)
        return state


@dataclass(frozen=True)
class ControlGenerationPlan:
    schema_version: int
    operation_id: str
    operation_kind: str
    start_nonce: str
    generation_id: str
    platform_target_name: str
    platform_target_sha256: str
    tuf_targets_version: int
    release_digest: str
    build_digest: str
    platform_version: str
    deployment_bundle: OciDeploymentBundle
    deployment_bundle_digest: str
    api_image: str
    worker_image: str
    database_revision: str
    current_database_revision: str
    current_generation_receipt_sha256: str | None
    current_selection_receipt_sha256: str | None
    current_projection_sequence: int | None
    site_configuration_digest: str
    running_identity_digest: str
    previous_generation: str | None
    host_updater_abi: int
    required_bytes: int
    plan_digest: str

    def _payload_document(self) -> dict[str, object]:
        bundle = self.deployment_bundle
        return {
            "api_image": self.api_image,
            "build_digest": self.build_digest,
            "database_revision": self.database_revision,
            "current_database_revision": self.current_database_revision,
            "current_generation_receipt_sha256": self.current_generation_receipt_sha256,
            "current_projection_sequence": self.current_projection_sequence,
            "current_selection_receipt_sha256": self.current_selection_receipt_sha256,
            "deployment_bundle": {
                "layer_digest": bundle.layer_digest,
                "layer_media_type": bundle.layer_media_type,
                "layer_size": bundle.layer_size,
                "manifest_digest": bundle.manifest_digest,
                "manifest_media_type": bundle.manifest_media_type,
                "manifest_size": bundle.manifest_size,
                "reference": bundle.reference,
            },
            "deployment_bundle_digest": self.deployment_bundle_digest,
            "generation_id": self.generation_id,
            "host_updater_abi": self.host_updater_abi,
            "operation_id": self.operation_id,
            "operation_kind": self.operation_kind,
            "platform_target_name": self.platform_target_name,
            "platform_target_sha256": self.platform_target_sha256,
            "platform_version": self.platform_version,
            "previous_generation": self.previous_generation,
            "release_digest": self.release_digest,
            "required_bytes": self.required_bytes,
            "running_identity_digest": self.running_identity_digest,
            "schema_version": self.schema_version,
            "start_nonce": self.start_nonce,
            "site_configuration_digest": self.site_configuration_digest,
            "tuf_targets_version": self.tuf_targets_version,
            "worker_image": self.worker_image,
        }

    def canonical_payload(self) -> bytes:
        return _canonical(self._payload_document())

    def document(self) -> dict[str, object]:
        return {**self._payload_document(), "plan_digest": self.plan_digest}

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> ControlGenerationPlan:
        expected = {
            "api_image",
            "build_digest",
            "current_database_revision",
            "current_generation_receipt_sha256",
            "current_projection_sequence",
            "current_selection_receipt_sha256",
            "database_revision",
            "deployment_bundle",
            "deployment_bundle_digest",
            "generation_id",
            "host_updater_abi",
            "operation_id",
            "operation_kind",
            "plan_digest",
            "platform_target_name",
            "platform_target_sha256",
            "platform_version",
            "previous_generation",
            "release_digest",
            "required_bytes",
            "running_identity_digest",
            "schema_version",
            "site_configuration_digest",
            "start_nonce",
            "tuf_targets_version",
            "worker_image",
        }
        if not isinstance(document, Mapping) or set(document) != expected:
            raise UpgradeConflict("control generation plan fields are invalid")
        bundle_document = document.get("deployment_bundle")
        bundle_fields = {
            "layer_digest",
            "layer_media_type",
            "layer_size",
            "manifest_digest",
            "manifest_media_type",
            "manifest_size",
            "reference",
        }
        if (
            not isinstance(bundle_document, Mapping)
            or set(bundle_document) != bundle_fields
        ):
            raise UpgradeConflict("control generation plan bundle is invalid")
        try:
            values = dict(document)
            values["deployment_bundle"] = OciDeploymentBundle(**dict(bundle_document))
            plan = cls(**values)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise UpgradeConflict("control generation plan is invalid") from error
        target = (
            _VERSIONED_TARGET.fullmatch(plan.platform_target_name)
            if isinstance(plan.platform_target_name, str)
            else None
        )
        try:
            valid = (
                plan.schema_version == 1
                and _GENERATION.fullmatch(plan.operation_id) is not None
                and plan.operation_kind in {"apply", "rollback"}
                and _SHA256.fullmatch(plan.start_nonce) is not None
                and target is not None
                and _SHA256.fullmatch(plan.platform_target_sha256) is not None
                and target.group("version") == plan.platform_version
                and target.group("sha256") == plan.platform_target_sha256
                and plan.generation_id == "gen-" + plan.platform_target_sha256[:24]
                and plan.release_digest == "sha256:" + plan.platform_target_sha256
                and _DIGEST.fullmatch(plan.build_digest) is not None
                and _DIGEST.fullmatch(plan.deployment_bundle_digest) is not None
                and plan.deployment_bundle_digest == plan.deployment_bundle.layer_digest
                and _IMAGE.fullmatch(plan.api_image) is not None
                and _IMAGE.fullmatch(plan.worker_image) is not None
                and _GENERATION.fullmatch(plan.database_revision) is not None
                and _GENERATION.fullmatch(plan.current_database_revision) is not None
                and (
                    plan.current_generation_receipt_sha256 is None
                    or _SHA256.fullmatch(plan.current_generation_receipt_sha256)
                    is not None
                )
                and (
                    plan.current_selection_receipt_sha256 is None
                    or _SHA256.fullmatch(plan.current_selection_receipt_sha256)
                    is not None
                )
                and (
                    plan.current_projection_sequence is None
                    or (
                        type(plan.current_projection_sequence) is int
                        and plan.current_projection_sequence >= 1
                    )
                )
                and _DIGEST.fullmatch(plan.site_configuration_digest) is not None
                and _DIGEST.fullmatch(plan.running_identity_digest) is not None
                and type(plan.tuf_targets_version) is int
                and plan.tuf_targets_version >= 1
                and type(plan.host_updater_abi) is int
                and 1 <= plan.host_updater_abi <= 65535
                and type(plan.required_bytes) is int
                and 1 <= plan.required_bytes <= 16 * 1024**4
                and (
                    plan.previous_generation is None
                    or _GENERATION.fullmatch(plan.previous_generation) is not None
                )
                and _DIGEST.fullmatch(plan.plan_digest) is not None
            )
        except (AttributeError, TypeError):
            valid = False
        if not valid:
            raise UpgradeConflict("control generation plan is invalid")
        expected_digest = (
            "sha256:" + hashlib.sha256(plan.canonical_payload()).hexdigest()
        )
        if not secrets.compare_digest(plan.plan_digest, expected_digest):
            raise UpgradeConflict("control generation plan digest is invalid")
        return plan


@dataclass(frozen=True)
class ControlGenerationResult:
    generation_id: str
    release_digest: str
    build_digest: str
    previous_generation: str | None
    status: str


@dataclass(frozen=True)
class ActiveControlRelease:
    """Exact active control identity authorized by host state and TUF."""

    generation_id: str
    release_digest: str
    build_digest: str
    platform_version: str
    api_image: str
    worker_image: str
    migration_revision: str


@dataclass(frozen=True)
class RunningControlIdentity:
    """Immutable release identity injected by the running container."""

    release_digest: str
    build_digest: str
    platform_version: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.release_digest, str)
            or _DIGEST.fullmatch(self.release_digest) is None
            or not isinstance(self.build_digest, str)
            or _DIGEST.fullmatch(self.build_digest) is None
            or not isinstance(self.platform_version, str)
            or _SEMVER.fullmatch(self.platform_version) is None
        ):
            raise ValueError("running control identity is invalid")


class VerifiedPlatformReleaseTarget(Protocol):
    """Return the fixed platform-release target after external TUF verification."""

    def __call__(self) -> bytes: ...


class VersionedPlatformReleaseSource(Protocol):
    """Refresh and return one exact caller-selected TUF target."""

    def refresh(self, target_name: str) -> tuple[bytes, int]: ...


class RunningControlIdentitySource(Protocol):
    """Return identity fixed in the current container at process start."""

    def __call__(self) -> RunningControlIdentity: ...


class UpgradeBoundary(Protocol):
    def database_revision(self) -> str: ...

    def site_configuration_digest(self) -> str: ...

    def running_control_identities(self) -> Mapping[str, object]: ...

    def probe_phase(
        self, phase: UpgradePhase, plan: ControlGenerationPlan
    ) -> PhaseObservation: ...

    def perform_phase(
        self, phase: UpgradePhase, plan: ControlGenerationPlan
    ) -> None: ...

    def control_is_running(self) -> bool: ...

    def available_bytes(self) -> int: ...

    def pull(self, references: tuple[str, ...]) -> None: ...

    def render_compose(self, environment: dict[str, str]) -> bytes: ...

    def backup(self, generation_id: str) -> dict[str, object]: ...

    def stop_worker(self) -> None: ...

    def migrate(self, revision: str) -> None: ...

    def start_api(self, generation_path: Path) -> None: ...

    def readiness(self) -> dict[str, object]: ...

    def start_worker(self) -> None: ...

    def stop_api(self) -> None: ...

    def restore_generation(self, generation_path: Path) -> None: ...


class ActiveControlReleaseLoader:
    """Read the active host generation and bind it to one verified TUF target."""

    def __init__(
        self,
        state_root: Path,
        verified_target: VerifiedPlatformReleaseTarget,
        running_identity: RunningControlIdentitySource,
    ) -> None:
        self._state_root = Path(state_root)
        if not self._state_root.is_absolute():
            raise UpgradeConflict("active control state root must be absolute")
        if not callable(verified_target):
            raise TypeError("verified platform release target must be callable")
        if not callable(running_identity):
            raise TypeError("running control identity source must be callable")
        self._verified_target = verified_target
        self._running_identity = running_identity

    def load(self) -> ActiveControlRelease:
        receipt = self._load_receipt()
        try:
            raw_target = self._verified_target()
        except Exception as error:
            raise UpgradeConflict(
                "verified platform release target is unavailable"
            ) from error
        try:
            release = PlatformRelease.from_bytes(raw_target)
        except PlatformReleaseError as error:
            raise UpgradeConflict(
                "verified platform release target is invalid"
            ) from error
        try:
            running = self._running_identity()
        except Exception as error:
            raise UpgradeConflict("running control identity is unavailable") from error
        if not isinstance(running, RunningControlIdentity):
            raise UpgradeConflict("running control identity is invalid")
        expected = {
            "release_digest": release.digest,
            "build_digest": release.build_digest,
            "platform_version": release.platform_version,
            "api_image": release.control.api_image.reference,
            "worker_image": release.control.worker_image.reference,
            "migration_revision": release.database.expand_revision,
        }
        if any(receipt[name] != value for name, value in expected.items()):
            raise UpgradeConflict(
                "active generation disagrees with verified platform release"
            )
        if (
            running.release_digest != release.digest
            or running.build_digest != release.build_digest
            or running.platform_version != release.platform_version
        ):
            raise UpgradeConflict(
                "running control identity disagrees with verified platform release"
            )
        expected_generation = "gen-" + release.digest.removeprefix("sha256:")[:24]
        if receipt["generation_id"] != expected_generation:
            raise UpgradeConflict(
                "active generation ID disagrees with verified platform release"
            )
        return ActiveControlRelease(
            generation_id=_receipt_string(receipt, "generation_id"),
            release_digest=release.digest,
            build_digest=release.build_digest,
            platform_version=release.platform_version,
            api_image=release.control.api_image.reference,
            worker_image=release.control.worker_image.reference,
            migration_revision=release.database.expand_revision,
        )

    def _load_receipt(self) -> dict[str, object]:
        root = _open_read_directory(self._state_root, "active control state root")
        try:
            marker = _read_bounded_file(
                root,
                "active-generation",
                _MAX_ACTIVE_POINTER,
                "active generation marker",
            )
            generation_id = _parse_active_marker(marker)
            generations = _open_read_directory_at(
                root, "generations", "control generations directory"
            )
            try:
                generation = _open_read_directory_at(
                    generations,
                    generation_id,
                    "active control generation directory",
                )
                try:
                    raw_receipt = _read_bounded_file(
                        generation,
                        "generation.json",
                        _MAX_GENERATION_RECEIPT,
                        "active generation receipt",
                    )
                finally:
                    os.close(generation)
            finally:
                os.close(generations)
        finally:
            os.close(root)
        receipt = _parse_active_receipt(raw_receipt)
        if receipt["generation_id"] != generation_id:
            raise UpgradeConflict(
                "active generation receipt generation binding is invalid"
            )
        return receipt


class ControlUpgrade:
    """Plan and apply one immutable control-host generation."""

    def __init__(
        self,
        state_root: Path,
        boundary: UpgradeBoundary,
        *,
        release_source: VersionedPlatformReleaseSource | None = None,
        operation_id_factory: Callable[[], str] | None = None,
        start_nonce_factory: Callable[[], str] | None = None,
        host_updater_abi: int = 2,
        host_owner_uid: int = 0,
        identity_root: Path | None = None,
    ) -> None:
        self._state_root = Path(state_root)
        if not self._state_root.is_absolute():
            raise UpgradeConflict("upgrade state root must be absolute")
        self._boundary = boundary
        self._release_source = release_source
        self._operation_id_factory = operation_id_factory or (
            lambda: "operation-" + secrets.token_hex(16)
        )
        self._start_nonce_factory = start_nonce_factory or (
            lambda: secrets.token_hex(32)
        )
        if (
            isinstance(host_updater_abi, bool)
            or not isinstance(host_updater_abi, int)
            or not 1 <= host_updater_abi <= 65535
        ):
            raise ValueError("host updater ABI is invalid")
        self._host_updater_abi = host_updater_abi
        if (
            isinstance(host_owner_uid, bool)
            or not isinstance(host_owner_uid, int)
            or host_owner_uid < 0
        ):
            raise ValueError("host owner UID is invalid")
        self._host_owner_uid = host_owner_uid
        self._identity_root = (
            Path(identity_root)
            if identity_root is not None
            else self._state_root.parent / "control-identity"
        )
        if not self._identity_root.is_absolute():
            raise ValueError("control identity root must be absolute")

    def plan(self, target_name: str | PlatformRelease) -> ControlGenerationPlan:
        if isinstance(target_name, PlatformRelease):
            exact_plan = False
            release = target_name
            target_sha256 = release.digest.removeprefix("sha256:")
            selected_target = (
                f"platform/releases/{release.platform_version}/{target_sha256}.json"
            )
            targets_version = 1
            operation_id = "operation-" + target_sha256[:32]
            start_nonce = hashlib.sha256(
                ("release-selection:" + release.digest).encode("ascii")
            ).hexdigest()
        else:
            exact_plan = True
            if self._release_source is None:
                raise UpgradeConflict(
                    "versioned platform release source is unavailable"
                )
            try:
                raw, targets_version = self._release_source.refresh(target_name)
            except Exception as error:
                raise UpgradeConflict(
                    "versioned platform target is unavailable"
                ) from error
            if (
                not isinstance(raw, bytes)
                or isinstance(targets_version, bool)
                or not isinstance(targets_version, int)
                or targets_version < 1
            ):
                raise UpgradeConflict("versioned platform target result is invalid")
            target_sha256 = hashlib.sha256(raw).hexdigest()
            try:
                release = PlatformRelease.from_bytes(raw)
                release.validate_target_identity(target_name, target_sha256)
            except PlatformReleaseError as error:
                raise UpgradeConflict(
                    "versioned platform target identity is invalid"
                ) from error
            selected_target = target_name
            operation_id = self._operation_id_factory()
            start_nonce = self._start_nonce_factory()
        if not release.host_updater_abi.contains(self._host_updater_abi):
            raise UpgradeConflict(
                "platform target does not support this host updater ABI"
            )
        if exact_plan:
            try:
                current_database_revision = self._boundary.database_revision()
                site_configuration_digest = self._boundary.site_configuration_digest()
                running_identities = self._boundary.running_control_identities()
            except Exception as error:
                raise UpgradeConflict(
                    "current control identity is unavailable"
                ) from error
            if (
                not isinstance(current_database_revision, str)
                or _GENERATION.fullmatch(current_database_revision) is None
                or not isinstance(site_configuration_digest, str)
                or _DIGEST.fullmatch(site_configuration_digest) is None
                or not isinstance(running_identities, Mapping)
            ):
                raise UpgradeConflict("current control identity is invalid")
            try:
                running_raw = (
                    json.dumps(
                        dict(running_identities),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("ascii")
            except (TypeError, ValueError, UnicodeEncodeError) as error:
                raise UpgradeConflict("current running identity is invalid") from error
            if not 1 <= len(running_raw) <= 64 * 1024:
                raise UpgradeConflict("current running identity is invalid")
            running_identity_digest = (
                "sha256:" + hashlib.sha256(running_raw).hexdigest()
            )
            try:
                current_generation = HostGenerationStore(
                    self._state_root,
                    self._identity_root,
                    owner_uid=self._host_owner_uid,
                ).load_active()
            except HostStateConflict as error:
                raise UpgradeConflict("active control generation is unsafe") from error
            if current_generation is not None:
                predecessor = next(
                    (
                        item
                        for item in release.predecessors
                        if item.target_name == current_generation.platform_target_name
                    ),
                    None,
                )
                if predecessor is None or (
                    predecessor.target_sha256
                    != current_generation.platform_target_sha256
                    or predecessor.release_digest != current_generation.release_digest
                    or predecessor.build_digest != current_generation.build_digest
                    or predecessor.deployment_bundle_digest
                    != current_generation.deployment_bundle_digest
                ):
                    raise UpgradeConflict(
                        "platform target does not authorize the exact active predecessor"
                    )
            previous = (
                current_generation.generation_id
                if current_generation is not None
                else None
            )
            current_generation_receipt_sha256 = (
                current_generation.generation_receipt_sha256
                if current_generation is not None
                else None
            )
            current_selection_receipt_sha256 = (
                current_generation.selection_receipt_sha256
                if current_generation is not None
                else None
            )
            current_projection_sequence = (
                current_generation.projection_sequence
                if current_generation is not None
                else None
            )
        else:
            current_database_revision = release.database.expand_revision
            site_configuration_digest = "sha256:" + "0" * 64
            running_identity_digest = "sha256:" + hashlib.sha256(b"{}\n").hexdigest()
            previous = _active_generation(self._state_root)
            current_generation_receipt_sha256 = None
            current_selection_receipt_sha256 = None
            current_projection_sequence = None
        content = {
            "schema_version": 1,
            "operation_id": operation_id,
            "operation_kind": "apply",
            "start_nonce": start_nonce,
            "generation_id": "gen-" + target_sha256[:24],
            "platform_target_name": selected_target,
            "platform_target_sha256": target_sha256,
            "tuf_targets_version": targets_version,
            "release_digest": release.digest,
            "build_digest": release.build_digest,
            "platform_version": release.platform_version,
            "deployment_bundle": release.deployment_bundle,
            "deployment_bundle_digest": release.deployment_bundle.layer_digest,
            "api_image": release.control.api_image.reference,
            "worker_image": release.control.worker_image.reference,
            "database_revision": release.database.expand_revision,
            "current_database_revision": current_database_revision,
            "current_generation_receipt_sha256": current_generation_receipt_sha256,
            "current_selection_receipt_sha256": current_selection_receipt_sha256,
            "current_projection_sequence": current_projection_sequence,
            "site_configuration_digest": site_configuration_digest,
            "running_identity_digest": running_identity_digest,
            "previous_generation": previous,
            "host_updater_abi": self._host_updater_abi,
            "required_bytes": (
                _required_bytes(release)
                + release.deployment_bundle.manifest_size
                + release.deployment_bundle.layer_size
            ),
        }
        candidate = ControlGenerationPlan(**content, plan_digest=f"sha256:{'0' * 64}")
        return ControlGenerationPlan(
            **content,
            plan_digest="sha256:"
            + hashlib.sha256(candidate.canonical_payload()).hexdigest(),
        )

    def active_generation(self) -> str | None:
        return _active_generation(self._state_root)

    def rollback_plan(self, generation_id: str) -> ControlGenerationPlan:
        if self._release_source is None:
            raise UpgradeConflict("versioned platform release source is unavailable")
        store = HostGenerationStore(
            self._state_root,
            self._identity_root,
            owner_uid=self._host_owner_uid,
        )
        try:
            active = store.load_active()
            if active is None:
                raise UpgradeConflict("there is no active control generation")
            if generation_id != active.previous_generation:
                raise UpgradeConflict("rollback target is not the selected predecessor")
            target_receipt = store.load_generation(generation_id)
            current_raw, _current_targets_version = self._release_source.refresh(
                active.platform_target_name
            )
            current_sha = hashlib.sha256(current_raw).hexdigest()
            current_release = PlatformRelease.from_bytes(current_raw)
            current_release.validate_target_identity(
                active.platform_target_name, current_sha
            )
            if (
                current_sha != active.platform_target_sha256
                or current_release.digest != active.release_digest
                or current_release.build_digest != active.build_digest
                or current_release.deployment_bundle.layer_digest
                != active.deployment_bundle_digest
            ):
                raise UpgradeConflict("active release authorization is inconsistent")
            predecessor = next(
                (
                    item
                    for item in current_release.predecessors
                    if item.target_name == target_receipt.platform_target_name
                ),
                None,
            )
            if predecessor is None or (
                predecessor.target_sha256 != target_receipt.platform_target_sha256
                or predecessor.release_digest != target_receipt.release_digest
                or predecessor.build_digest != target_receipt.build_digest
                or predecessor.deployment_bundle_digest
                != target_receipt.deployment_bundle_digest
            ):
                raise UpgradeConflict(
                    "rollback target does not match the exact authorized predecessor"
                )
            target_raw, target_targets_version = self._release_source.refresh(
                predecessor.target_name
            )
            target_sha = hashlib.sha256(target_raw).hexdigest()
            target_release = PlatformRelease.from_bytes(target_raw)
            target_release.validate_target_identity(predecessor.target_name, target_sha)
        except UpgradeConflict:
            raise
        except Exception as error:
            raise UpgradeConflict("rollback authorization is unavailable") from error
        if (
            target_sha != target_receipt.platform_target_sha256
            or target_release.digest != target_receipt.release_digest
            or target_release.build_digest != target_receipt.build_digest
            or target_release.platform_version != target_receipt.platform_version
            or target_release.deployment_bundle.layer_digest
            != target_receipt.deployment_bundle_digest
            or target_release.control.api_image.reference != target_receipt.api_image
            or target_release.control.worker_image.reference
            != target_receipt.worker_image
            or target_release.database.expand_revision
            != target_receipt.database_revision
            or not target_release.host_updater_abi.contains(self._host_updater_abi)
        ):
            raise UpgradeConflict(
                "rollback generation disagrees with authorized target"
            )
        try:
            current_database = self._boundary.database_revision()
            site_digest = self._boundary.site_configuration_digest()
            running = self._boundary.running_control_identities()
            running_raw = (
                json.dumps(
                    dict(running),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("ascii")
        except Exception as error:
            raise UpgradeConflict("current rollback snapshot is unavailable") from error
        if (
            _GENERATION.fullmatch(current_database) is None
            or _DIGEST.fullmatch(site_digest) is None
            or not 1 <= len(running_raw) <= 64 * 1024
        ):
            raise UpgradeConflict("current rollback snapshot is invalid")
        content = {
            "schema_version": 1,
            "operation_id": self._operation_id_factory(),
            "operation_kind": "rollback",
            "start_nonce": self._start_nonce_factory(),
            "generation_id": target_receipt.generation_id,
            "platform_target_name": target_receipt.platform_target_name,
            "platform_target_sha256": target_receipt.platform_target_sha256,
            # Rollback authorization binds the current TUF metadata snapshot.
            # The immutable generation receipt correctly retains the older
            # snapshot under which that generation was first installed.
            "tuf_targets_version": target_targets_version,
            "release_digest": target_receipt.release_digest,
            "build_digest": target_receipt.build_digest,
            "platform_version": target_receipt.platform_version,
            "deployment_bundle": target_release.deployment_bundle,
            "deployment_bundle_digest": target_receipt.deployment_bundle_digest,
            "api_image": target_receipt.api_image,
            "worker_image": target_receipt.worker_image,
            "database_revision": target_receipt.database_revision,
            "current_database_revision": current_database,
            "current_generation_receipt_sha256": active.generation_receipt_sha256,
            "current_selection_receipt_sha256": active.selection_receipt_sha256,
            "current_projection_sequence": active.projection_sequence,
            "site_configuration_digest": site_digest,
            "running_identity_digest": "sha256:"
            + hashlib.sha256(running_raw).hexdigest(),
            "previous_generation": active.generation_id,
            "host_updater_abi": self._host_updater_abi,
            "required_bytes": (
                _required_bytes(target_release)
                + target_release.deployment_bundle.manifest_size
                + target_release.deployment_bundle.layer_size
            ),
        }
        provisional = ControlGenerationPlan(**content, plan_digest=f"sha256:{'0' * 64}")
        return ControlGenerationPlan(
            **content,
            plan_digest="sha256:"
            + hashlib.sha256(provisional.canonical_payload()).hexdigest(),
        )

    def apply(
        self,
        plan: ControlGenerationPlan,
        release: PlatformRelease | None = None,
    ) -> ControlGenerationResult:
        if release is None:
            return self._apply_exact(plan)
        self._require_release_binding(plan, release)
        if self._release_source is not None:
            # Compatibility callers may still pass the already parsed release.
            # The exact TUF plan remains authoritative and must retain its
            # operation ID/start nonce rather than being regenerated.
            return self._apply_exact(plan)
        if self._boundary.control_is_running():
            raise UpgradeConflict("control plane is running; stop API and worker first")
        lock = HostOperationLock(self._state_root, owner_uid=self._host_owner_uid)
        try:
            lock.__enter__()
        except HostStateConflict as error:
            raise UpgradeConflict("another host operation is active") from error
        try:
            return self._apply_locked(plan, release)
        finally:
            lock.__exit__(None, None, None)

    def _apply_exact(self, plan: ControlGenerationPlan) -> ControlGenerationResult:
        if not isinstance(plan, ControlGenerationPlan):
            raise UpgradeConflict("control generation plan is invalid")
        ControlGenerationPlan.from_document(plan.document())
        lock = HostOperationLock(self._state_root, owner_uid=self._host_owner_uid)
        try:
            lock.__enter__()
        except HostStateConflict as error:
            raise UpgradeConflict("another host operation is active") from error
        try:
            journal = PhaseJournal(
                self._state_root,
                operation_id=plan.operation_id,
                owner_uid=self._host_owner_uid,
            )
            pending = PhaseJournal(
                self._state_root, owner_uid=self._host_owner_uid
            ).load_pending()
            if pending is not None:
                raise UpgradeRecoveryRequired(
                    "a pending host operation must be recovered before apply"
                )
            self._require_new_plan_snapshot(plan)
            if plan.operation_kind == "rollback":
                self._require_rollback_predecessor(plan)
            state = journal.create(plan.document())
            try:
                completed = PhaseDispatcher(journal).run(
                    state,
                    self._phase_program(plan),
                )
            except UpgradeError as error:
                if not self._can_compensate(error, plan):
                    raise
                pending = PhaseJournal(
                    self._state_root, owner_uid=self._host_owner_uid
                ).load_pending()
                self._run_compensation(plan, pending, journal)
                raise
            if not completed.terminal:
                raise UpgradeRecoveryRequired("host operation did not complete")
            return self._result(plan)
        except HostStateConflict as error:
            raise UpgradeRecoveryRequired("host operation state is unsafe") from error
        finally:
            lock.__exit__(None, None, None)

    def recover(self) -> ControlGenerationResult:
        lock = HostOperationLock(self._state_root, owner_uid=self._host_owner_uid)
        try:
            lock.__enter__()
        except HostStateConflict as error:
            raise UpgradeConflict("another host operation is active") from error
        try:
            state = PhaseJournal(
                self._state_root, owner_uid=self._host_owner_uid
            ).load_pending()
            if state is None:
                raise UpgradeConflict("there is no pending host operation")
            plan = ControlGenerationPlan.from_document(state.plan_document)
            journal = PhaseJournal(
                self._state_root,
                operation_id=state.operation_id,
                owner_uid=self._host_owner_uid,
            )
            if self._is_compensating(state):
                completed = self._run_compensation(plan, state, journal)
                if not completed.terminal:
                    raise UpgradeRecoveryRequired("compensation did not complete")
                return self._compensation_result(plan)
            try:
                completed = PhaseDispatcher(journal).run(
                    state,
                    self._phase_program(plan),
                )
            except UpgradeError as error:
                if not self._can_compensate(error, plan):
                    raise
                pending = PhaseJournal(
                    self._state_root, owner_uid=self._host_owner_uid
                ).load_pending()
                completed = self._run_compensation(plan, pending, journal)
                if not completed.terminal:
                    raise UpgradeRecoveryRequired("compensation did not complete")
                return self._compensation_result(plan)
            if not completed.terminal:
                raise UpgradeRecoveryRequired("host operation did not complete")
            return self._result(plan)
        except HostStateConflict as error:
            raise UpgradeRecoveryRequired("host operation state is unsafe") from error
        finally:
            lock.__exit__(None, None, None)

    def _phase_program(self, plan: ControlGenerationPlan) -> tuple[PhaseStep, ...]:
        phases = (
            _ROLLBACK_PHASES if plan.operation_kind == "rollback" else _APPLY_PHASES
        )
        steps: list[PhaseStep] = [
            PhaseStep(
                UpgradePhase.AUTHORIZED,
                lambda: self._authorization_observation(plan),
                lambda: None,
                recheck_on_resume=True,
            )
        ]
        for phase in phases[1:]:
            if phase is UpgradePhase.PREDECESSOR_VERIFIED:
                steps.append(
                    PhaseStep(
                        phase,
                        lambda: self._rollback_predecessor_observation(plan),
                        lambda: None,
                    )
                )
                continue
            steps.append(
                PhaseStep(
                    phase,
                    lambda phase=phase: self._boundary.probe_phase(phase, plan),
                    lambda phase=phase: self._boundary.perform_phase(phase, plan),
                )
            )
        return tuple(steps)

    def _rollback_predecessor_observation(
        self, plan: ControlGenerationPlan
    ) -> PhaseObservation:
        try:
            self._require_rollback_predecessor(plan)
        except UpgradeConflict:
            return PhaseObservation(ProbeDisposition.CONFLICT, {})
        return PhaseObservation(
            ProbeDisposition.EXACT,
            {
                "from_generation": plan.previous_generation,
                "predecessor_generation": plan.generation_id,
                "predecessor_release_digest": plan.release_digest,
                "predecessor_target_name": plan.platform_target_name,
                "predecessor_target_sha256": plan.platform_target_sha256,
            },
        )

    @staticmethod
    def _is_compensating(state: JournalState) -> bool:
        return any(
            entry.phase in {phase.value for phase in _COMPENSATION_PHASES}
            for entry in state.entries
        )

    @staticmethod
    def _can_compensate(error: UpgradeError, plan: ControlGenerationPlan) -> bool:
        return plan.operation_kind == "apply" and not isinstance(
            error,
            (AmbiguousMigrationError, UpgradeConflict, UpgradeRecoveryRequired),
        )

    def _run_compensation(
        self,
        plan: ControlGenerationPlan,
        state: JournalState | None,
        journal: PhaseJournal,
    ) -> JournalState:
        if state is None:
            raise UpgradeRecoveryRequired("pending compensation journal is unavailable")
        steps = self._compensation_program(plan, state)
        completed = PhaseDispatcher(journal).run(state, steps)
        if not completed.terminal or completed.entries[-1].phase != UpgradePhase.FAILED:
            raise UpgradeRecoveryRequired("compensation did not reach a terminal state")
        return completed

    def _compensation_program(
        self,
        plan: ControlGenerationPlan,
        state: JournalState,
    ) -> tuple[PhaseStep, ...]:
        recorded = tuple(entry.phase for entry in state.entries)
        first_compensation = next(
            (
                index
                for index, phase in enumerate(recorded)
                if phase in {item.value for item in _COMPENSATION_PHASES}
            ),
            len(recorded),
        )
        apply_prefix = recorded[:first_compensation]
        expected_apply = tuple(phase.value for phase in _APPLY_PHASES)
        if apply_prefix != expected_apply[: len(apply_prefix)]:
            raise UpgradeRecoveryRequired(
                "operation journal cannot transition to compensation"
            )
        destructive = (
            UpgradePhase.SERVICES_STOPPED_DATABASE_MIGRATED.value in apply_prefix
            or (
                first_compensation < len(recorded)
                and recorded[first_compensation]
                == UpgradePhase.COMPENSATION_SERVICES_STOPPED.value
            )
        )
        if destructive and plan.previous_generation is None:
            raise UpgradeRecoveryRequired(
                "destructive first-install failure has no predecessor to restore"
            )
        compensation = (
            _DESTRUCTIVE_COMPENSATION_PHASES
            if destructive
            else _NONDESTRUCTIVE_COMPENSATION_PHASES
        )
        compensation_recorded = recorded[first_compensation:]
        expected_compensation = tuple(phase.value for phase in compensation)
        if compensation_recorded != expected_compensation[: len(compensation_recorded)]:
            raise UpgradeRecoveryRequired("compensation journal phase order is invalid")

        # Recorded apply entries remain part of the one immutable journal chain, but
        # candidate authorization must not be rechecked while restoring an already
        # recorded exact predecessor after a candidate is revoked.
        prefix_steps = tuple(
            PhaseStep(
                phase,
                lambda: PhaseObservation(ProbeDisposition.EXACT, {}),
                lambda: None,
            )
            for phase in _APPLY_PHASES[: len(apply_prefix)]
        )
        compensation_steps = tuple(
            (
                PhaseStep(
                    phase,
                    lambda: PhaseObservation(
                        ProbeDisposition.EXACT,
                        {
                            "candidate_generation": plan.generation_id,
                            "predecessor_generation": plan.previous_generation,
                            "transition": "compensation",
                        },
                    ),
                    lambda: None,
                )
                if phase is UpgradePhase.COMPENSATION_STARTED
                else PhaseStep(
                    phase,
                    lambda phase=phase: self._boundary.probe_phase(phase, plan),
                    lambda phase=phase: self._boundary.perform_phase(phase, plan),
                )
            )
            for phase in compensation
        )
        return prefix_steps + compensation_steps

    def _compensation_result(
        self, plan: ControlGenerationPlan
    ) -> ControlGenerationResult:
        if plan.previous_generation is None:
            raise UpgradeRecoveryRequired("compensation has no predecessor generation")
        try:
            predecessor = HostGenerationStore(
                self._state_root,
                self._identity_root,
                owner_uid=self._host_owner_uid,
            ).load_generation(plan.previous_generation)
        except HostStateConflict as error:
            raise UpgradeRecoveryRequired(
                "compensated predecessor generation is unsafe"
            ) from error
        return ControlGenerationResult(
            generation_id=predecessor.generation_id,
            release_digest=predecessor.release_digest,
            build_digest=predecessor.build_digest,
            previous_generation=plan.generation_id,
            status="rolled-back",
        )

    def _authorization_observation(
        self, plan: ControlGenerationPlan
    ) -> PhaseObservation:
        if self._release_source is None:
            return PhaseObservation(ProbeDisposition.CONFLICT, {})
        try:
            raw, targets_version = self._release_source.refresh(
                plan.platform_target_name
            )
            target_sha256 = hashlib.sha256(raw).hexdigest()
            release = PlatformRelease.from_bytes(raw)
            release.validate_target_identity(plan.platform_target_name, target_sha256)
        except Exception:  # noqa: BLE001 - external TUF source is fail-closed
            return PhaseObservation(ProbeDisposition.CONFLICT, {})
        exact = (
            targets_version == plan.tuf_targets_version
            and target_sha256 == plan.platform_target_sha256
            and release.digest == plan.release_digest
            and release.build_digest == plan.build_digest
            and release.platform_version == plan.platform_version
            and release.deployment_bundle == plan.deployment_bundle
            and release.deployment_bundle.layer_digest == plan.deployment_bundle_digest
            and release.control.api_image.reference == plan.api_image
            and release.control.worker_image.reference == plan.worker_image
            and release.database.expand_revision == plan.database_revision
            and release.host_updater_abi.contains(plan.host_updater_abi)
        )
        if not exact:
            return PhaseObservation(ProbeDisposition.CONFLICT, {})
        return PhaseObservation(
            ProbeDisposition.EXACT,
            {
                "host_updater_abi": plan.host_updater_abi,
                "platform_target_name": plan.platform_target_name,
                "platform_target_sha256": plan.platform_target_sha256,
                "release_digest": plan.release_digest,
                "tuf_targets_version": plan.tuf_targets_version,
            },
        )

    def _require_rollback_predecessor(self, plan: ControlGenerationPlan) -> None:
        """Re-resolve selected N and exact N-1 after taking the host lock."""

        if plan.operation_kind != "rollback" or self._release_source is None:
            raise UpgradeConflict("rollback authorization is unavailable")
        try:
            store = HostGenerationStore(
                self._state_root,
                self._identity_root,
                owner_uid=self._host_owner_uid,
            )
            active = store.load_active()
            if active is None or active.generation_id != plan.previous_generation:
                raise UpgradeConflict("selected rollback source changed after planning")
            predecessor_receipt = store.load_generation(plan.generation_id)
            current_raw, _current_targets_version = self._release_source.refresh(
                active.platform_target_name
            )
            current_sha = hashlib.sha256(current_raw).hexdigest()
            current_release = PlatformRelease.from_bytes(current_raw)
            current_release.validate_target_identity(
                active.platform_target_name, current_sha
            )
        except UpgradeConflict:
            raise
        except Exception as error:
            raise UpgradeConflict(
                "selected rollback source authorization is unavailable"
            ) from error
        active_exact = (
            current_sha == active.platform_target_sha256
            and current_release.digest == active.release_digest
            and current_release.build_digest == active.build_digest
            and current_release.platform_version == active.platform_version
            and current_release.deployment_bundle.layer_digest
            == active.deployment_bundle_digest
            and current_release.control.api_image.reference == active.api_image
            and current_release.control.worker_image.reference == active.worker_image
            and current_release.database.expand_revision == active.database_revision
        )
        predecessor = next(
            (
                item
                for item in current_release.predecessors
                if item.target_name == plan.platform_target_name
            ),
            None,
        )
        predecessor_exact = predecessor is not None and (
            predecessor.target_sha256 == plan.platform_target_sha256
            and predecessor.release_digest == plan.release_digest
            and predecessor.build_digest == plan.build_digest
            and predecessor.deployment_bundle_digest == plan.deployment_bundle_digest
        )
        receipt_exact = (
            predecessor_receipt.generation_id == plan.generation_id
            and predecessor_receipt.platform_target_name == plan.platform_target_name
            and predecessor_receipt.platform_target_sha256
            == plan.platform_target_sha256
            and predecessor_receipt.release_digest == plan.release_digest
            and predecessor_receipt.build_digest == plan.build_digest
            and predecessor_receipt.platform_version == plan.platform_version
            and predecessor_receipt.deployment_bundle_digest
            == plan.deployment_bundle_digest
            and predecessor_receipt.api_image == plan.api_image
            and predecessor_receipt.worker_image == plan.worker_image
            and predecessor_receipt.database_revision == plan.database_revision
        )
        if not active_exact or not predecessor_exact or not receipt_exact:
            raise UpgradeConflict(
                "rollback source no longer authorizes the exact predecessor"
            )

    def _require_new_plan_snapshot(self, plan: ControlGenerationPlan) -> None:
        authorization = self._authorization_observation(plan)
        if authorization.disposition is not ProbeDisposition.EXACT:
            raise UpgradeConflict("platform authorization changed after planning")
        try:
            current_database = self._boundary.database_revision()
            site_digest = self._boundary.site_configuration_digest()
            running = self._boundary.running_control_identities()
            available = self._boundary.available_bytes()
            running_raw = (
                json.dumps(
                    dict(running),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("ascii")
        except Exception as error:
            raise UpgradeConflict("current control snapshot is unavailable") from error
        running_digest = "sha256:" + hashlib.sha256(running_raw).hexdigest()
        try:
            selected = HostGenerationStore(
                self._state_root,
                self._identity_root,
                owner_uid=self._host_owner_uid,
            ).load_active()
        except HostStateConflict as error:
            raise UpgradeConflict("active control generation is unsafe") from error
        selected_fields = (
            (
                None,
                None,
                None,
                None,
            )
            if selected is None
            else (
                selected.generation_id,
                selected.generation_receipt_sha256,
                selected.selection_receipt_sha256,
                selected.projection_sequence,
            )
        )
        if (
            current_database != plan.current_database_revision
            or site_digest != plan.site_configuration_digest
            or running_digest != plan.running_identity_digest
            or isinstance(available, bool)
            or not isinstance(available, int)
            or available < plan.required_bytes
            or selected_fields
            != (
                plan.previous_generation,
                plan.current_generation_receipt_sha256,
                plan.current_selection_receipt_sha256,
                plan.current_projection_sequence,
            )
        ):
            raise UpgradeConflict("control generation plan is stale")

    @staticmethod
    def _result(plan: ControlGenerationPlan) -> ControlGenerationResult:
        return ControlGenerationResult(
            generation_id=plan.generation_id,
            release_digest=plan.release_digest,
            build_digest=plan.build_digest,
            previous_generation=plan.previous_generation,
            status="rolled-back" if plan.operation_kind == "rollback" else "active",
        )

    def _apply_locked(
        self,
        plan: ControlGenerationPlan,
        release: PlatformRelease,
    ) -> ControlGenerationResult:
        self._require_plan_snapshot(plan, release)
        available = self._boundary.available_bytes()
        if (
            isinstance(available, bool)
            or not isinstance(available, int)
            or available < plan.required_bytes
        ):
            raise UpgradeConflict("insufficient disk space for control generation")

        _secure_directory(self._state_root)
        generations = self._state_root / "generations"
        staging = generations / f".{plan.generation_id}.staging"
        final = generations / plan.generation_id
        environment = {
            "CONTROL_API_IMAGE": plan.api_image,
            "CONTROL_WORKER_IMAGE": plan.worker_image,
            "VONK_PLATFORM_BUILD_DIGEST": plan.build_digest,
            "VONK_PLATFORM_RELEASE_DIGEST": plan.release_digest,
            "VONK_PLATFORM_VERSION": plan.platform_version,
        }
        references = (plan.api_image, plan.worker_image)
        try:
            _secure_directory(generations)
            if (
                staging.exists()
                or staging.is_symlink()
                or final.exists()
                or final.is_symlink()
            ):
                raise UpgradeConflict("control generation already exists")
            staging.mkdir(mode=0o700)
            self._boundary.pull(references)
            rendered = self._boundary.render_compose(environment)
            if (
                not isinstance(rendered, bytes)
                or not 0 < len(rendered) <= _MAX_RENDERED_COMPOSE
            ):
                raise UpgradeError("rendered Compose generation is invalid")
            _write_new(staging / "compose.rendered.yaml", rendered)
            _write_new(
                staging / "platform.env",
                "".join(
                    f"{key}={environment[key]}\n" for key in sorted(environment)
                ).encode(),
            )
            backup = self._boundary.backup(plan.generation_id)
            _json_mapping(backup, "backup manifest")
            self._boundary.stop_worker()
            try:
                self._boundary.migrate(plan.database_revision)
            except AmbiguousMigrationError as error:
                recovery = {
                    "schema_version": 1,
                    "generation_id": plan.generation_id,
                    "previous_generation": plan.previous_generation,
                    "phase": "migration-ambiguous",
                    "release_digest": plan.release_digest,
                }
                _write_atomic(
                    self._state_root / "recovery-required.json",
                    _canonical(recovery),
                )
                raise UpgradeRecoveryRequired(
                    "database migration is ambiguous; operator recovery is required"
                ) from error
            self._boundary.start_api(staging)
            try:
                readiness = self._boundary.readiness()
                _json_mapping(readiness, "readiness evidence")
            except UpgradeReadinessError:
                self._boundary.stop_api()
                if plan.previous_generation is None:
                    raise UpgradeRecoveryRequired(
                        "candidate failed readiness and no prior generation exists"
                    )
                previous_path = _generation_path(generations, plan.previous_generation)
                self._boundary.restore_generation(previous_path)
                raise
            receipt = {
                "schema_version": 1,
                "generation_id": plan.generation_id,
                "release_digest": plan.release_digest,
                "build_digest": plan.build_digest,
                "platform_version": plan.platform_version,
                "api_image": plan.api_image,
                "worker_image": plan.worker_image,
                "migration_revision": plan.database_revision,
                "previous_generation": plan.previous_generation,
                "compose_sha256": hashlib.sha256(rendered).hexdigest(),
                "backup": backup,
                "readiness": readiness,
                "status": "active",
            }
            _write_new(staging / "generation.json", _canonical(receipt))
            os.replace(staging, final)
            _fsync_directory(generations)
            _write_atomic(
                self._state_root / "active-generation",
                (plan.generation_id + "\n").encode(),
            )
            try:
                self._boundary.start_worker()
            except Exception:
                self._boundary.stop_api()
                if plan.previous_generation is None:
                    raise UpgradeRecoveryRequired(
                        "candidate worker failed and no prior generation exists"
                    )
                previous_path = _generation_path(generations, plan.previous_generation)
                self._boundary.restore_generation(previous_path)
                _write_atomic(
                    self._state_root / "active-generation",
                    (plan.previous_generation + "\n").encode(),
                )
                raise
            return ControlGenerationResult(
                generation_id=plan.generation_id,
                release_digest=plan.release_digest,
                build_digest=plan.build_digest,
                previous_generation=plan.previous_generation,
                status="active",
            )
        except (UpgradeError, OSError):
            raise
        except Exception as error:
            raise UpgradeError("control generation upgrade failed") from error

    def _require_release_binding(
        self,
        plan: ControlGenerationPlan,
        release: PlatformRelease,
    ) -> None:
        if not isinstance(plan, ControlGenerationPlan) or not isinstance(
            release, PlatformRelease
        ):
            raise UpgradeConflict("upgrade plan or release is invalid")
        ControlGenerationPlan.from_document(plan.document())
        target_sha256 = release.digest.removeprefix("sha256:")
        expected_target = (
            f"platform/releases/{release.platform_version}/{target_sha256}.json"
        )
        if (
            plan.operation_kind != "apply"
            or plan.generation_id != "gen-" + target_sha256[:24]
            or plan.platform_target_name != expected_target
            or plan.platform_target_sha256 != target_sha256
            or plan.release_digest != release.digest
            or plan.build_digest != release.build_digest
            or plan.platform_version != release.platform_version
            or plan.deployment_bundle != release.deployment_bundle
            or plan.deployment_bundle_digest != release.deployment_bundle.layer_digest
            or plan.api_image != release.control.api_image.reference
            or plan.worker_image != release.control.worker_image.reference
            or plan.database_revision != release.database.expand_revision
            or plan.host_updater_abi != self._host_updater_abi
            or not release.host_updater_abi.contains(plan.host_updater_abi)
            or plan.required_bytes
            != (
                _required_bytes(release)
                + release.deployment_bundle.manifest_size
                + release.deployment_bundle.layer_size
            )
        ):
            raise UpgradeConflict("upgrade plan does not match the exact release")

    def _require_plan_snapshot(
        self,
        plan: ControlGenerationPlan,
        release: PlatformRelease,
    ) -> None:
        self._require_release_binding(plan, release)
        if (
            plan.tuf_targets_version != 1
            or plan.current_database_revision != release.database.expand_revision
            or plan.current_generation_receipt_sha256 is not None
            or plan.current_selection_receipt_sha256 is not None
            or plan.current_projection_sequence is not None
            or plan.site_configuration_digest != "sha256:" + "0" * 64
            or plan.running_identity_digest
            != "sha256:" + hashlib.sha256(b"{}\n").hexdigest()
            or plan.previous_generation != _active_generation(self._state_root)
        ):
            raise UpgradeConflict("upgrade plan is stale or does not match the release")

    def rollback(
        self, generation_id: str | ControlGenerationPlan
    ) -> ControlGenerationResult:
        if isinstance(generation_id, ControlGenerationPlan):
            if generation_id.operation_kind != "rollback":
                raise UpgradeConflict("exact rollback requires a rollback plan")
            return self._apply_exact(generation_id)
        if self._boundary.control_is_running():
            raise UpgradeConflict("control plane is running; stop API and worker first")
        lock = HostOperationLock(self._state_root, owner_uid=self._host_owner_uid)
        try:
            lock.__enter__()
        except HostStateConflict as error:
            raise UpgradeConflict("another host operation is active") from error
        try:
            return self._rollback_locked(generation_id)
        finally:
            lock.__exit__(None, None, None)

    def _rollback_locked(self, generation_id: str) -> ControlGenerationResult:
        active = _active_generation(self._state_root)
        if active is None:
            raise UpgradeConflict("there is no active control generation")
        generations = self._state_root / "generations"
        active_receipt = _generation_receipt(_generation_path(generations, active))
        if active_receipt.get("previous_generation") != generation_id:
            raise UpgradeConflict("rollback target is not the recorded predecessor")
        target_path = _generation_path(generations, generation_id)
        target_receipt = _generation_receipt(target_path)
        self._boundary.restore_generation(target_path)
        _write_atomic(
            self._state_root / "active-generation",
            (generation_id + "\n").encode(),
        )
        evidence = {
            "schema_version": 1,
            "from_generation": active,
            "to_generation": generation_id,
            "from_release_digest": active_receipt.get("release_digest"),
            "to_release_digest": target_receipt.get("release_digest"),
            "status": "rolled-back",
        }
        _write_atomic(
            self._state_root / f"rollback-{active}.json",
            _canonical(evidence),
        )
        return ControlGenerationResult(
            generation_id=generation_id,
            release_digest=_receipt_string(target_receipt, "release_digest"),
            build_digest=_receipt_string(target_receipt, "build_digest"),
            previous_generation=active,
            status="rolled-back",
        )


class _DuplicateReceiptField(ValueError):
    pass


def _open_read_directory(path: Path, label: str) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise UpgradeConflict(f"{label} is missing or unsafe") from error
    try:
        _validate_read_metadata(os.fstat(descriptor), label, directory=True)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_read_directory_at(parent: int, name: str, label: str) -> int:
    if _GENERATION.fullmatch(name) is None:
        raise UpgradeConflict(f"{label} name is invalid")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent,
        )
    except OSError as error:
        raise UpgradeConflict(f"{label} is missing or unsafe") from error
    try:
        _validate_read_metadata(os.fstat(descriptor), label, directory=True)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _read_bounded_file(
    parent: int,
    name: str,
    maximum: int,
    label: str,
) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent,
        )
    except OSError as error:
        raise UpgradeConflict(f"{label} is missing or unsafe") from error
    try:
        before = os.fstat(descriptor)
        _validate_read_metadata(before, label, directory=False)
        if not 0 < before.st_size <= maximum:
            raise UpgradeConflict(f"{label} size is invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                raise UpgradeConflict(f"{label} changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise UpgradeConflict(f"{label} changed while being read")
        content = b"".join(chunks)
        os.lseek(descriptor, 0, os.SEEK_SET)
        repeated = bytearray()
        while len(repeated) < before.st_size:
            chunk = os.read(
                descriptor,
                min(65536, before.st_size - len(repeated)),
            )
            if not chunk:
                raise UpgradeConflict(f"{label} changed while being read")
            repeated.extend(chunk)
        if os.read(descriptor, 1) or bytes(repeated) != content:
            raise UpgradeConflict(f"{label} changed while being read")
        after = os.fstat(descriptor)
        _validate_read_metadata(after, label, directory=False)
        if _file_identity(before) != _file_identity(after):
            raise UpgradeConflict(f"{label} changed while being read")
        return content
    except OSError as error:
        raise UpgradeConflict(f"{label} is unreadable") from error
    finally:
        os.close(descriptor)


def _validate_read_metadata(
    metadata: os.stat_result, label: str, *, directory: bool
) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode):
        raise UpgradeConflict(f"{label} is unsafe")
    if metadata.st_uid not in {0, os.geteuid()}:
        raise UpgradeConflict(f"{label} ownership is unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise UpgradeConflict(f"{label} permissions are unsafe")
    if not directory and metadata.st_nlink != 1:
        raise UpgradeConflict(f"{label} hard-link count is unsafe")


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _parse_active_marker(raw: bytes) -> str:
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise UpgradeConflict("active generation marker is invalid") from error
    if not value.endswith("\n") or value.count("\n") != 1:
        raise UpgradeConflict("active generation marker is noncanonical")
    generation_id = value[:-1]
    if _GENERATION.fullmatch(generation_id) is None:
        raise UpgradeConflict("active generation marker is invalid")
    return generation_id


def _unique_receipt_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise _DuplicateReceiptField(name)
        result[name] = value
    return result


def _reject_receipt_constant(value: str) -> object:
    raise ValueError(value)


def _parse_active_receipt(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_receipt_object,
            parse_constant=_reject_receipt_constant,
        )
    except _DuplicateReceiptField as error:
        raise UpgradeConflict(
            "active generation receipt has a duplicate field"
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise UpgradeConflict("active generation receipt is not valid JSON") from error
    if not isinstance(value, dict):
        raise UpgradeConflict("active generation receipt is invalid")
    if raw != _canonical(value):
        raise UpgradeConflict("active generation receipt is not canonical")
    required = {
        "api_image",
        "backup",
        "build_digest",
        "compose_sha256",
        "generation_id",
        "migration_revision",
        "platform_version",
        "previous_generation",
        "readiness",
        "release_digest",
        "schema_version",
        "status",
        "worker_image",
    }
    if set(value) != required:
        raise UpgradeConflict("active generation receipt fields are invalid")
    previous = value["previous_generation"]
    valid = (
        isinstance(value["schema_version"], int)
        and not isinstance(value["schema_version"], bool)
        and value["schema_version"] == 1
        and isinstance(value["generation_id"], str)
        and _GENERATION.fullmatch(value["generation_id"]) is not None
        and isinstance(value["release_digest"], str)
        and _DIGEST.fullmatch(value["release_digest"]) is not None
        and isinstance(value["build_digest"], str)
        and _DIGEST.fullmatch(value["build_digest"]) is not None
        and isinstance(value["platform_version"], str)
        and 0 < len(value["platform_version"]) <= 32
        and isinstance(value["api_image"], str)
        and _IMAGE.fullmatch(value["api_image"]) is not None
        and isinstance(value["worker_image"], str)
        and _IMAGE.fullmatch(value["worker_image"]) is not None
        and isinstance(value["migration_revision"], str)
        and _GENERATION.fullmatch(value["migration_revision"]) is not None
        and (
            previous is None
            or (
                isinstance(previous, str)
                and _GENERATION.fullmatch(previous) is not None
            )
        )
        and isinstance(value["compose_sha256"], str)
        and _SHA256.fullmatch(value["compose_sha256"]) is not None
        and isinstance(value["backup"], dict)
        and bool(value["backup"])
        and isinstance(value["readiness"], dict)
        and bool(value["readiness"])
        and value["status"] == "active"
    )
    if not valid:
        raise UpgradeConflict("active generation receipt is malformed")
    return value


def _required_bytes(release: PlatformRelease) -> int:
    artifacts = (
        release.control.api_image,
        release.control.worker_image,
        *release.control.assets,
    )
    return sum(item.size for item in artifacts) + 1024 * 1024


def _active_generation(state_root: Path) -> str | None:
    path = state_root / "active-generation"
    try:
        if path.is_symlink():
            raise UpgradeConflict("active generation marker is unsafe")
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise UpgradeConflict("active generation marker is unreadable") from error
    if _GENERATION.fullmatch(value) is None:
        raise UpgradeConflict("active generation marker is invalid")
    return value


def _generation_path(generations: Path, generation_id: str) -> Path:
    if _GENERATION.fullmatch(generation_id) is None:
        raise UpgradeConflict("generation ID is invalid")
    path = generations / generation_id
    if path.is_symlink() or not path.is_dir():
        raise UpgradeRecoveryRequired("previous generation is unavailable")
    return path


def _generation_receipt(generation: Path) -> dict[str, object]:
    path = generation / "generation.json"
    try:
        if path.is_symlink():
            raise UpgradeConflict("generation receipt is unsafe")
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise UpgradeConflict("generation receipt is unreadable") from error
    if not isinstance(value, dict) or value.get("generation_id") != generation.name:
        raise UpgradeConflict("generation receipt is invalid")
    return value


def _receipt_string(receipt: dict[str, object], name: str) -> str:
    value = receipt.get(name)
    if not isinstance(value, str):
        raise UpgradeConflict(f"generation receipt {name} is invalid")
    return value


def _secure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise UpgradeConflict("upgrade state directory is unsafe") from error
    if (
        not path.is_dir()
        or path.is_symlink()
        or metadata.st_uid not in {0, os.geteuid()}
    ):
        raise UpgradeConflict("upgrade state directory is unsafe")
    os.chmod(path, 0o700)


def _write_new(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    try:
        _write_new(temporary, content)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise UpgradeError("control generation write was incomplete")
        offset += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _json_mapping(value: object, label: str) -> None:
    if not isinstance(value, dict) or not value:
        raise UpgradeError(f"{label} is invalid")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise UpgradeError(f"{label} is invalid") from error
