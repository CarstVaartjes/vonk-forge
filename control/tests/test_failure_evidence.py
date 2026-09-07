from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.failure_evidence import FailureDiagnostics
from vonk_control.failure_evidence import (
    EvidenceRetention,
    FailureEvidenceBundle,
    FailureEvidenceService,
    collect_failure,
    log_tail,
)
from vonk_control.failure_evidence_api import install_failure_evidence_routes
from vonk_control.failure_evidence_models import (
    FailureEvidenceCursor,
    FailureEvidenceRecord,
)
from vonk_control.models import Base, Job

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


@pytest.fixture
def service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'evidence.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    return FailureEvidenceService(sessions, clock=lambda: NOW)


def item(kind="recipe.build.v1", attempt=1):
    return {
        "id": str(uuid4()),
        "attempt": attempt,
        "kind": kind,
        "node_ids": ["spk_" + "a" * 32],
        "authority_revision": "a" * 64,
        "plan_digest": "b" * 64,
        "payload_digest": "c" * 64,
        "updated_at": NOW.isoformat(),
        "progress": {"phase": "prepare"},
        "result": {
            "reason": "Podman could not mount proc",
            "diagnostic": "proc-mount-denied",
            "stderr": "Error mounting /proc: permission denied",
        },
    }


@pytest.mark.parametrize(
    "kind",
    [
        "recipe.build.v1",
        "artifact.distribution.v1",
        "recipe.image.import.v1",
        "recipe.install",
        "recipe.start",
        "recipe.job.run.v1",
        "agent.upgrade.v1",
        "model-cache.download",
    ],
)
def test_every_operation_collector_keeps_phase_and_authority(service, kind):
    value = item(kind)
    before = copy.deepcopy(value)
    assert service.capture(value)
    content, digest, bundle = service.read(value["id"], 1)
    assert bundle.diagnostics.phase == "prepare"
    assert bundle.diagnostics.category == "platform-policy"
    assert bundle.context.authority_revision == "a" * 64
    assert "permission denied" in bundle.diagnostics.stderr.text
    assert bundle.context.node_ids == value["node_ids"]
    assert value == before
    assert not service.capture(value)
    assert service.read(value["id"], 1)[0] == content
    assert service.decorate(value)["result"]["evidence_download"]["sha256"] == digest


@pytest.mark.parametrize(
    "code,expected",
    [
        ("permission-denied", "platform-policy"),
        ("insufficient-storage", "capacity"),
        ("network-unreachable", "network"),
        ("digest-mismatch", "digest"),
        ("deadline-exceeded", "timeout"),
        ("engine-exit", "runtime"),
    ],
)
def test_failure_classification_uses_codes(code, expected):
    value = item()
    value["result"]["diagnostic"] = code
    assert collect_failure(value, now=NOW).diagnostics.category == expected


def test_redaction_handles_adversarial_values_before_persistence(service):
    value = item()
    value["result"].update(
        {
            "stderr": "\n".join(
                [
                    "Authorization: Bearer super-sensitive",
                    'api_key="quoted multi word value"',
                    "Cookie: sessionid=hidden",
                    "https://user:password@example.test/blob?X-Amz-Signature=signed-secret",
                    "-----BEGIN PRIVATE KEY-----",
                    "A" * 64,
                    "-----END PRIVATE KEY-----",
                    "permission denied mounting proc",
                ]
            ),
            "nested": {"environment": {"HF_TOKEN": "sensitive"}},
        }
    )
    service.capture(value)
    content = service.read(value["id"], 1)[0].decode()
    for secret in (
        "super-sensitive",
        "quoted multi word",
        "sessionid=hidden",
        "signed-secret",
        "A" * 64,
    ):
        assert secret not in content
    assert "permission denied" in content
    with service.sessions() as session:
        assert session.scalar(select(FailureEvidenceRecord)).content.decode() == content


def test_ring_buffer_preserves_last_lines_and_reports_loss():
    tail = log_tail("noise\n" * 20000 + "last useful error\n")
    assert tail.text.endswith("last useful error")
    assert tail.truncated and tail.dropped_bytes > 0 and tail.dropped_lines > 0
    assert len(tail.text.encode()) <= 2048
    assert len(tail.text.splitlines()) <= 32


def test_collector_failure_preserves_original_result_and_is_separate(
    service, monkeypatch
):
    value = item()
    before = copy.deepcopy(value)

    def fail(*_args, **_kwargs):
        raise RuntimeError("secret diagnostic error")

    monkeypatch.setattr("vonk_control.failure_evidence.collect_failure", fail)
    service.capture(value)
    bundle = service.read(value["id"], 1)[2]
    assert bundle.collector_errors == ["collector-failed"]
    assert bundle.summary == value["result"]["reason"]
    assert value == before
    assert "secret diagnostic error" not in bundle.model_dump_json()


def test_offline_node_uses_durable_evidence_without_probe(service):
    value = item("recipe.start")
    service.capture(value)
    bundle = service.read(value["id"], 1)[2]
    assert bundle.diagnostics.collector_errors == ["agent-observations-unavailable"]
    assert bundle.context.node_ids == value["node_ids"]


