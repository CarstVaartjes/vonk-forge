from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from vonk_control import offline
from vonk_control import upgrade as upgrade_module
from vonk_control.host_commands import CommandResult, HostCommandError
from vonk_control.host_state import (
    GenerationReceipt,
    HostGenerationStore,
    HostOperationLock,
    HostStateConflict,
    SelectionReceipt,
)
from vonk_control.upgrade import (
    ActiveControlReleaseLoader,
    AmbiguousMigrationError,
    ControlGenerationPlan,
    ControlUpgrade,
    PhaseObservation,
    ProbeDisposition,
    RunningControlIdentity,
    UpgradeConflict,
    UpgradeError,
    UpgradePhase,
    UpgradeReadinessError,
    UpgradeRecoveryRequired,
)

from cluster_profiles.platform_release import PlatformRelease

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _artifact(name: str, digest: str) -> dict[str, object]:
    return {
        "name": name,
        "reference": f"ghcr.io/example/vonk-forge/{name}@sha256:{digest}",
        "sha256": digest,
        "size": 1024,
        "sbom_sha256": SHA_D,
        "provenance_sha256": SHA_E,
    }


def _payload(name: str, digest: str) -> dict[str, object]:
    return {"name": name, "sha256": digest, "size": 4096}


def _release(tmp_path: Path) -> PlatformRelease:
    document = {
        "schema_version": 2,
        "platform_version": "1.2.0",
        "build_digest": f"sha256:{SHA_A}",
        "host_updater_abi": {"minimum": 2, "maximum": 3},
        "deployment_bundle": {
            "reference": (
                f"ghcr.io/example/vonk-forge/control-deployment@sha256:{SHA_A}"
            ),
            "manifest_digest": f"sha256:{SHA_A}",
            "manifest_size": 4096,
            "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
            "layer_digest": f"sha256:{SHA_B}",
            "layer_size": 1048576,
            "layer_media_type": ("application/vnd.vonk-forge.control-deployment.v1.tar"),
        },
        "control": {
            "config_version": 3,
            "protocol": {"minimum": 1, "maximum": 2},
            "images": {
                "api": _artifact("api", SHA_A),
                "worker": _artifact("worker", SHA_B),
            },
            "assets": [_artifact("web", SHA_C)],
        },
        "database": {
            "expand_revision": "0001_fleet_library_baseline",
            "contract_revision": None,
            "predecessor_compatible": True,
        },
        "agents": [
            {
                "architecture": "linux-arm64",
                "protocol": {"minimum": 1, "maximum": 2},
                "artifact": _artifact("agent-linux-arm64", SHA_A),
                "payload": _payload("vonk-agent", SHA_B),
            }
        ],
        "supervisors": [
            {
                "architecture": "linux-arm64",
                "artifact": _artifact("supervisor-linux-arm64", SHA_B),
                "payload": _payload("vonk-agent-supervisor", SHA_C),
            }
        ],
        "tooling": [
            {
                "architecture": "linux-arm64",
                "artifact": _artifact("tooling-linux-arm64", SHA_C),
                "payload": _payload("vonk-forge-tooling", SHA_D),
            }
        ],
        "rollback": {
            "predecessors": [
                {
                    "target_name": f"platform/releases/1.1.0/{SHA_B}.json",
                    "target_sha256": SHA_B,
                    "release_digest": f"sha256:{SHA_C}",
                    "build_digest": f"sha256:{SHA_B}",
                    "deployment_bundle_digest": f"sha256:{SHA_D}",
                }
            ]
        },
    }
    path = tmp_path / "platform-release.json"
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return PlatformRelease.load(path)


class FakeVersionedReleaseSource:
    def __init__(self, target_name: str, raw: bytes, targets_version: int = 7) -> None:
        self.target_name = target_name
        self.raw = raw
        self.targets_version = targets_version
        self.calls: list[str] = []

    def refresh(self, target_name: str) -> tuple[bytes, int]:
        self.calls.append(target_name)
        if target_name != self.target_name:
            raise KeyError(target_name)
        return self.raw, self.targets_version


class FakeReleaseSet:
    def __init__(self, targets: dict[str, bytes], targets_version: int = 9) -> None:
        self.targets = targets
        self.targets_version = targets_version
        self.calls: list[str] = []

    def refresh(self, target_name: str) -> tuple[bytes, int]:
        self.calls.append(target_name)
        return self.targets[target_name], self.targets_version


def _versioned_release(
    tmp_path: Path,
    *,
    name: str,
    version: str,
    build: str,
    bundle_layer: str,
    predecessor: tuple[str, PlatformRelease] | None,
) -> tuple[PlatformRelease, bytes, str]:
    fixture = tmp_path / f"fixture-{name}"
    fixture.mkdir()
    _release(fixture)
    document = json.loads((fixture / "platform-release.json").read_bytes())
    document["platform_version"] = version
    document["build_digest"] = f"sha256:{build * 64}"
    document["deployment_bundle"]["layer_digest"] = f"sha256:{bundle_layer * 64}"
    if predecessor is None:
        document["rollback"]["predecessors"] = [
            {
                "target_name": f"platform/releases/1.0.0/{'0' * 64}.json",
                "target_sha256": "0" * 64,
                "release_digest": f"sha256:{'0' * 64}",
                "build_digest": f"sha256:{'0' * 64}",
                "deployment_bundle_digest": f"sha256:{'0' * 64}",
            }
        ]
    else:
        predecessor_name, predecessor_release = predecessor
        predecessor_sha = predecessor_name.removesuffix(".json").rsplit("/", 1)[1]
        document["rollback"]["predecessors"] = [
            {
                "target_name": predecessor_name,
                "target_sha256": predecessor_sha,
                "release_digest": predecessor_release.digest,
                "build_digest": predecessor_release.build_digest,
                "deployment_bundle_digest": predecessor_release.deployment_bundle.layer_digest,
            }
        ]
    raw = _canonical_document(document)
    release = PlatformRelease.from_bytes(raw)
    target_sha = hashlib.sha256(raw).hexdigest()
    target_name = f"platform/releases/{version}/{target_sha}.json"
    return release, raw, target_name


def _commit_release_generation(
    store: HostGenerationStore,
    release: PlatformRelease,
    target_name: str,
    *,
    targets_version: int,
) -> GenerationReceipt:
    target_sha = target_name.removesuffix(".json").rsplit("/", 1)[1]
    receipt = GenerationReceipt(
        generation_id="gen-" + target_sha[:24],
        platform_target_name=target_name,
        platform_target_sha256=target_sha,
        tuf_targets_version=targets_version,
        release_digest=release.digest,
        build_digest=release.build_digest,
        platform_version=release.platform_version,
        deployment_bundle_digest=release.deployment_bundle.layer_digest,
        api_image=release.control.api_image.reference,
        worker_image=release.control.worker_image.reference,
        database_revision=release.database.expand_revision,
    )

    def populate(destination: Path) -> None:
        destination.mkdir(mode=0o700)
        (destination / "compose.yaml").write_text("services: {}\n")

    staged = store.prepare_staging(receipt.generation_id, populate)
    store.commit_generation(staged, receipt)
    return receipt


