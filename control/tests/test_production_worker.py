from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control import telemetry_maintenance
from vonk_control.artifact_maintenance import ArtifactMaintenanceCadence
from vonk_control.jobs import JobService
from vonk_control.models import Base
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.route_runtime import AtomicRouteBundlePublisher
from vonk_control.settings import SettingsError, WorkerSettings
from vonk_control.worker import Worker, assemble_production_worker


def _jobs(tmp_path) -> JobService:
    engine = create_engine(f"sqlite:///{tmp_path / 'production-worker.sqlite'}")
    Base.metadata.create_all(engine)
    return JobService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
    )


def test_production_worker_fails_unknown_generic_work(
    tmp_path,
) -> None:
    jobs = _jobs(tmp_path)
    job = jobs.enqueue("probe", "operator", "a" * 40, [], {})

    assert (
        Worker(
            jobs,
            "worker",
            {},
            reconciliations=None,
        ).run_once()
        is True
    )
    persisted = jobs.get(job.id)
    assert persisted.state == "failed"
    assert persisted.status_reason == "unsupported job kind: probe"
    assert persisted.current_attempt == 1


def test_production_worker_does_not_claim_agent_owned_upgrade_parent(
    tmp_path,
) -> None:
    jobs = _jobs(tmp_path)
    job = jobs.enqueue("agent-upgrade", "operator", "a" * 40, [], {})

    assert (
        Worker(
            jobs,
            "worker",
            {},
            reconciliations=None,
        ).run_once()
        is False
    )
    persisted = jobs.get(job.id)
    assert persisted.state == "queued"
    assert persisted.status_reason is None
    assert persisted.current_attempt == 0


@pytest.mark.parametrize("coordinator", ["fleet_profiles", "run_switches", "recoveries"])
def test_recipe_worker_services_routes_while_coordinators_are_active(tmp_path, coordinator) -> None:
    calls: list[str] = []
    engine = create_engine(f"sqlite:///{tmp_path / 'fair-worker.sqlite'}")
    Base.metadata.create_all(engine)

    class Coordinator:
        def tick(self) -> bool:
            calls.append(coordinator)
            return True

    class Routes:
        def maintain(self, **_kwargs) -> bool:
            calls.append("routes")
            return False

    worker = RecipeOperationWorker(
        sessionmaker(engine, expire_on_commit=False),
        Routes(),
        clock=lambda: datetime(2026, 8, 6, tzinfo=UTC),
        **{coordinator: Coordinator()},
    )

    assert worker.tick() is True
    assert calls == [coordinator, "routes"]