def test_typed_agent_diagnostics_retained_and_resanitized(service):
    value = item()
    diagnostics = collect_failure(value, now=NOW).diagnostics.model_dump(mode="json")
    diagnostics["sandbox"] = [{"name": "NoNewPrivileges", "value": "yes"}]
    diagnostics["storage"] = [{"name": "free-bytes", "value": "1024"}]
    diagnostics["versions"] = [{"name": "kernel", "value": "6.12"}]
    diagnostics["stderr"]["text"] = "token=should-never-persist"
    value["result"]["diagnostics"] = FailureDiagnostics.model_validate(
        diagnostics
    ).model_dump(mode="json")
    service.capture(value)
    raw, _, bundle = service.read(value["id"], 1)
    assert b"should-never-persist" not in raw
    assert bundle.diagnostics.sandbox[0].value == "yes"
    assert bundle.diagnostics.storage[0].value == "1024"


def test_retention_caps_count_and_age_and_does_not_recapture(service):
    service.retention = EvidenceRetention(max_entries=2, days=1)
    ids = []
    for index in range(4):
        value = item()
        ids.append(value["id"])
        service.clock = lambda index=index: NOW + timedelta(seconds=index)
        service.capture(value)
    with service.sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(FailureEvidenceRecord)) == 2
        )
    service.clock = lambda: NOW + timedelta(days=2)
    assert service.prune() == 2
    with pytest.raises(KeyError):
        service.read(ids[-1], 1)


def test_worker_cursor_survives_restart_and_retention(service):
    identity = str(uuid4())
    with service.sessions.begin() as session:
        session.add(
            Job(
                id=identity,
                request_id=str(uuid4()),
                kind="recipe.install",
                state="failed",
                actor="test",
                authority_revision="a" * 64,
                targets=[],
                payload_digest="b" * 64,
                payload={},
                result={
                    "reason": "installation failed",
                    "error_code": "permission_denied",
                },
                current_attempt=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    assert service.tick()
    with service.sessions() as session:
        assert session.get(FailureEvidenceCursor, "job").operation_id == identity
    service.retention = EvidenceRetention(days=1)
    service.clock = lambda: NOW + timedelta(days=2)
    service.prune()
    restarted = FailureEvidenceService(
        service.sessions, clock=service.clock, retention=service.retention
    )
    assert not restarted.tick()
    with service.sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(FailureEvidenceRecord)) == 0
        )
    assert restarted.last_collection_error is None


def test_evidence_download_is_authenticated_exact_attempt_and_stable(service):
    value = item()
    service.capture(value)
    app = FastAPI()

    def actor(authorization: str | None = Header(default=None)):
        if authorization != "Bearer test":
            raise HTTPException(401, "authentication required")
        return "viewer"

    install_failure_evidence_routes(
        app, actor_dependency=Depends(actor), service=service
    )
    client = TestClient(app)
    url = f"/api/v1/operations/{value['id']}/evidence?attempt=1"
    assert client.get(url).status_code == 401
    response = client.get(url, headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert (
        FailureEvidenceBundle.model_validate_json(response.content).context.attempt == 1
    )
    assert response.headers["content-disposition"].startswith("attachment;")
    assert (
        response.content
        == client.get(url, headers={"Authorization": "Bearer test"}).content
    )
    assert (
        client.get(
            url.replace("attempt=1", "attempt=2"),
            headers={"Authorization": "Bearer test"},
        ).status_code
        == 404
    )
    with service.sessions.begin() as session:
        session.get(FailureEvidenceRecord, (value["id"], 1)).content = json.dumps(
            {"invalid": True}
        ).encode()
    assert client.get(url, headers={"Authorization": "Bearer test"}).status_code == 503


def test_storage_byte_budget_is_enforced(service):
    service.retention = EvidenceRetention(max_bytes=32 * 1024)
    for index in range(12):
        value = item()
        value["result"]["stderr"] = "useful diagnostic\n" * 1000
        value["result"].update({f"field_{i}": "x " * 256 for i in range(15)})
        service.clock = lambda index=index: NOW + timedelta(seconds=index)
        service.capture(value)
    with service.sessions() as session:
        assert (
            session.scalar(select(func.sum(func.length(FailureEvidenceRecord.content))))
            <= 32 * 1024
        )


def test_collection_time_budget_yields_and_leaves_cursor_for_next_tick(
    service, monkeypatch
):
    with service.sessions.begin() as session:
        for _ in range(5):
            session.add(
                Job(
                    request_id=str(uuid4()),
                    kind="recipe.start",
                    state="failed",
                    actor="test",
                    authority_revision="a" * 64,
                    targets=[],
                    payload_digest="b" * 64,
                    payload={},
                    result={"reason": "start failed"},
                    current_attempt=1,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    observed = iter([0, 0.01, 0.02, 0.03, 0.3])
    monkeypatch.setattr(
        "vonk_control.failure_evidence.time.monotonic", lambda: next(observed, 1.0)
    )
    assert service.tick()
    with service.sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(FailureEvidenceRecord)) == 1
        )
    assert service.last_collection_error is None


def test_migration_creates_evidence_tables_on_fresh_database(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = Path(__file__).parents[1] / "migrations/versions/0024_failure_evidence.py"
    spec = importlib.util.spec_from_file_location("failure_evidence_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.sqlite'}")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        assert set(inspect(connection).get_table_names()) == {
            "failure_evidence_records",
            "failure_evidence_cursors",
        }


def test_secret_control_characters_cannot_evade_redaction(service):
    value = item()
    value["result"]["stderr"] = (
        "Auth\x00orization: Bearer hidden-value\n\x1b[31mpermission denied\n"
    )
    value["result"]["tok\u200ben"] = "hidden-in-obfuscated-key"
    service.capture(value)
    content = service.read(value["id"], 1)[0].decode()
    assert "hidden-value" not in content
    assert "hidden-in-obfuscated-key" not in content
    assert "\\u001b" not in content
    assert "permission denied" in content