def _exact_apply_with_selected_predecessor(tmp_path: Path):
    old, _old_raw, old_name = _versioned_release(
        tmp_path,
        name="compensation-old",
        version="1.1.0",
        build="1",
        bundle_layer="2",
        predecessor=None,
    )
    _current, current_raw, current_name = _versioned_release(
        tmp_path,
        name="compensation-current",
        version="1.2.0",
        build="3",
        bundle_layer="4",
        predecessor=(old_name, old),
    )
    source = FakeReleaseSet({current_name: current_raw}, targets_version=10)
    state_root = tmp_path / "control-host"
    identity_root = tmp_path / "control-identity"
    store = HostGenerationStore(state_root, identity_root, owner_uid=os.geteuid())
    old_receipt = _commit_release_generation(store, old, old_name, targets_version=9)
    store.select(
        SelectionReceipt.for_generation(
            old_receipt,
            operation_id="select-old",
            plan_digest=f"sha256:{SHA_A}",
            previous_generation=None,
        )
    )
    boundary = FakeUpgradeBoundary()
    boundary.current_database_revision = old.database.expand_revision
    service = ControlUpgrade(
        state_root,
        boundary,
        release_source=source,
        identity_root=identity_root,
        operation_id_factory=lambda: "apply-with-compensation",
        start_nonce_factory=lambda: SHA_D,
        host_owner_uid=os.geteuid(),
    )
    return (
        service,
        service.plan(current_name),
        boundary,
        source,
        state_root,
        identity_root,
        old_receipt,
    )


def _exact_rollback_fixture(tmp_path: Path, *, wrong_bundle: bool = False):
    old, old_raw, old_name = _versioned_release(
        tmp_path,
        name="rollback-old",
        version="1.1.0",
        build="1",
        bundle_layer="2",
        predecessor=None,
    )
    current, current_raw, current_name = _versioned_release(
        tmp_path,
        name="rollback-current",
        version="1.2.0",
        build="3",
        bundle_layer="4",
        predecessor=(old_name, old),
    )
    if wrong_bundle:
        document = json.loads(current_raw)
        document["rollback"]["predecessors"][0]["deployment_bundle_digest"] = (
            f"sha256:{SHA_E}"
        )
        current_raw = _canonical_document(document)
        current = PlatformRelease.from_bytes(current_raw)
        current_sha = hashlib.sha256(current_raw).hexdigest()
        current_name = f"platform/releases/1.2.0/{current_sha}.json"
    source = FakeReleaseSet(
        {old_name: old_raw, current_name: current_raw}, targets_version=10
    )
    state_root = tmp_path / "control-host"
    identity_root = tmp_path / "control-identity"
    store = HostGenerationStore(state_root, identity_root, owner_uid=os.geteuid())
    old_receipt = _commit_release_generation(store, old, old_name, targets_version=9)
    current_receipt = _commit_release_generation(
        store, current, current_name, targets_version=9
    )
    store.select(
        SelectionReceipt.for_generation(
            current_receipt,
            operation_id="select-current",
            plan_digest=f"sha256:{SHA_A}",
            previous_generation=old_receipt.generation_id,
        )
    )
    boundary = FakeUpgradeBoundary()
    boundary.current_database_revision = current.database.expand_revision
    service = ControlUpgrade(
        state_root,
        boundary,
        release_source=source,
        identity_root=identity_root,
        operation_id_factory=lambda: "rollback-exact",
        start_nonce_factory=lambda: SHA_D,
        host_owner_uid=os.geteuid(),
    )
    return service, source, old_receipt, current_name, current_raw, boundary


class FakeUpgradeBoundary:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.online = False
        self.free_bytes = 10 * 1024 * 1024
        self.failure: str | None = None
        self.current_database_revision = "0001_fleet_library_baseline"
        self.site_digest = f"sha256:{SHA_E}"
        self.running = {
            "control-api": {
                "generation_id": None,
                "image": None,
                "status": "absent",
            },
            "control-worker": {
                "generation_id": None,
                "image": None,
                "status": "absent",
            },
        }
        self.phase_effects: dict[UpgradePhase, bool] = {}
        self.phase_counts: dict[UpgradePhase, int] = {}
        self.crash_phase: UpgradePhase | None = None
        self.failure_phase: UpgradePhase | None = None

    def database_revision(self) -> str:
        self.events.append("database-revision")
        return self.current_database_revision

    def site_configuration_digest(self) -> str:
        self.events.append("site-configuration")
        return self.site_digest

    def running_control_identities(self) -> dict[str, object]:
        self.events.append("running-identities")
        return self.running

    def probe_phase(
        self, phase: UpgradePhase, _plan: ControlGenerationPlan
    ) -> PhaseObservation:
        if self.phase_effects.get(phase, False):
            return PhaseObservation(
                ProbeDisposition.EXACT,
                {"generation_id": _plan.generation_id, "phase": phase.value},
            )
        return PhaseObservation(ProbeDisposition.ABSENT, {})

    def perform_phase(self, phase: UpgradePhase, _plan: ControlGenerationPlan) -> None:
        self.phase_counts[phase] = self.phase_counts.get(phase, 0) + 1
        if self.failure_phase is phase:
            raise UpgradeError(f"controlled failure during {phase.value}")
        self.phase_effects[phase] = True
        if self.crash_phase is phase:
            raise RuntimeError("injected crash after exact phase effect")

    def control_is_running(self) -> bool:
        self.events.append("check-online")
        return self.online

    def available_bytes(self) -> int:
        self.events.append("check-disk")
        return self.free_bytes

    def pull(self, references: tuple[str, ...]) -> None:
        self.events.append("pull:" + ",".join(references))

    def render_compose(self, environment: dict[str, str]) -> bytes:
        self.events.append("render")
        return (json.dumps(environment, sort_keys=True) + "\n").encode()

    def backup(self, generation_id: str) -> dict[str, object]:
        self.events.append("backup")
        return {"id": f"backup-{generation_id}", "sha256": SHA_C}

    def stop_worker(self) -> None:
        self.events.append("stop-worker")

    def migrate(self, revision: str) -> None:
        self.events.append(f"migrate:{revision}")
        if self.failure == "migration-ambiguous":
            raise AmbiguousMigrationError("database result is unknown")

    def start_api(self, generation_path: Path) -> None:
        self.events.append(f"start-api:{generation_path.name}")

    def readiness(self) -> dict[str, object]:
        self.events.append("readiness")
        if self.failure == "readiness":
            raise UpgradeReadinessError("candidate did not become ready")
        return {"status": "ready", "probe": "caddy"}

    def start_worker(self) -> None:
        self.events.append("start-worker")

    def stop_api(self) -> None:
        self.events.append("stop-api")

    def restore_generation(self, generation_path: Path) -> None:
        self.events.append(f"restore:{generation_path.name}")


def _seed_previous(state_root: Path) -> str:
    generation_id = "previous-generation"
    generation = state_root / "generations" / generation_id
    generation.mkdir(parents=True, mode=0o700)
    state_root.chmod(0o700)
    (state_root / "generations").chmod(0o700)
    (generation / "generation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation_id": generation_id,
                "release_digest": f"sha256:{SHA_B}",
                "build_digest": f"sha256:{SHA_B}",
                "status": "active",
            }
        ),
        encoding="utf-8",
    )
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "active-generation").write_text(
        generation_id + "\n", encoding="utf-8"
    )
    return generation_id