def test_production_builder_wires_reconciliation_and_housekeeping(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'builder.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    current = datetime(2026, 8, 6, tzinfo=UTC)
    clock = lambda: current
    jobs = JobService(sessions, clock=clock)
    route_root = tmp_path / "routes"
    publisher = AtomicRouteBundlePublisher(
        route_root,
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
    )

    class SignerBackedAgentJobs:
        def enqueue_in_session(self, *_args, **_kwargs):
            raise AssertionError("validation enqueue is not exercised here")

        def notify_available(self):
            return None

    class Authority:
        def prefetch(self, *_args):
            return None

        def authorization_reason(self, *_args):
            return True

        def clear(self, *_args):
            return None

    agent_jobs = SignerBackedAgentJobs()

    worker = assemble_production_worker(
        jobs=jobs,
        sessions=sessions,
        agent_jobs=agent_jobs,
        publisher=publisher,
        route_root=route_root,
        endpoint_resolver=lambda _session, _node: ("10.0.0.11", clock()),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        authority=Authority(),
        worker_id="control-worker-test",
        artifact_job_root=tmp_path / "artifact-jobs" / "blobs",
        artifact_job_storage_max_bytes=16 * 1024**3,
        artifact_job_retention_seconds=7 * 24 * 60 * 60,
        artifact_job_reconcile_interval_seconds=3600,
        artifact_job_reconcile_batch_limit=1000,
        model_cache=object(),
        agent_artifact_root=tmp_path / "agent-artifacts",
        recipe_image_artifact_root=tmp_path / "agent-artifacts",
    )

    assert not hasattr(worker, "_updates")
    assert not hasattr(worker, "_packages")
    assert not hasattr(worker, "_validation")
    assert worker._reconciliations._agent_jobs is agent_jobs
    assert worker._reconciliations._publisher is publisher
    assert isinstance(
        worker._housekeeping,
        telemetry_maintenance.TelemetryMaintenanceCadence,
    )
    assert isinstance(
        worker._housekeeping._maintenance,
        telemetry_maintenance.TelemetryMaintenance,
    )
    assert isinstance(worker._artifact_housekeeping, ArtifactMaintenanceCadence)
    assert worker._artifact_housekeeping._batch_limit == 1000
    worker._artifact_housekeeping()
    current += timedelta(seconds=3600)
    worker._artifact_housekeeping()
    maintenance_state = json.loads(
        (tmp_path / "artifact-jobs" / "blobs" / ".maintenance.json").read_text()
    )
    assert maintenance_state["last_success_at"] == current.isoformat()
    assert worker._recipes._fleet_profiles is not None
    assert worker._recipes._fleet_profiles._recipe_operations is not None
    assert worker._recipes._run_switches._artifact_phase_executor is not None
    assert len(worker._background_services) == 1
    image_production = worker._background_closers[0].__self__
    assert image_production.scheduler is not None
    scheduler = image_production.scheduler
    worker.close()
    assert scheduler.executor._shutdown is True


def test_production_worker_settings_load_only_worker_authority_secrets(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "database-url"
    token = tmp_path / "worker-api-token"
    database.write_text("postgresql://control:test@postgres/control")
    token.write_text("w" * 32)
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(database))
    monkeypatch.setenv("VONK_WORKER_API_TOKEN_FILE", str(token))
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("VONK_INTERNAL_API_URL", "http://control-api:8000")
    monkeypatch.setenv("VONK_STATE_PATH", str(tmp_path / "state"))

    settings = WorkerSettings.from_env_and_secrets()

    assert settings.database_url == database.read_text()
    assert settings.internal_api_token == b"w" * 32
    assert settings.internal_api_url == "http://control-api:8000"
    assert settings.state_path == tmp_path / "state"
    assert settings.agent_artifact_root == Path("/state/agent-artifacts")
    assert settings.artifact_job_storage_max_bytes == 16 * 1024**3
    assert settings.artifact_job_retention_seconds == 7 * 24 * 60 * 60
    assert settings.artifact_job_reconcile_interval_seconds == 3600
    assert settings.artifact_job_reconcile_batch_limit == 1000
    for forbidden in (
        "repository_path",
        "git_signing_key_path",
        "token_signing_key",
        "metrics_token",
        "agent_ca_credential_path",
    ):
        assert not hasattr(settings, forbidden)


def test_production_worker_settings_reject_raw_or_cross_origin_authority(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "database-url"
    database.write_text("postgresql://control:test@postgres/control")
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(database))
    monkeypatch.setenv("VONK_WORKER_API_TOKEN", "w" * 32)
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("VONK_INTERNAL_API_URL", "http://127.0.0.1:8000/path")

    with pytest.raises(SettingsError):
        WorkerSettings.from_env_and_secrets()

    token = tmp_path / "worker-api-token"
    token.write_text("w" * 32)
    monkeypatch.delenv("VONK_WORKER_API_TOKEN")
    monkeypatch.setenv("VONK_WORKER_API_TOKEN_FILE", str(token))
    with pytest.raises(SettingsError, match="fixed HTTP origin"):
        WorkerSettings.from_env_and_secrets()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "VONK_ARTIFACT_JOB_RECONCILE_INTERVAL_SECONDS",
            "59",
            "reconciliation interval",
        ),
        (
            "VONK_ARTIFACT_JOB_RECONCILE_BATCH_LIMIT",
            "10001",
            "batch limit",
        ),
    ],
)
def test_production_worker_settings_bound_artifact_maintenance(
    tmp_path,
    monkeypatch,
    name,
    value,
    message,
) -> None:
    database = tmp_path / "database-url"
    token = tmp_path / "worker-api-token"
    database.write_text("postgresql://control:test@postgres/control")
    token.write_text("w" * 32)
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(database))
    monkeypatch.setenv("VONK_WORKER_API_TOKEN_FILE", str(token))
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv(name, value)

    with pytest.raises(SettingsError, match=message):
        WorkerSettings.from_env_and_secrets()
