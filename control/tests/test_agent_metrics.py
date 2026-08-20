import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.dashboard import DashboardService
from vonk_control.metrics import MetricsRegistry, OperationalMetricsCollector
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)
NODE = "spk_00000000000000000000000000000001"
NEW_NODE = "spk_00000000000000000000000000000002"
OLD_NODE = "spk_00000000000000000000000000000003"
UNKNOWN_NODE = "spk_00000000000000000000000000000004"


class Repository:
    def head(self) -> str:
        return "a"  * 64

    def read_document(self, _commit: str, _path: str):
        return type(
            "Document",
            (),
            {
                "parsed": {
                    "schema_version": 2,
                    "nodes": {
                        NODE: {
                            "display_name": "Alpha",
                            "hostname": "alpha.internal.example",
                            "lifecycle": "ready",
                        },
                        NEW_NODE: {
                            "display_name": "Beta",
                            "hostname": "beta.internal.example",
                            "lifecycle": "ready",
                        },
                    },
                }
            },
        )()


def _sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'agent-metrics.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=NODE,
                    state="active",
                    protocol_version=3,
                    capabilities=[],
                    last_seen_at=NOW - timedelta(seconds=90),
                ),
                AgentNode(
                    node_id=NEW_NODE,
                    state="retired",
                    protocol_version=4,
                    capabilities=[],
                ),
                AgentNode(
                    node_id=OLD_NODE,
                    state="active",
                    protocol_version=2,
                    capabilities=[],
                ),
                AgentNode(
                    node_id=UNKNOWN_NODE,
                    state="database-value-that-must-not-be-a-label",
                    protocol_version=None,
                    capabilities=[],
                ),
            ]
        )
        session.add(
            AgentCertificate(
                serial="certificate-secret-value",
                node_id=NODE,
                not_before=NOW - timedelta(hours=1),
                not_after=NOW + timedelta(hours=1),
                fingerprint="fingerprint-secret-value",
                state="active",
                generation=1,
            )
        )
        session.add(
            Job(
                id="job-id-secret-value",
                request_id="request-id-secret-value",
                kind="reconcile",
                state="waiting-for-operator",
                actor="actor-secret-value",
                authority_revision="a"  * 64,
                targets=[NODE],
                payload_digest="b" * 64,
                payload={"content": "payload-secret-value"},
                status_reason="error-secret-value",
                current_attempt=1,
                created_at=NOW - timedelta(minutes=2),
                updated_at=NOW - timedelta(seconds=30),
            )
        )
        session.add_all(
            [
                AgentOperation(
                    id="running-operation-id-secret",
                    parent_job_id="job-id-secret-value",
                    node_id=NODE,
                    kind="workload.start",
                    payload_digest="c" * 64,
                    payload={"content": "operation-payload-secret"},
                    authority_revision="a"  * 64,
                    state="running",
                    current_attempt=1,
                    created_at=NOW - timedelta(minutes=1),
                    updated_at=NOW - timedelta(seconds=12),
                ),
                AgentOperation(
                    id="failed-operation-id-secret",
                    parent_job_id="job-id-secret-value",
                    node_id=NODE,
                    kind="release.install",
                    payload_digest="d" * 64,
                    payload={},
                    authority_revision="a"  * 64,
                    state="failed",
                    current_attempt=0,
                    created_at=NOW - timedelta(minutes=1),
                    updated_at=NOW - timedelta(seconds=20),
                ),
                AgentOperation(
                    id="unknown-operation-id-secret",
                    parent_job_id="job-id-secret-value",
                    node_id=NODE,
                    kind="unique-operation-secret",
                    payload_digest="e" * 64,
                    payload={},
                    authority_revision="a"  * 64,
                    state="unique-state-secret",
                    current_attempt=0,
                    created_at=NOW - timedelta(minutes=1),
                    updated_at=NOW - timedelta(seconds=20),
                ),
            ]
        )
        session.add(
            AgentOperationAttempt(
                operation_id="running-operation-id-secret",
                attempt=1,
                fence="fence-secret-value",
                lease_deadline=NOW + timedelta(seconds=18),
                agent_certificate_serial="certificate-secret-value",
                state="running",
            )
        )
    return sessions