def _seed_active_release(
    tmp_path: Path,
) -> tuple[Path, PlatformRelease, bytes, str]:
    state_root = tmp_path / "state"
    backend = FakeUpgradeBoundary()
    release = _release(tmp_path)
    target = (tmp_path / "platform-release.json").read_bytes()
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())
    result = upgrade.apply(upgrade.plan(release), release)
    return state_root, release, target, result.generation_id


def _canonical_document(document: object) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _running_identity(release: PlatformRelease) -> RunningControlIdentity:
    return RunningControlIdentity(
        release_digest=release.digest,
        build_digest=release.build_digest,
        platform_version=release.platform_version,
    )


def _active_loader(
    state_root: Path,
    target: bytes,
    release: PlatformRelease,
) -> ActiveControlReleaseLoader:
    return ActiveControlReleaseLoader(
        state_root,
        lambda: target,
        lambda: _running_identity(release),
    )


def test_active_control_release_is_projected_only_from_verified_target_and_receipt(
    tmp_path: Path,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    calls = 0

    def verified_target() -> bytes:
        nonlocal calls
        calls += 1
        return target

    active = ActiveControlReleaseLoader(
        state_root,
        verified_target,
        lambda: _running_identity(release),
    ).load()

    assert calls == 1
    assert active.generation_id == generation_id
    assert active.release_digest == release.digest
    assert active.build_digest == release.build_digest
    assert active.platform_version == release.platform_version
    assert active.api_image == release.control.api_image.reference
    assert active.worker_image == release.control.worker_image.reference
    assert active.migration_revision == release.database.expand_revision


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("release_digest", f"sha256:{SHA_E}"),
        ("build_digest", f"sha256:{SHA_E}"),
        ("platform_version", "1.3.0"),
    ),
)
def test_active_control_release_requires_exact_running_container_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    state_root, release, target, _generation_id = _seed_active_release(tmp_path)
    identity = {
        "release_digest": release.digest,
        "build_digest": release.build_digest,
        "platform_version": release.platform_version,
    }
    identity[field] = value

    with pytest.raises(UpgradeConflict, match="running control identity"):
        ActiveControlReleaseLoader(
            state_root,
            lambda: target,
            lambda: RunningControlIdentity(**identity),
        ).load()


