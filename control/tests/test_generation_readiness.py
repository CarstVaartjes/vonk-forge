from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control import dev_cohort, dev_init
from vonk_control.api import (
    DirectoryIdentityProjectionSource,
    GenerationProcessIdentity,
    GenerationReadinessError,
    GenerationReadinessService,
    create_preselection_app,
    install_selected_generation_readiness,
    production_app,
)
from vonk_control.dev_cohort import build_identity, verify_cohort
from vonk_control.models import Base, ControlProcessHeartbeat
from vonk_control.settings import (
    GenerationStartupSettings,
    SettingsError,
    StartupMode,
)
from vonk_control.worker import (
    Worker,
    WorkerHeartbeatRecorder,
    WorkerSelectedIdentityVerifier,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
GEN_A = f"gen-{SHA_A[:24]}"
GEN_B = f"gen-{'d' * 24}"
START_NONCE = "c" * 64
NOW = datetime(2026, 8, 6, 10, tzinfo=UTC)


def _development_cohort_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected_commit: str = "e" * 40,
    embedded_commit: str | None = None,
    embedded_role: str = "api",
):
    selected = verify_cohort(
        [
            build_identity(role="api", source_commit=selected_commit),
            build_identity(role="worker", source_commit=selected_commit),
        ]
    )
    cohort_root = tmp_path / "cohort"
    cohort_root.mkdir()
    selected_path = cohort_root / "selected.json"
    selected_path.write_bytes(selected.to_bytes())
    embedded_path = tmp_path / "development-image-identity.json"
    embedded_path.write_bytes(
        build_identity(
            role=embedded_role,
            source_commit=embedded_commit or selected_commit,
        ).to_bytes()
    )
    monkeypatch.setattr(
        dev_cohort,
        "DEVELOPMENT_IMAGE_IDENTITY_PATH",
        embedded_path,
    )
    identity_root = tmp_path / "identity"
    identity_root.mkdir()
    active = identity_root / "active.json"
    active.write_bytes(
        dev_init._active_projection(
            dev_init._selected_generation_identity(selected)
        )
    )
    active.chmod(0o444)
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "development")
    monkeypatch.setenv("VONK_CONTROL_STARTUP_MODE", "selected")
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.delenv("VONK_DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("VONK_CONTROL_IDENTITY_ROOT", str(identity_root))
    monkeypatch.setenv("VONK_DEV_SELECTED_COHORT_FILE", str(selected_path))
    for name in (
        "VONK_CONTROL_GENERATION_ID",
        "VONK_PLATFORM_RELEASE_DIGEST",
        "VONK_PLATFORM_BUILD_DIGEST",
        "VONK_PLATFORM_VERSION",
        "VONK_CONTROL_PROCESS_IMAGE",
        "VONK_DATABASE_REVISION",
        "VONK_CONTROL_START_NONCE",
    ):
        monkeypatch.delenv(name, raising=False)
    return selected, identity_root, selected_path


def _identity(
    *, mode: StartupMode, generation_id: str = GEN_A, start_nonce: str = START_NONCE
) -> GenerationProcessIdentity:
    return GenerationProcessIdentity(
        startup_mode=mode,
        operation_id="operation-1" if mode is StartupMode.PRESELECTION else None,
        generation_id=generation_id,
        release_digest=f"sha256:{SHA_A}",
        build_digest=f"sha256:{SHA_B}",
        platform_version="1.2.0",
        process_image=f"ghcr.io/example/control-api@sha256:{SHA_A}",
        database_revision="0012_control_process_heartbeats",
        start_nonce=start_nonce,
    )


def _projection(kind: str = "candidate", **changes: object) -> dict[str, object]:
    plan_document: dict[str, object] = {
        "schema_version": 1,
        "operation_id": "operation-1",
        "plan_digest": f"sha256:{'d' * 64}",
        "generation_id": GEN_A,
        "platform_target_name": f"platform/releases/1.2.0/{SHA_A}.json",
        "platform_target_sha256": SHA_A,
        "tuf_targets_version": 7,
        "release_digest": f"sha256:{SHA_A}",
        "build_digest": f"sha256:{SHA_B}",
        "platform_version": "1.2.0",
        "deployment_bundle_digest": f"sha256:{'e' * 64}",
        "api_image": f"ghcr.io/example/control-api@sha256:{SHA_A}",
        "worker_image": f"ghcr.io/example/control-worker@sha256:{SHA_B}",
        "database_revision": "0012_control_process_heartbeats",
    }
    if kind != "active":
        value = {**plan_document, "projection_kind": kind}
        value.update(changes)
        return value

    from vonk_control.host_state import HostOperationPlan, SelectionReceipt

    receipt = SelectionReceipt.from_plan(
        HostOperationPlan.from_document(plan_document),
        previous_generation="gen-previous",
    )
    selection = receipt.document()
    canonical_selection = (
        json.dumps(selection, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    value = {
        "generation_receipt_sha256": selection["generation_receipt_sha256"],
        "projection_kind": "active",
        "projection_sequence": 2,
        "schema_version": 1,
        "selection": selection,
        "selection_receipt_sha256": hashlib.sha256(canonical_selection).hexdigest(),
    }
    for field, replacement in changes.items():
        if field in {"projection_kind", "projection_sequence"}:
            value[field] = replacement
        elif field in {"operation_id", "plan_digest", "previous_generation"}:
            selection[field] = replacement
        else:
            generation = selection["generation"]
            assert isinstance(generation, dict)
            generation[field] = replacement
    return value


def _worker_verifier(projections: Projections) -> WorkerSelectedIdentityVerifier:
    return WorkerSelectedIdentityVerifier(
        projections,
        generation_id=GEN_A,
        release_digest=f"sha256:{SHA_A}",
        build_digest=f"sha256:{SHA_B}",
        platform_version="1.2.0",
        process_image=f"ghcr.io/example/control-worker@sha256:{SHA_B}",
        database_revision="0012_control_process_heartbeats",
    )


class Projections:
    def __init__(
        self,
        *,
        candidate: dict[str, object] | None = None,
        active: dict[str, object] | None = None,
    ) -> None:
        self._candidate = candidate
        self._active = active
        self.calls: list[tuple[str, str | None]] = []

    def load_candidate(self, operation_id: str) -> dict[str, object] | None:
        self.calls.append(("candidate", operation_id))
        return self._candidate

    def load_active(self) -> dict[str, object] | None:
        self.calls.append(("active", None))
        return self._active


def _sessions(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'readiness.sqlite'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _service(
    tmp_path: Path,
    *,
    identity: GenerationProcessIdentity,
    projections: Projections,
    clock=lambda: NOW,
    database_revision=lambda: "0012_control_process_heartbeats",
) -> GenerationReadinessService:
    return GenerationReadinessService(
        _sessions(tmp_path),
        identity,
        projections,
        clock=clock,
        database_revision=database_revision,
        heartbeat_maximum_age_seconds=15,
    )


def test_preselection_app_exposes_only_generation_readiness(tmp_path: Path) -> None:
    projections = Projections(candidate=_projection())
    service = _service(
        tmp_path,
        identity=_identity(mode=StartupMode.PRESELECTION),
        projections=projections,
    )

    app = create_preselection_app(service)
    client = TestClient(app)

    assert {
        route.path for route in app.routes
    } == {"/internal/v1/generation/readiness"}
    response = client.get("/internal/v1/generation/readiness")
    assert response.status_code == 200
    assert response.json() == {
        "build_digest": f"sha256:{SHA_B}",
        "database_revision": "0012_control_process_heartbeats",
        "generation_id": GEN_A,
        "mode": "preselection",
        "operation_id": "operation-1",
        "release_digest": f"sha256:{SHA_A}",
        "start_nonce": START_NONCE,
        "status": "ready",
    }
    for path in (
        "/api/v1/healthz",
        "/api/v1/repository",
        "/agent/v1/operations",
        "/internal/v1/repository/evaluate",
        "/metrics",
        "/openapi.json",
    ):
        assert client.get(path).status_code == 404
    assert projections.calls == [("candidate", "operation-1")]


def test_production_entrypoint_selects_inert_preselection_before_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vonk_control import api as api_module
    from vonk_control import jobs as jobs_module
    from vonk_control import proposals as proposals_module
    from vonk_control import repository as repository_module
    from vonk_control import update_admin as update_admin_module

    constructed: list[str] = []

    def forbidden_constructor(name: str):
        def construct(*_args: object, **_kwargs: object) -> None:
            constructed.append(name)
            raise AssertionError(f"preselection constructed {name}")

        return construct

    monkeypatch.setattr(
        repository_module,
        "RepositoryService",
        forbidden_constructor("repository service"),
    )
    monkeypatch.setattr(
        proposals_module,
        "ProposalService",
        forbidden_constructor("admin proposal service"),
    )
    monkeypatch.setattr(
        jobs_module,
        "JobService",
        forbidden_constructor("admin job service"),
    )
    monkeypatch.setattr(
        api_module,
        "build_agent_services",
        forbidden_constructor("agent service"),
    )
    monkeypatch.setattr(
        update_admin_module,
        "PlatformUpdateAdminService",
        forbidden_constructor("update service"),
    )
    monkeypatch.setattr(
        api_module,
        "create_app",
        forbidden_constructor("admin application"),
    )
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("VONK_AGENT_RUNTIME", "disabled")
    monkeypatch.setenv(
        "VONK_DATABASE_URL", "postgresql+psycopg://control:test@db/control"
    )
    monkeypatch.delenv("VONK_DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("VONK_CONTROL_STARTUP_MODE", "preselection")
    monkeypatch.setenv("VONK_CONTROL_OPERATION_ID", "operation-1")
    monkeypatch.setenv("VONK_CONTROL_GENERATION_ID", GEN_A)
    monkeypatch.setenv("VONK_PLATFORM_RELEASE_DIGEST", f"sha256:{SHA_A}")
    monkeypatch.setenv("VONK_PLATFORM_BUILD_DIGEST", f"sha256:{SHA_B}")
    monkeypatch.setenv("VONK_PLATFORM_VERSION", "1.2.0")
    monkeypatch.setenv(
        "VONK_CONTROL_PROCESS_IMAGE",
        f"ghcr.io/example/control-api@sha256:{SHA_A}",
    )
    monkeypatch.setenv("VONK_DATABASE_REVISION", "0012_control_process_heartbeats")
    monkeypatch.setenv("VONK_CONTROL_START_NONCE", START_NONCE)
    monkeypatch.setenv("VONK_CONTROL_IDENTITY_ROOT", str(tmp_path / "identity"))
    app = production_app()

    assert {route.path for route in app.routes} == {
        "/internal/v1/generation/readiness"
    }
    assert constructed == []


@pytest.mark.parametrize(
    ("projection", "identity", "message"),
    (
        (None, _identity(mode=StartupMode.PRESELECTION), "candidate projection is unavailable"),
        (_projection("active"), _identity(mode=StartupMode.PRESELECTION), "candidate projection kind"),
        (_projection(operation_id="operation-2"), _identity(mode=StartupMode.PRESELECTION), "operation"),
        (_projection(generation_id=GEN_B), _identity(mode=StartupMode.PRESELECTION), "generation"),
        (_projection(release_digest=f"sha256:{'d' * 64}"), _identity(mode=StartupMode.PRESELECTION), "release"),
        (_projection(api_image=f"ghcr.io/example/api@sha256:{'d' * 64}"), _identity(mode=StartupMode.PRESELECTION), "image"),
    ),
)
def test_generation_readiness_rejects_wrong_candidate_projection(
    tmp_path: Path,
    projection: dict[str, object] | None,
    identity: GenerationProcessIdentity,
    message: str,
) -> None:
    service = _service(
        tmp_path,
        identity=identity,
        projections=Projections(candidate=projection),
    )

    with pytest.raises(GenerationReadinessError, match=message):
        service.candidate(identity.generation_id, identity.start_nonce)


def test_generation_readiness_rejects_wrong_call_identity_and_database_revision(
    tmp_path: Path,
) -> None:
    identity = _identity(mode=StartupMode.PRESELECTION)
    service = _service(
        tmp_path,
        identity=identity,
        projections=Projections(candidate=_projection()),
    )

    with pytest.raises(GenerationReadinessError, match="requested generation"):
        service.candidate(GEN_B, identity.start_nonce)
    with pytest.raises(GenerationReadinessError, match="requested start nonce"):
        service.candidate(identity.generation_id, "d" * 64)

    wrong_database = _service(
        tmp_path,
        identity=identity,
        projections=Projections(candidate=_projection()),
        database_revision=lambda: "0011_update_rollouts",
    )
    with pytest.raises(GenerationReadinessError, match="database revision"):
        wrong_database.candidate(identity.generation_id, identity.start_nonce)


def test_selected_generation_readiness_requires_exact_fresh_worker_loop(
    tmp_path: Path,
) -> None:
    identity = _identity(mode=StartupMode.SELECTED)
    projections = Projections(active=_projection("active"))
    service = _service(tmp_path, identity=identity, projections=projections)
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    install_selected_generation_readiness(app, service)
    client = TestClient(app)

    assert client.get("/internal/v1/generation/readiness").status_code == 503

    recorder = WorkerHeartbeatRecorder(
        service.sessions,
        generation_id=identity.generation_id,
        release_digest=identity.release_digest,
        build_digest=identity.build_digest,
        start_nonce=identity.start_nonce,
        clock=lambda: NOW,
    )
    recorder.completed_loop()

    response = client.get("/internal/v1/generation/readiness")
    assert response.status_code == 200
    assert response.json()["worker_loop_sequence"] == 1
    assert response.json()["mode"] == "selected"


def test_worker_reopens_active_projection_and_requires_exact_worker_image() -> None:
    projection = _projection("active")
    projections = Projections(active=projection)
    verifier = _worker_verifier(projections)

    verifier.verify()
    selection = projection["selection"]
    assert isinstance(selection, dict)
    generation = selection["generation"]
    assert isinstance(generation, dict)
    generation["worker_image"] = f"ghcr.io/example/other@sha256:{SHA_A}"

    with pytest.raises(RuntimeError, match="worker image"):
        verifier.verify()
    assert projections.calls == [("active", None), ("active", None)]


def test_worker_does_not_publish_heartbeat_when_active_projection_drifts(
    tmp_path: Path,
) -> None:
    sessions = _sessions(tmp_path)
    projections = Projections(
        active=_projection("active", generation_id=GEN_B)
    )
    recorder = WorkerHeartbeatRecorder(
        sessions,
        generation_id=GEN_A,
        release_digest=f"sha256:{SHA_A}",
        build_digest=f"sha256:{SHA_B}",
        start_nonce=START_NONCE,
        clock=lambda: NOW,
        verify_selected=_worker_verifier(projections).verify,
    )

    with pytest.raises(RuntimeError, match="worker generation"):
        recorder.completed_loop()

    with sessions() as session:
        assert session.scalar(select(ControlProcessHeartbeat)) is None


@pytest.mark.parametrize(
    (
        "generation_id",
        "release_digest",
        "build_digest",
        "start_nonce",
        "completed_at",
    ),
    (
        (GEN_B, f"sha256:{SHA_A}", f"sha256:{SHA_B}", START_NONCE, NOW),
        (GEN_A, f"sha256:{'d' * 64}", f"sha256:{SHA_B}", START_NONCE, NOW),
        (GEN_A, f"sha256:{SHA_A}", f"sha256:{'d' * 64}", START_NONCE, NOW),
        (GEN_A, f"sha256:{SHA_A}", f"sha256:{SHA_B}", "d" * 64, NOW),
        (
            GEN_A,
            f"sha256:{SHA_A}",
            f"sha256:{SHA_B}",
            START_NONCE,
            NOW - timedelta(seconds=16),
        ),
        (
            GEN_A,
            f"sha256:{SHA_A}",
            f"sha256:{SHA_B}",
            START_NONCE,
            NOW + timedelta(seconds=1),
        ),
    ),
)
def test_selected_generation_readiness_rejects_nonmatching_or_stale_heartbeat(
    tmp_path: Path,
    generation_id: str,
    release_digest: str,
    build_digest: str,
    start_nonce: str,
    completed_at: datetime,
) -> None:
    identity = _identity(mode=StartupMode.SELECTED)
    service = _service(
        tmp_path,
        identity=identity,
        projections=Projections(active=_projection("active")),
    )
    with service.sessions.begin() as session:
        session.add(
            ControlProcessHeartbeat(
                process_kind="worker",
                generation_id=generation_id,
                release_digest=release_digest,
                build_digest=build_digest,
                start_nonce=start_nonce,
                loop_sequence=1,
                completed_at=completed_at,
            )
        )

    with pytest.raises(GenerationReadinessError, match="worker heartbeat"):
        service.selected(identity.generation_id, identity.start_nonce)


def test_selected_mode_never_accepts_candidate_projection(tmp_path: Path) -> None:
    identity = _identity(mode=StartupMode.SELECTED)
    service = _service(
        tmp_path,
        identity=identity,
        projections=Projections(active=_projection("candidate")),
    )

    with pytest.raises(GenerationReadinessError, match="active projection kind"):
        service.selected(identity.generation_id, identity.start_nonce)


def test_worker_heartbeat_is_persisted_only_after_scheduler_loop_returns(
    tmp_path: Path,
) -> None:
    sessions = _sessions(tmp_path)
    identity = _identity(mode=StartupMode.SELECTED)
    recorder = WorkerHeartbeatRecorder(
        sessions,
        generation_id=identity.generation_id,
        release_digest=identity.release_digest,
        build_digest=identity.build_digest,
        start_nonce=identity.start_nonce,
        clock=lambda: NOW,
    )

    class Jobs:
        def claim(self, _worker_id: str, _lease_seconds: int):
            return None

    idle = Worker(Jobs(), "worker", {}, loop_heartbeat=recorder.completed_loop)
    assert idle.run_once() is False
    assert idle.run_once() is False
    with sessions() as session:
        heartbeat = session.scalar(select(ControlProcessHeartbeat))
        assert heartbeat is not None
        assert heartbeat.loop_sequence == 2
        assert heartbeat.completed_at == NOW.replace(tzinfo=None)

    failing = Worker(
        Jobs(),
        "worker",
        {},
        housekeeping=lambda: (_ for _ in ()).throw(RuntimeError("loop failed")),
        loop_heartbeat=recorder.completed_loop,
    )
    with pytest.raises(RuntimeError, match="loop failed"):
        failing.run_once()
    with sessions() as session:
        assert session.scalar(select(ControlProcessHeartbeat)).loop_sequence == 2


def test_directory_projection_source_reopens_exact_file_by_directory_fd(
    tmp_path: Path,
) -> None:
    from vonk_control.api import DirectoryIdentityProjectionSource

    root = tmp_path / "identity"
    candidates = root / "candidates"
    candidates.mkdir(parents=True)
    candidate = candidates / "operation-1.json"
    candidate.write_text(
        json.dumps(_projection(), sort_keys=True, separators=(",", ":")) + "\n"
    )
    candidate.chmod(0o444)
    source = DirectoryIdentityProjectionSource(root, expected_owner=os.geteuid())

    assert source.load_candidate("operation-1") == _projection()

    outside = tmp_path / "outside"
    outside.write_bytes(candidate.read_bytes())
    outside.chmod(0o444)
    candidate.unlink()
    candidate.symlink_to(outside)
    with pytest.raises(GenerationReadinessError, match="unsafe"):
        source.load_candidate("operation-1")


def test_generation_readiness_accepts_task3_candidate_projection_contract(
    tmp_path: Path,
) -> None:
    from vonk_control.host_state import HostGenerationStore, HostOperationPlan

    document = _projection()
    document.pop("projection_kind")
    plan = HostOperationPlan.from_document(document)
    projections = HostGenerationStore(
        tmp_path / "control-host",
        tmp_path / "control-identity",
        owner_uid=os.geteuid(),
    )
    projections.project_candidate(plan)
    identity = _identity(mode=StartupMode.PRESELECTION)
    service = _service(
        tmp_path,
        identity=identity,
        projections=projections,
    )

    assert service.candidate(identity.generation_id, identity.start_nonce)[
        "status"
    ] == "ready"


@pytest.mark.parametrize(
    "replacement_digest",
    ("not-a-sha256", "0" * 64),
    ids=("corrupt", "stale"),
)
def test_directory_projection_source_rejects_active_receipt_digest_mismatch(
    tmp_path: Path,
    replacement_digest: str,
) -> None:
    from vonk_control.api import DirectoryIdentityProjectionSource
    from vonk_control.host_state import (
        HostGenerationStore,
        HostOperationPlan,
        SelectionReceipt,
    )

    document = _projection()
    document.pop("projection_kind")
    plan = HostOperationPlan.from_document(document)
    receipt = SelectionReceipt.from_plan(
        plan,
        previous_generation="gen-previous",
    )
    store = HostGenerationStore(
        tmp_path / "control-host",
        tmp_path / "control-identity",
        owner_uid=os.geteuid(),
    )

    def populate(destination: Path) -> None:
        destination.mkdir(mode=0o700)

    staged = store.prepare_staging(plan.generation_id, populate)
    store.commit_generation(staged, receipt.generation)
    store.select(receipt)
    source = DirectoryIdentityProjectionSource(
        store.identity_root,
        expected_owner=os.geteuid(),
    )
    assert source.load_active() is not None

    active_path = store.identity_root / "active.json"
    active = json.loads(active_path.read_bytes())
    active["generation_receipt_sha256"] = replacement_digest
    active_path.chmod(0o644)
    active_path.write_text(
        json.dumps(active, sort_keys=True, separators=(",", ":")) + "\n"
    )
    active_path.chmod(0o444)

    with pytest.raises(GenerationReadinessError, match="receipt digest"):
        source.load_active()


def test_cohort_derived_api_and_worker_identities_match_strict_active_projection_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected, identity_root, _selected_path = _development_cohort_runtime(
        tmp_path,
        monkeypatch,
    )
    monkeypatch.setenv("VONK_CONTROL_PROCESS_ROLE", "api")
    api_settings = GenerationStartupSettings.from_env_and_secrets()
    assert (api_settings.protocol_minimum, api_settings.protocol_maximum) == (1, 3)
    api_identity = GenerationProcessIdentity(
        startup_mode=api_settings.startup_mode,
        operation_id=api_settings.operation_id,
        generation_id=api_settings.generation_id,
        release_digest=api_settings.release_digest,
        build_digest=api_settings.build_digest,
        platform_version=api_settings.platform_version,
        process_image=api_settings.process_image,
        database_revision=api_settings.database_revision,
        start_nonce=api_settings.start_nonce,
    )
    projections = DirectoryIdentityProjectionSource(
        identity_root,
        expected_owner=os.geteuid(),
    )
    sessions = _sessions(tmp_path)
    service = GenerationReadinessService(
        sessions,
        api_identity,
        projections,
        clock=lambda: NOW,
        database_revision=lambda: selected.database_revision,
        heartbeat_maximum_age_seconds=15,
    )
    with sessions.begin() as session:
        session.add(
            ControlProcessHeartbeat(
                process_kind="worker",
                generation_id=selected.generation_id,
                release_digest=selected.release_digest,
                build_digest=selected.build_digest,
                start_nonce=selected.start_nonce,
                loop_sequence=1,
                completed_at=NOW,
            )
        )

    response = service.selected(selected.generation_id, selected.start_nonce)

    assert response["status"] == "ready"
    embedded_path = dev_cohort.DEVELOPMENT_IMAGE_IDENTITY_PATH
    embedded_path.write_bytes(
        build_identity(role="worker", source_commit=selected.source_commit).to_bytes()
    )
    monkeypatch.setenv("VONK_CONTROL_PROCESS_ROLE", "worker")
    worker_settings = GenerationStartupSettings.from_env_and_secrets()
    WorkerSelectedIdentityVerifier(
        projections,
        generation_id=worker_settings.generation_id,
        release_digest=worker_settings.release_digest,
        build_digest=worker_settings.build_digest,
        platform_version=worker_settings.platform_version,
        process_image=worker_settings.process_image,
        database_revision=worker_settings.database_revision,
    ).verify()


def test_tampered_cohort_fails_before_api_application_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _selected, _identity_root, selected_path = _development_cohort_runtime(
        tmp_path,
        monkeypatch,
    )
    document = json.loads(selected_path.read_bytes())
    document["source_commit"] = "f" * 40
    selected_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    )
    monkeypatch.setenv("VONK_CONTROL_PROCESS_ROLE", "api")
    from vonk_control import db as db_module

    constructed: list[str] = []
    monkeypatch.setattr(
        db_module,
        "build_engine",
        lambda *_args, **_kwargs: constructed.append("database engine"),
    )

    with pytest.raises(SettingsError, match="selected cohort"):
        production_app()

    assert constructed == []


def test_stale_cohort_fails_before_worker_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _development_cohort_runtime(
        tmp_path,
        monkeypatch,
        selected_commit="f" * 40,
        embedded_commit="e" * 40,
        embedded_role="worker",
    )
    monkeypatch.setenv("VONK_CONTROL_PROCESS_ROLE", "worker")
    monkeypatch.setenv("VONK_WORKER_API_TOKEN", "worker-token-abcdefghijklmnopqrstuvwxyz012345")
    from vonk_control import db as db_module

    started: list[str] = []
    monkeypatch.setattr(
        db_module,
        "build_engine",
        lambda *_args, **_kwargs: started.append("database engine"),
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SettingsError, match="selected cohort"):
            runpy.run_module("vonk_control.worker", run_name="__main__")

    assert started == []


def test_mutable_compose_supplies_only_cohort_path_and_role_for_dynamic_identity() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(root / "deploy/compose/compose.dev.images.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]
    dynamic_names = {
        "VONK_CONTROL_GENERATION_ID",
        "VONK_PLATFORM_RELEASE_DIGEST",
        "VONK_PLATFORM_BUILD_DIGEST",
        "VONK_PLATFORM_VERSION",
        "VONK_CONTROL_PROCESS_IMAGE",
        "VONK_DATABASE_REVISION",
        "VONK_CONTROL_START_NONCE",
    }
    for service_name, role in (("control-api", "api"), ("control-worker", "worker")):
        environment = services[service_name]["environment"]
        assert environment["VONK_DEV_SELECTED_COHORT_FILE"] == (
            "/cohort/selected.json"
        )
        assert environment["VONK_CONTROL_PROCESS_ROLE"] == role
        assert dynamic_names.isdisjoint(environment)

    initializer = services["dev-bootstrap"]["environment"]
    assert initializer["VONK_DEV_SELECTED_COHORT_FILE"] == "/cohort/selected.json"
    assert {
        "VONK_DEV_EXPECTED_COMMIT",
        "VONK_DEV_API_IMAGE",
        "VONK_DEV_WORKER_IMAGE",
    }.isdisjoint(initializer)