def test_operational_metrics_project_existing_agent_state_with_bounded_labels(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    metrics = MetricsRegistry()
    collector = OperationalMetricsCollector(metrics, sessions, clock=lambda: NOW)

    collector.refresh()
    rendered = metrics.render()

    assert f'vonk_agent_state{{node_id="{NODE}",state="active"}} 1' in rendered
    assert f'vonk_agent_version_compatibility{{node_id="{NODE}",version_bucket="supported"}} 1' in rendered
    assert f'vonk_agent_version_compatibility{{node_id="{NEW_NODE}",version_bucket="new"}} 1' in rendered
    assert f'vonk_agent_version_compatibility{{node_id="{OLD_NODE}",version_bucket="old"}} 1' in rendered
    assert f'vonk_agent_version_compatibility{{node_id="{UNKNOWN_NODE}",version_bucket="incompatible"}} 1' in rendered
    assert f'vonk_agent_certificate_expiry_seconds{{node_id="{NODE}"}} 3600' in rendered
    assert f'vonk_agent_last_seen_age_seconds{{node_id="{NODE}"}} 90' in rendered
    assert 'vonk_agent_operations{operation="release.install",state="failed"} 1' in rendered
    assert 'vonk_agent_operations{operation="workload.start",state="running"} 1' in rendered
    assert 'vonk_agent_operations{operation="other",state="other"} 1' in rendered
    assert f'vonk_agent_operation_lease_age_seconds{{node_id="{NODE}",operation="workload.start"}} 12' in rendered
    assert 'vonk_agent_rollouts{state="waiting-for-operator"} 1' in rendered

    allowed = {"node_id", "operation", "state", "version_bucket"}
    for line in rendered.splitlines():
        if not line.startswith("vonk_agent_") or "{" not in line:
            continue
        assert set(re.findall(r'([a-z_]+)="', line)) <= allowed
    for secret in (
        "job-id-secret-value",
        "certificate-secret-value",
        "fingerprint-secret-value",
        "alpha.internal.example",
        "error-secret-value",
        "actor-secret-value",
        "payload-secret-value",
        "unique-operation-secret",
        "unique-state-secret",
        "database-value-that-must-not-be-a-label",
    ):
        assert secret not in rendered


def test_operational_metrics_refresh_replaces_stale_node_series(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    metrics = MetricsRegistry()
    collector = OperationalMetricsCollector(metrics, sessions, clock=lambda: NOW)
    collector.refresh()

    with sessions.begin() as session:
        node = session.get(AgentNode, NODE)
        assert node is not None
        node.state = "retired"
        node.last_seen_at = NOW - timedelta(seconds=5)
    collector.refresh()
    rendered = metrics.render()

    assert f'vonk_agent_state{{node_id="{NODE}",state="active"}}' not in rendered
    assert f'vonk_agent_state{{node_id="{NODE}",state="retired"}} 1' in rendered
    assert f'vonk_agent_last_seen_age_seconds{{node_id="{NODE}"}} 5' in rendered


def test_dashboard_projects_agent_recency_certificate_expiry_and_compatibility(tmp_path) -> None:
    sessions = _sessions(tmp_path)

    fleet = DashboardService(Repository(), sessions, clock=lambda: NOW).fleet()
    nodes = {node["id"]: node for node in fleet["nodes"]}

    assert {
        key: nodes[NODE][key]
        for key in (
            "agent_state",
            "last_seen_at",
            "last_seen_age_seconds",
            "certificate_expires_at",
            "certificate_expiry_seconds",
            "compatibility",
        )
    } == {
        "agent_state": "active",
        "last_seen_at": "2026-08-05T11:58:30+00:00",
        "last_seen_age_seconds": 90.0,
        "certificate_expires_at": "2026-08-05T13:00:00+00:00",
        "certificate_expiry_seconds": 3600.0,
        "compatibility": "supported",
    }
    assert nodes[NEW_NODE]["agent_state"] == "retired"
    assert nodes[NEW_NODE]["last_seen_at"] is None
    assert nodes[NEW_NODE]["last_seen_age_seconds"] is None
    assert nodes[NEW_NODE]["certificate_expires_at"] is None
    assert nodes[NEW_NODE]["certificate_expiry_seconds"] is None
    assert nodes[NEW_NODE]["compatibility"] == "new"
    assert "certificate-secret-value" not in repr(fleet)
    assert "fingerprint-secret-value" not in repr(fleet)