def test_active_control_release_rejects_missing_or_wrong_running_identity_type(
    tmp_path: Path,
) -> None:
    state_root, _release_value, target, _generation_id = _seed_active_release(tmp_path)
    with pytest.raises(TypeError, match="identity source"):
        ActiveControlReleaseLoader(state_root, lambda: target, None)  # type: ignore[arg-type]

    def missing_identity() -> RunningControlIdentity:
        raise KeyError("VONK_PLATFORM_RELEASE_DIGEST")

    with pytest.raises(UpgradeConflict, match="identity is unavailable"):
        ActiveControlReleaseLoader(state_root, lambda: target, missing_identity).load()

    with pytest.raises(UpgradeConflict, match="identity is invalid"):
        ActiveControlReleaseLoader(
            state_root,
            lambda: target,
            lambda: {"release_digest": f"sha256:{SHA_A}"},  # type: ignore[arg-type,return-value]
        ).load()

    with pytest.raises(ValueError, match="identity is invalid"):
        RunningControlIdentity(
            release_digest=SHA_A,
            build_digest=f"sha256:{SHA_A}",
            platform_version="1.2.0",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("release_digest", f"sha256:{SHA_E}"),
        ("build_digest", f"sha256:{SHA_E}"),
        ("platform_version", "9.9.9"),
        ("api_image", f"ghcr.io/example/api@sha256:{SHA_E}"),
        ("worker_image", f"ghcr.io/example/worker@sha256:{SHA_E}"),
        ("migration_revision", "arbitrary-revision"),
    ),
)
def test_active_control_release_rejects_receipt_target_disagreement(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    receipt_path = state_root / "generations" / generation_id / "generation.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt[field] = value
    receipt_path.write_bytes(_canonical_document(receipt))

    with pytest.raises(UpgradeConflict, match="verified platform release"):
        _active_loader(state_root, target, release).load()


def test_active_control_release_rejects_noncanonical_or_duplicate_receipts(
    tmp_path: Path,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    receipt_path = state_root / "generations" / generation_id / "generation.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    loader = _active_loader(state_root, target, release)

    with pytest.raises(UpgradeConflict, match="canonical"):
        loader.load()

    canonical = _canonical_document(receipt)
    receipt_path.write_bytes(
        canonical.replace(b'{"api_image":', b'{"schema_version":1,"api_image":', 1)
    )
    with pytest.raises(UpgradeConflict, match="duplicate"):
        loader.load()


def test_active_control_release_rejects_oversized_or_malformed_receipts(
    tmp_path: Path,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    receipt_path = state_root / "generations" / generation_id / "generation.json"
    loader = _active_loader(state_root, target, release)
    receipt_path.write_bytes(b"{" + b" " * (128 * 1024) + b"}")
    with pytest.raises(UpgradeConflict, match="size"):
        loader.load()

    receipt_path.write_bytes(b"not-json\n")
    with pytest.raises(UpgradeConflict, match="JSON"):
        loader.load()


def test_active_control_release_rejects_noncanonical_pointer_and_symlinks(
    tmp_path: Path,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    pointer = state_root / "active-generation"
    pointer.write_text(f" {generation_id}\n", encoding="utf-8")
    loader = _active_loader(state_root, target, release)
    with pytest.raises(UpgradeConflict, match="marker"):
        loader.load()

    pointer.unlink()
    pointer.symlink_to(state_root / "pointer-target")
    with pytest.raises(UpgradeConflict, match="unsafe"):
        loader.load()


def test_active_control_release_rejects_pointer_receipt_disagreement(
    tmp_path: Path,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    receipt_path = state_root / "generations" / generation_id / "generation.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["generation_id"] = "different-generation"
    receipt_path.write_bytes(_canonical_document(receipt))

    with pytest.raises(UpgradeConflict, match="generation binding"):
        _active_loader(state_root, target, release).load()


def test_active_control_release_requires_digest_derived_generation_id(
    tmp_path: Path,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    generations = state_root / "generations"
    original = generations / generation_id
    renamed = generations / "gen-arbitrary-active-release"
    original.rename(renamed)
    receipt_path = renamed / "generation.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["generation_id"] = renamed.name
    receipt_path.write_bytes(_canonical_document(receipt))
    (state_root / "active-generation").write_text(renamed.name + "\n", encoding="ascii")

    with pytest.raises(UpgradeConflict, match="generation ID"):
        _active_loader(state_root, target, release).load()


@pytest.mark.parametrize("hardlink_name", ("active-generation", "generation.json"))
def test_active_control_release_rejects_hardlinked_state_files(
    tmp_path: Path,
    hardlink_name: str,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    if hardlink_name == "active-generation":
        protected = state_root / hardlink_name
    else:
        protected = state_root / "generations" / generation_id / hardlink_name
    (tmp_path / f"outside-{hardlink_name}").hardlink_to(protected)

    with pytest.raises(UpgradeConflict, match="hard-link count"):
        _active_loader(state_root, target, release).load()


def test_active_control_release_rejects_receipt_rewrite_during_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    receipt_path = state_root / "generations" / generation_id / "generation.json"
    original_read = upgrade_module.os.read
    rewritten = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal rewritten
        chunk = original_read(descriptor, count)
        if not rewritten and chunk.startswith(b"{"):
            rewritten = True
            receipt = receipt_path.read_bytes()
            receipt_path.write_bytes(
                receipt.replace(b'"status":"active"', b'"status":"failed"')
            )
        return chunk

    monkeypatch.setattr(upgrade_module.os, "read", racing_read)

    with pytest.raises(UpgradeConflict, match="changed while being read"):
        _active_loader(state_root, target, release).load()


def test_active_control_release_rejects_missing_or_writable_state(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    calls = 0

    def verified_target() -> bytes:
        nonlocal calls
        calls += 1
        return b"arbitrary"

    running = RunningControlIdentity(
        release_digest=f"sha256:{SHA_A}",
        build_digest=f"sha256:{SHA_A}",
        platform_version="1.2.0",
    )

    with pytest.raises(UpgradeConflict, match="state root"):
        ActiveControlReleaseLoader(missing, verified_target, lambda: running).load()
    assert calls == 0

    state_root, release, target, _generation_id = _seed_active_release(tmp_path)
    state_root.chmod(0o777)
    with pytest.raises(UpgradeConflict, match="permissions"):
        _active_loader(state_root, target, release).load()


def test_active_control_release_rejects_unverified_target_shape(
    tmp_path: Path,
) -> None:
    state_root, release, _target, _generation_id = _seed_active_release(tmp_path)

    with pytest.raises(UpgradeConflict, match="verified platform release target"):
        ActiveControlReleaseLoader(
            state_root,
            lambda: b"arbitrary",
            lambda: _running_identity(release),
        ).load()


def test_active_control_release_rejects_a_different_valid_verified_target(
    tmp_path: Path,
) -> None:
    state_root, release, target, _generation_id = _seed_active_release(tmp_path)
    other = json.loads(target)
    other["platform_version"] = "1.3.0"

    with pytest.raises(UpgradeConflict, match="verified platform release"):
        ActiveControlReleaseLoader(
            state_root,
            lambda: _canonical_document(other),
            lambda: _running_identity(release),
        ).load()


def test_active_control_release_rejects_symlinked_generation_receipt(
    tmp_path: Path,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    receipt = state_root / "generations" / generation_id / "generation.json"
    outside = tmp_path / "outside-generation.json"
    receipt.replace(outside)
    receipt.symlink_to(outside)

    with pytest.raises(UpgradeConflict, match="unsafe"):
        _active_loader(state_root, target, release).load()


def test_active_control_release_rejects_symlinked_generation_directory(
    tmp_path: Path,
) -> None:
    state_root, release, target, generation_id = _seed_active_release(tmp_path)
    generation = state_root / "generations" / generation_id
    outside = tmp_path / "outside-generation"
    generation.replace(outside)
    generation.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UpgradeConflict, match="unsafe"):
        _active_loader(state_root, target, release).load()


def test_upgrade_plan_is_deterministic_and_dry_run_mutates_nothing(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())

    first = upgrade.plan(_release(tmp_path))
    second = upgrade.plan(_release(tmp_path))

    assert first == second
    assert first.plan_digest.startswith("sha256:")
    assert first.api_image.endswith(f"@sha256:{SHA_A}")
    assert first.worker_image.endswith(f"@sha256:{SHA_B}")
    assert backend.events == []
    assert not state_root.exists()


def test_exact_plan_resolves_caller_selected_versioned_target_and_bundle(
    tmp_path: Path,
) -> None:
    """Resolving a fixed/latest target would detach the plan from TUF identity."""

    release = _release(tmp_path)
    raw = (tmp_path / "platform-release.json").read_bytes()
    target_sha256 = hashlib.sha256(raw).hexdigest()
    assert release.digest == f"sha256:{target_sha256}"
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    source = FakeVersionedReleaseSource(target_name, raw)
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(
        tmp_path / "control-host",
        backend,
        release_source=source,
        operation_id_factory=lambda: "operation-1",
        start_nonce_factory=lambda: SHA_D,
    )

    plan = upgrade.plan(target_name)

    assert source.calls == [target_name]
    assert plan.operation_id == "operation-1"
    assert plan.start_nonce == SHA_D
    assert plan.platform_target_name == target_name
    assert plan.platform_target_sha256 == target_sha256
    assert plan.tuf_targets_version == 7
    assert plan.release_digest == release.digest
    assert plan.deployment_bundle == release.deployment_bundle
    assert plan.deployment_bundle_digest == release.deployment_bundle.layer_digest
    assert plan.api_image == release.control.api_image.reference
    assert plan.worker_image == release.control.worker_image.reference
    assert plan.database_revision == release.database.expand_revision
    assert plan.current_database_revision == "0001_fleet_library_baseline"
    assert plan.previous_generation is None
    assert plan.current_generation_receipt_sha256 is None
    assert plan.current_selection_receipt_sha256 is None
    assert plan.current_projection_sequence is None
    assert plan.site_configuration_digest == f"sha256:{SHA_E}"
    assert (
        plan.running_identity_digest
        == "sha256:" + hashlib.sha256(_canonical_document(backend.running)).hexdigest()
    )
    assert plan.required_bytes >= (
        release.deployment_bundle.manifest_size
        + release.deployment_bundle.layer_size
        + release.control.api_image.size
        + release.control.worker_image.size
    )
    assert (
        plan.plan_digest
        == "sha256:" + hashlib.sha256(plan.canonical_payload()).hexdigest()
    )
    assert backend.events == [
        "database-revision",
        "site-configuration",
        "running-identities",
    ]
    assert not (tmp_path / "control-host").exists()


def test_exact_plan_rejects_target_outside_installed_host_updater_abi(
    tmp_path: Path,
) -> None:
    """Planning an unsupported updater transition would execute candidate tooling."""

    _release(tmp_path)
    raw = (tmp_path / "platform-release.json").read_bytes()
    target_sha256 = hashlib.sha256(raw).hexdigest()
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    source = FakeVersionedReleaseSource(target_name, raw)
    upgrade = ControlUpgrade(
        tmp_path / "control-host",
        FakeUpgradeBoundary(),
        release_source=source,
        host_updater_abi=4,
    )

    with pytest.raises(UpgradeConflict, match="host updater ABI"):
        upgrade.plan(target_name)

    assert not (tmp_path / "control-host").exists()


def test_exact_apply_plan_requires_exact_active_predecessor_descriptor(
    tmp_path: Path,
) -> None:
    old, _old_raw, old_name = _versioned_release(
        tmp_path,
        name="apply-old",
        version="1.1.0",
        build="1",
        bundle_layer="2",
        predecessor=None,
    )
    _target, target_raw, target_name = _versioned_release(
        tmp_path,
        name="apply-unrelated",
        version="1.2.0",
        build="3",
        bundle_layer="4",
        predecessor=None,
    )
    state_root = tmp_path / "control-host"
    identity_root = tmp_path / "control-identity"
    store = HostGenerationStore(state_root, identity_root, owner_uid=os.geteuid())
    old_receipt = _commit_release_generation(store, old, old_name, targets_version=9)
    store.select(
        SelectionReceipt.for_generation(
            old_receipt,
            operation_id="select-old",
            plan_digest=f"sha256:{SHA_A}",
            previous_generation=None,
        )
    )
    boundary = FakeUpgradeBoundary()
    boundary.current_database_revision = old.database.expand_revision
    service = ControlUpgrade(
        state_root,
        boundary,
        release_source=FakeReleaseSet({target_name: target_raw}),
        identity_root=identity_root,
        host_owner_uid=os.geteuid(),
    )

    with pytest.raises(UpgradeConflict, match="exact active predecessor"):
        service.plan(target_name)


def test_exact_plan_round_trip_recomputes_digest_for_recovery(
    tmp_path: Path,
) -> None:
    """Trusting a stored digest without recomputation would authorize plan tampering."""

    _release(tmp_path)
    raw = (tmp_path / "platform-release.json").read_bytes()
    target_sha256 = hashlib.sha256(raw).hexdigest()
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    plan = ControlUpgrade(
        tmp_path / "control-host",
        FakeUpgradeBoundary(),
        release_source=FakeVersionedReleaseSource(target_name, raw),
        operation_id_factory=lambda: "operation-1",
        start_nonce_factory=lambda: SHA_D,
    ).plan(target_name)

    assert ControlGenerationPlan.from_document(plan.document()) == plan
    tampered = plan.document()
    tampered["required_bytes"] = plan.required_bytes + 1
    with pytest.raises(UpgradeConflict, match="plan digest"):
        ControlGenerationPlan.from_document(tampered)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", 7),
        ("start_nonce", []),
        ("current_generation_receipt_sha256", 7),
        ("deployment_bundle.layer_size", "unbounded"),
    ],
)
def test_exact_plan_recovery_rejects_malformed_types_fail_closed(
    tmp_path: Path, field: str, value: object
) -> None:
    """Malformed root-owned journal JSON must not leak parser type errors."""

    _release(tmp_path)
    raw = (tmp_path / "platform-release.json").read_bytes()
    target_sha256 = hashlib.sha256(raw).hexdigest()
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    plan = ControlUpgrade(
        tmp_path / "control-host",
        FakeUpgradeBoundary(),
        release_source=FakeVersionedReleaseSource(target_name, raw),
        operation_id_factory=lambda: "operation-1",
        start_nonce_factory=lambda: SHA_D,
    ).plan(target_name)
    document = plan.document()
    if field.startswith("deployment_bundle."):
        bundle = dict(document["deployment_bundle"])
        bundle[field.rsplit(".", 1)[1]] = value
        document["deployment_bundle"] = bundle
    else:
        document[field] = value

    with pytest.raises(UpgradeConflict, match="plan"):
        ControlGenerationPlan.from_document(document)


@pytest.mark.parametrize(
    "crash_phase",
    [
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
    ],
)
def test_exact_apply_recovers_effect_completed_before_journal_append(
    tmp_path: Path, crash_phase: UpgradePhase
) -> None:
    """Every exact effect is adopted after a crash before its journal append."""

    _release(tmp_path)
    raw = (tmp_path / "platform-release.json").read_bytes()
    target_sha256 = hashlib.sha256(raw).hexdigest()
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    source = FakeVersionedReleaseSource(target_name, raw)
    boundary = FakeUpgradeBoundary()
    boundary.crash_phase = crash_phase
    state_root = tmp_path / "control-host"
    service = ControlUpgrade(
        state_root,
        boundary,
        release_source=source,
        operation_id_factory=lambda: "operation-1",
        start_nonce_factory=lambda: SHA_D,
        host_owner_uid=os.geteuid(),
    )
    plan = service.plan(target_name)

    with pytest.raises(RuntimeError, match="injected crash"):
        service.apply(plan)

    boundary.crash_phase = None
    recovered = ControlUpgrade(
        state_root,
        boundary,
        release_source=source,
        host_owner_uid=os.geteuid(),
    ).recover()

    assert recovered.status == "active"
    assert recovered.generation_id == plan.generation_id
    apply_phases = (
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
    for phase in apply_phases:
        if phase is not UpgradePhase.AUTHORIZED:
            assert boundary.phase_counts[phase] == 1
    operation = state_root / "operations/operation-1"
    assert [
        path.name.split("-", 1)[1] for path in sorted(operation.glob("[0-9]*.json"))
    ] == [f"{phase.value}.json" for phase in apply_phases]


def test_exact_two_argument_apply_preserves_operation_identity(
    tmp_path: Path,
) -> None:
    """Compatibility release input must not regenerate operation-scoped fields."""

    release = _release(tmp_path)
    raw = (tmp_path / "platform-release.json").read_bytes()
    target_sha256 = hashlib.sha256(raw).hexdigest()
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    operation_calls = 0
    nonce_calls = 0

    def operation_id() -> str:
        nonlocal operation_calls
        operation_calls += 1
        return f"operation-{operation_calls}"

    def start_nonce() -> str:
        nonlocal nonce_calls
        nonce_calls += 1
        return f"{nonce_calls:064x}"

    state_root = tmp_path / "control-host"
    service = ControlUpgrade(
        state_root,
        FakeUpgradeBoundary(),
        release_source=FakeVersionedReleaseSource(target_name, raw),
        operation_id_factory=operation_id,
        start_nonce_factory=start_nonce,
        host_owner_uid=os.geteuid(),
    )
    plan = service.plan(target_name)

    result = service.apply(plan, release)

    assert result.status == "active"
    assert operation_calls == 1
    assert nonce_calls == 1
    assert plan.operation_id == "operation-1"
    assert (state_root / "operations" / plan.operation_id / "plan.json").exists()


@pytest.mark.parametrize(
    "compensation_crash_phase",
    [
        UpgradePhase.COMPENSATION_SERVICES_STOPPED,
        UpgradePhase.BACKUP_RESTORED,
        UpgradePhase.PREDECESSOR_SELECTED,
        UpgradePhase.PREDECESSOR_SERVICES_STARTED,
        UpgradePhase.PREDECESSOR_WORKER_READY,
        UpgradePhase.CANDIDATE_CLEANED,
        UpgradePhase.FAILED,
    ],
)
def test_exact_apply_compensation_restores_backup_and_recovers_its_crash(
    tmp_path: Path, compensation_crash_phase: UpgradePhase
) -> None:
    """Each compensation effect is adopted after a pre-journal crash."""

    (
        service,
        plan,
        boundary,
        source,
        state_root,
        identity_root,
        predecessor,
    ) = _exact_apply_with_selected_predecessor(tmp_path)
    boundary.failure_phase = UpgradePhase.WORKER_READY
    boundary.crash_phase = compensation_crash_phase

    with pytest.raises(RuntimeError, match="injected crash"):
        service.apply(plan)

    boundary.crash_phase = None
    boundary.failure_phase = None
    recovered = ControlUpgrade(
        state_root,
        boundary,
        release_source=source,
        identity_root=identity_root,
        host_owner_uid=os.geteuid(),
    ).recover()

    assert recovered.status == "rolled-back"
    assert recovered.generation_id == predecessor.generation_id
    assert boundary.phase_counts[UpgradePhase.BACKUP_RESTORED] == 1
    compensation = (
        UpgradePhase.COMPENSATION_SERVICES_STOPPED,
        UpgradePhase.BACKUP_RESTORED,
        UpgradePhase.PREDECESSOR_SELECTED,
        UpgradePhase.PREDECESSOR_SERVICES_STARTED,
        UpgradePhase.PREDECESSOR_WORKER_READY,
        UpgradePhase.CANDIDATE_CLEANED,
        UpgradePhase.FAILED,
    )
    for phase in compensation:
        assert boundary.phase_counts[phase] == 1
    entries = sorted(
        (state_root / "operations" / plan.operation_id).glob("[0-9]*.json")
    )
    assert any(path.name.endswith("-compensation-started.json") for path in entries)
    assert entries[-1].name.endswith("-failed.json")


def test_exact_apply_failure_before_migration_does_not_restore_backup(
    tmp_path: Path,
) -> None:
    """Compensating an acquisition error must not overwrite a healthy database."""

    service, plan, boundary, *_rest = _exact_apply_with_selected_predecessor(tmp_path)
    boundary.failure_phase = UpgradePhase.BUNDLE_IMAGES_ACQUIRED

    with pytest.raises(UpgradeError, match="bundle-images-acquired"):
        service.apply(plan)

    assert boundary.phase_counts[UpgradePhase.CANDIDATE_CLEANED] == 1
    assert boundary.phase_counts[UpgradePhase.FAILED] == 1
    assert UpgradePhase.BACKUP_RESTORED not in boundary.phase_counts
    assert UpgradePhase.PREDECESSOR_SELECTED not in boundary.phase_counts


def test_rollback_plan_resolves_active_release_exact_predecessor_not_newer_channel(
    tmp_path: Path,
) -> None:
    """Resolving discovery/latest during rollback could select N+1 instead of N-1."""

    old, old_raw, old_name = _versioned_release(
        tmp_path,
        name="old",
        version="1.1.0",
        build="1",
        bundle_layer="2",
        predecessor=None,
    )
    current, current_raw, current_name = _versioned_release(
        tmp_path,
        name="current",
        version="1.2.0",
        build="3",
        bundle_layer="4",
        predecessor=(old_name, old),
    )
    _newer, newer_raw, newer_name = _versioned_release(
        tmp_path,
        name="newer",
        version="1.3.0",
        build="5",
        bundle_layer="6",
        predecessor=(current_name, current),
    )
    source = FakeReleaseSet(
        {old_name: old_raw, current_name: current_raw, newer_name: newer_raw},
        targets_version=10,
    )
    state_root = tmp_path / "control-host"
    identity_root = tmp_path / "control-identity"
    store = HostGenerationStore(state_root, identity_root, owner_uid=os.geteuid())
    old_receipt = _commit_release_generation(store, old, old_name, targets_version=9)
    current_receipt = _commit_release_generation(
        store, current, current_name, targets_version=9
    )
    store.select(
        SelectionReceipt.for_generation(
            current_receipt,
            operation_id="select-current",
            plan_digest=f"sha256:{SHA_A}",
            previous_generation=old_receipt.generation_id,
        )
    )
    boundary = FakeUpgradeBoundary()
    boundary.current_database_revision = current.database.expand_revision
    service = ControlUpgrade(
        state_root,
        boundary,
        release_source=source,
        identity_root=identity_root,
        operation_id_factory=lambda: "rollback-1",
        start_nonce_factory=lambda: SHA_D,
        host_owner_uid=os.geteuid(),
    )

    plan = service.rollback_plan(old_receipt.generation_id)

    assert plan.operation_kind == "rollback"
    assert plan.generation_id == old_receipt.generation_id
    assert plan.previous_generation == current_receipt.generation_id
    assert plan.platform_target_name == old_name
    assert plan.tuf_targets_version == 10
    assert plan.release_digest == old.digest
    assert plan.deployment_bundle_digest == old.deployment_bundle.layer_digest
    assert source.calls == [current_name, old_name]
    assert newer_name not in source.calls

    source.calls.clear()
    result = service.rollback(plan)
    assert result.status == "rolled-back"
    assert set(boundary.phase_counts) == {
        UpgradePhase.SERVICES_STOPPED,
        UpgradePhase.GENERATION_SELECTED,
        UpgradePhase.SERVICES_STARTED,
        UpgradePhase.WORKER_READY,
        UpgradePhase.ROLLED_BACK,
    }
    assert UpgradePhase.BACKUP_COMPLETED not in boundary.phase_counts
    assert UpgradePhase.SERVICES_STOPPED_DATABASE_MIGRATED not in boundary.phase_counts
    assert UpgradePhase.CANDIDATE_READY not in boundary.phase_counts


@pytest.mark.parametrize("mode", ["revoked", "tampered", "wrong-bundle"])
def test_rollback_plan_rejects_untrusted_or_mismatched_predecessor(
    tmp_path: Path, mode: str
) -> None:
    """Only the still-authorized bytes named exactly by N may restore N-1."""

    service, source, predecessor, _current_name, _current_raw, _boundary = (
        _exact_rollback_fixture(tmp_path, wrong_bundle=mode == "wrong-bundle")
    )
    if mode == "revoked":
        source.targets.pop(predecessor.platform_target_name)
    elif mode == "tampered":
        source.targets[predecessor.platform_target_name] += b" "

    with pytest.raises(UpgradeConflict, match="rollback"):
        service.rollback_plan(predecessor.generation_id)


def test_rollback_revalidates_selected_release_and_predecessor_under_lock(
    tmp_path: Path,
) -> None:
    """A plan-time N manifest cannot authorize rollback after N changes."""

    service, source, predecessor, current_name, current_raw, _boundary = (
        _exact_rollback_fixture(tmp_path)
    )
    plan = service.rollback_plan(predecessor.generation_id)
    source.targets[current_name] = current_raw + b" "

    with pytest.raises(UpgradeConflict, match="rollback source"):
        service.rollback(plan)


@pytest.mark.parametrize(
    "crash_phase",
    [
        UpgradePhase.SERVICES_STOPPED,
        UpgradePhase.GENERATION_SELECTED,
        UpgradePhase.SERVICES_STARTED,
        UpgradePhase.WORKER_READY,
        UpgradePhase.ROLLED_BACK,
    ],
)
def test_exact_rollback_recovers_each_effect_before_journal_append(
    tmp_path: Path, crash_phase: UpgradePhase
) -> None:
    service, source, predecessor, _current_name, _current_raw, boundary = (
        _exact_rollback_fixture(tmp_path)
    )
    plan = service.rollback_plan(predecessor.generation_id)
    boundary.crash_phase = crash_phase

    with pytest.raises(RuntimeError, match="injected crash"):
        service.rollback(plan)

    boundary.crash_phase = None
    recovered = ControlUpgrade(
        tmp_path / "control-host",
        boundary,
        release_source=source,
        identity_root=tmp_path / "control-identity",
        host_owner_uid=os.geteuid(),
    ).recover()

    assert recovered.status == "rolled-back"
    assert recovered.generation_id == predecessor.generation_id
    assert boundary.phase_counts[crash_phase] == 1


def test_upgrade_rejects_running_control_plane_before_mutation(tmp_path: Path) -> None:
    backend = FakeUpgradeBoundary()
    backend.online = True
    upgrade = ControlUpgrade(tmp_path / "state", backend, host_owner_uid=os.geteuid())
    release = _release(tmp_path)

    with pytest.raises(UpgradeConflict, match="running"):
        upgrade.apply(upgrade.plan(release), release)

    assert backend.events == ["check-online"]
    assert not (tmp_path / "state").exists()


def test_upgrade_rejects_an_active_offline_maintenance_lock(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())
    release = _release(tmp_path)

    with (
        HostOperationLock(state_root, owner_uid=os.geteuid()),
        pytest.raises(UpgradeConflict, match="host operation"),
    ):
        upgrade.apply(upgrade.plan(release), release)

    assert not (state_root / "generations").exists()


def test_apply_operation_lock_is_held_through_candidate_readiness(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())
    release = _release(tmp_path)
    original = backend.readiness

    def readiness_while_locked() -> dict[str, object]:
        with (
            pytest.raises(HostStateConflict, match="operation is active"),
            HostOperationLock(state_root, owner_uid=os.geteuid()),
        ):
            pass
        return original()

    backend.readiness = readiness_while_locked
    upgrade.apply(upgrade.plan(release), release)


def test_rollback_operation_lock_is_held_through_restore_callback(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    previous = _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())
    release = _release(tmp_path)
    upgrade.apply(upgrade.plan(release), release)
    original = backend.restore_generation

    def restore_while_locked(generation_path: Path) -> None:
        with (
            pytest.raises(HostStateConflict, match="operation is active"),
            HostOperationLock(state_root, owner_uid=os.geteuid()),
        ):
            pass
        original(generation_path)

    backend.restore_generation = restore_while_locked
    upgrade.rollback(previous)


def test_upgrade_applies_backup_migration_readiness_and_commit_in_order(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    previous = _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())
    release = _release(tmp_path)
    plan = upgrade.plan(release)

    original_start_worker = backend.start_worker

    def start_worker_after_activation() -> None:
        generation = state_root / "generations" / plan.generation_id
        assert (generation / "generation.json").is_file()
        assert (
            state_root / "active-generation"
        ).read_text().strip() == plan.generation_id
        original_start_worker()

    backend.start_worker = start_worker_after_activation

    result = upgrade.apply(plan, release)

    assert result.status == "active"
    assert result.previous_generation == previous
    assert backend.events[:5] == [
        "check-online",
        "check-disk",
        (
            "pull:ghcr.io/example/vonk-forge/api@sha256:"
            f"{SHA_A},ghcr.io/example/vonk-forge/worker@sha256:{SHA_B}"
        ),
        "render",
        "backup",
    ]
    assert backend.events.index("backup") < backend.events.index(
        "migrate:0001_fleet_library_baseline"
    )
    assert backend.events.index("stop-worker") < backend.events.index(
        "migrate:0001_fleet_library_baseline"
    )
    assert backend.events.index("readiness") < backend.events.index("start-worker")
    assert (
        state_root / "active-generation"
    ).read_text().strip() == result.generation_id
    receipt = json.loads(
        (
            state_root / "generations" / result.generation_id / "generation.json"
        ).read_text()
    )
    assert receipt["release_digest"] == release.digest
    assert receipt["backup"]["sha256"] == SHA_C
    assert receipt["readiness"]["probe"] == "caddy"


def test_worker_start_failure_reselects_previous_generation(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    previous = _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())
    release = _release(tmp_path)
    plan = upgrade.plan(release)

    def fail_after_candidate_activation() -> None:
        generation = state_root / "generations" / plan.generation_id
        assert (generation / "generation.json").is_file()
        assert (
            state_root / "active-generation"
        ).read_text().strip() == plan.generation_id
        backend.events.append("start-worker")
        raise UpgradeError("candidate worker failed to start")

    backend.start_worker = fail_after_candidate_activation

    with pytest.raises(UpgradeError, match="worker failed"):
        upgrade.apply(plan, release)

    assert backend.events[-3:] == [
        "start-worker",
        "stop-api",
        f"restore:{previous}",
    ]
    assert (state_root / "active-generation").read_text().strip() == previous


def test_failed_readiness_restores_previous_generation(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    previous = _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    backend.failure = "readiness"
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())
    release = _release(tmp_path)

    with pytest.raises(UpgradeReadinessError):
        upgrade.apply(upgrade.plan(release), release)

    assert backend.events[-2:] == ["stop-api", f"restore:{previous}"]
    assert (state_root / "active-generation").read_text().strip() == previous


def test_ambiguous_database_failure_enters_operator_recovery(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    previous = _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    backend.failure = "migration-ambiguous"
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())
    release = _release(tmp_path)
    plan = upgrade.plan(release)

    with pytest.raises(UpgradeRecoveryRequired):
        upgrade.apply(plan, release)

    assert not any(event.startswith("restore:") for event in backend.events)
    recovery = json.loads((state_root / "recovery-required.json").read_text())
    assert recovery["generation_id"] == plan.generation_id
    assert recovery["previous_generation"] == previous
    assert recovery["phase"] == "migration-ambiguous"


def test_upgrade_rejects_insufficient_space_before_pull_or_backup(
    tmp_path: Path,
) -> None:
    backend = FakeUpgradeBoundary()
    backend.free_bytes = 1
    upgrade = ControlUpgrade(tmp_path / "state", backend, host_owner_uid=os.geteuid())
    release = _release(tmp_path)

    with pytest.raises(UpgradeConflict, match="disk space"):
        upgrade.apply(upgrade.plan(release), release)

    assert backend.events == ["check-online", "check-disk"]


def test_explicit_rollback_selects_only_recorded_previous_generation(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    previous = _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())
    release = _release(tmp_path)
    active = upgrade.apply(upgrade.plan(release), release)
    backend.events.clear()
    backend.online = False

    result = upgrade.rollback(previous)

    assert result.status == "rolled-back"
    assert result.generation_id == previous
    assert result.previous_generation == active.generation_id
    assert backend.events == ["check-online", f"restore:{previous}"]
    assert (state_root / "active-generation").read_text().strip() == previous


def test_rollback_rejects_unrecorded_or_running_target(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend, host_owner_uid=os.geteuid())

    with pytest.raises(UpgradeConflict, match="not the recorded predecessor"):
        upgrade.rollback("unrelated-generation")

    backend.online = True
    with pytest.raises(UpgradeConflict, match="running"):
        upgrade.rollback("previous-generation")


def test_offline_upgrade_cli_rejects_untrusted_local_release_input(
    tmp_path: Path,
) -> None:
    _release(tmp_path)
    state_root = tmp_path / "state"

    with pytest.raises(SystemExit):
        offline.main(
            [
                "--state-path",
                str(state_root),
                "upgrade",
                "--release",
                str(tmp_path / "platform-release.json"),
            ]
        )

    assert not state_root.exists()


def test_offline_exact_upgrade_recover_and_rollback_cli_require_apply(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    target_name = f"platform/releases/1.2.0/{SHA_A}.json"
    calls: list[tuple[str, object]] = []

    class FakeCliUpgrade:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def plan(self, target: str) -> dict[str, object]:
            calls.append(("plan", target))
            return {"plan_digest": f"sha256:{SHA_B}", "target": target}

        def apply(self, plan: object) -> dict[str, object]:
            calls.append(("apply", plan))
            return {"generation_id": "gen-a", "status": "active"}

        def recover(self) -> dict[str, object]:
            calls.append(("recover", True))
            return {"generation_id": "gen-a", "status": "active"}

        def rollback_plan(self, generation: str) -> dict[str, object]:
            calls.append(("rollback-plan", generation))
            return {"generation_id": generation, "plan_digest": f"sha256:{SHA_C}"}

        def rollback(self, plan: object) -> dict[str, object]:
            calls.append(("rollback", plan))
            return {"generation_id": "gen-old", "status": "rolled-back"}

    monkeypatch.setattr(offline, "ControlUpgrade", FakeCliUpgrade)
    monkeypatch.setattr(offline, "HostUpgradeBoundary", lambda **_kwargs: object())
    monkeypatch.setattr(offline, "_load_release_source", lambda _root: object())
    monkeypatch.setattr(offline, "asdict", lambda value: value)
    monkeypatch.setenv("VONK_BACKUP_RECIPIENTS_FILE", str(tmp_path / "recipients"))
    monkeypatch.setenv("VONK_BACKUP_IDENTITY_FILE", str(tmp_path / "identity"))
    site_environment = tmp_path / "site.env"
    site_environment.write_text("VONK_SITE_NAME=test\n", encoding="utf-8")
    site_environment.chmod(0o600)
    monkeypatch.setenv("VONK_CONTROL_SITE_ENV_FILE", str(site_environment))

    assert (
        offline.main(
            [
                "--state-path",
                str(tmp_path / "state"),
                "upgrade",
                "--target-name",
                target_name,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["mode"] == "plan"
    assert calls == [("plan", target_name)]

    assert (
        offline.main(
            [
                "--state-path",
                str(tmp_path / "state"),
                "upgrade",
                "--target-name",
                target_name,
                "--apply",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "active"
    assert calls[-2][0] == "plan"
    assert calls[-1][0] == "apply"

    assert offline.main(["--state-path", str(tmp_path / "state"), "recover"]) == 2
    capsys.readouterr()
    assert not any(name == "recover" for name, _value in calls)
    assert (
        offline.main(["--state-path", str(tmp_path / "state"), "recover", "--apply"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "active"

    assert (
        offline.main(
            [
                "--state-path",
                str(tmp_path / "state"),
                "rollback",
                "--generation",
                "gen-old",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["mode"] == "plan"
    assert calls[-1] == ("rollback-plan", "gen-old")
    assert (
        offline.main(
            [
                "--state-path",
                str(tmp_path / "state"),
                "rollback",
                "--generation",
                "gen-old",
                "--apply",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "rolled-back"
    assert calls[-2] == ("rollback-plan", "gen-old")
    assert calls[-1][0] == "rollback"


def test_offline_apply_requires_backup_identity_and_tuf_authorization(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _release(tmp_path)
    backend = FakeUpgradeBoundary()
    monkeypatch.setattr(offline, "HostUpgradeBoundary", lambda **_kwargs: backend)
    recipients = tmp_path / "recipients"
    identity = tmp_path / "identity"
    recipients.write_text("age1test\n")
    identity.write_text("AGE-SECRET-KEY-test\n")
    monkeypatch.setenv("VONK_BACKUP_RECIPIENTS_FILE", str(recipients))
    monkeypatch.setenv("VONK_BACKUP_IDENTITY_FILE", str(identity))
    site_environment = tmp_path / "site.env"
    site_environment.write_text("VONK_SITE_NAME=test\n", encoding="utf-8")
    site_environment.chmod(0o600)
    monkeypatch.setenv("VONK_CONTROL_SITE_ENV_FILE", str(site_environment))

    result = offline.main(
        [
            "--state-path",
            str(tmp_path / "state"),
            "upgrade",
            "--target-name",
            f"platform/releases/1.2.0/{SHA_A}.json",
            "--apply",
        ]
    )

    assert result == 2
    assert "VONK_PLATFORM_TUF_ROOT" in capsys.readouterr().err
    assert backend.events == []


def test_host_upgrade_boundary_uses_only_fixed_argv_and_exact_image_digests(
    tmp_path: Path, monkeypatch
) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    recipients = tmp_path / "recipients"
    identity = tmp_path / "identity"
    recipients.write_text("age1test\n")
    identity.write_text("AGE-SECRET-KEY-test\n")
    recipients.chmod(0o400)
    identity.chmod(0o400)
    calls: list[tuple[str, ...]] = []

    class Runner:
        def run(self, argv, **_kwargs):
            fixed = tuple(argv)
            calls.append(fixed)
            stdout = b"services: {}\n" if fixed[-1] == "config" else b""
            return CommandResult(0, stdout, b"", 0.01)

    boundary = offline.HostUpgradeBoundary(
        state_root=tmp_path / "state",
        compose_file=compose,
        recipients_file=recipients,
        identity_file=identity,
        health_url="https://control.example.test/api/v1/healthz",
        runner=Runner(),
    )
    api = f"ghcr.io/example/api@sha256:{SHA_A}"
    worker = f"ghcr.io/example/worker@sha256:{SHA_B}"
    environment = {
        "CONTROL_API_IMAGE": api,
        "CONTROL_WORKER_IMAGE": worker,
        "VONK_PLATFORM_BUILD_DIGEST": f"sha256:{SHA_A}",
        "VONK_PLATFORM_RELEASE_DIGEST": f"sha256:{SHA_C}",
        "VONK_PLATFORM_VERSION": "1.2.0",
    }
    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / "platform.env").write_text(
        "".join(f"{key}={environment[key]}\n" for key in sorted(environment)),
        encoding="utf-8",
    )

    assert boundary.control_is_running() is False
    boundary.pull((api, worker))
    assert boundary.render_compose(environment) == b"services: {}\n"
    boundary.stop_worker()
    boundary.migrate("0001_fleet_library_baseline")
    boundary.start_api(generation)
    boundary.start_worker()
    boundary.stop_api()
    boundary.restore_generation(generation)

    assert ("/usr/bin/docker", "pull", api) in calls
    assert ("/usr/bin/docker", "pull", worker) in calls
    assert any(
        argv[-8:]
        == (
            "run",
            "--rm",
            "control-api",
            "python",
            "-m",
            "alembic",
            "upgrade",
            "0001_fleet_library_baseline",
        )
        for argv in calls
    )


def test_control_release_image_contains_offline_migration_assets() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "control/Dockerfile").read_text(encoding="utf-8")

    assert "COPY control/alembic.ini /srv/vonk-control/alembic.ini" in dockerfile
    assert "COPY control/migrations /srv/vonk-control/migrations" in dockerfile


def test_host_migration_command_failure_is_always_ambiguous(
    tmp_path: Path, monkeypatch
) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    recipients = tmp_path / "recipients"
    identity = tmp_path / "identity"
    recipients.write_text("age1test\n")
    identity.write_text("AGE-SECRET-KEY-test\n")
    recipients.chmod(0o400)
    identity.chmod(0o400)

    class FailingRunner:
        def run(self, _argv, **_kwargs):
            raise HostCommandError("nonzero exit")

    boundary = offline.HostUpgradeBoundary(
        state_root=tmp_path / "state",
        compose_file=compose,
        recipients_file=recipients,
        identity_file=identity,
        health_url="https://control.example.test/api/v1/healthz",
        runner=FailingRunner(),
    )

    with pytest.raises(AmbiguousMigrationError, match="outcome is unknown"):
        boundary.migrate("0001_fleet_library_baseline")
