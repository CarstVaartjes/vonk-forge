from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control import telemetry_maintenance
from vonk_control.jobs import JobService
from vonk_control.models import Base
from vonk_control.presence import ManagementAddressPolicy
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

    assert Worker(
        jobs,
        "worker",
        {},
        reconciliations=None,
    ).run_once() is True
    persisted = jobs.get(job.id)
    assert persisted.state == "failed"
    assert persisted.status_reason == "unsupported job kind: probe"
    assert persisted.current_attempt == 1


def test_production_builder_wires_reconciliation_and_housekeeping(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'builder.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 8, 6, tzinfo=UTC)
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

    settings = WorkerSettings.from_env_and_secrets()

    assert settings.database_url == database.read_text()
    assert settings.internal_api_token == b"w" * 32
    assert settings.internal_api_url == "http://control-api:8000"
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
