from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.metrics import MetricsRegistry, OperationalMetricsCollector
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentNodeContact,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)

from .node_contacts import contact_row

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)
NODE = "spk_00000000000000000000000000000001"
NEW_NODE = "spk_00000000000000000000000000000002"
OLD_NODE = "spk_00000000000000000000000000000003"
UNKNOWN_NODE = "spk_00000000000000000000000000000004"


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
                    contact=AgentNodeContact(
                        protocol_version=4, last_seen_at=NOW - timedelta(seconds=90)
                    ),
                ),
                AgentNode(
                    node_id=NEW_NODE,
                    state="retired",
                    contact=AgentNodeContact(protocol_version=5),
                ),
                AgentNode(
                    node_id=OLD_NODE,
                    state="active",
                    contact=AgentNodeContact(protocol_version=3),
                ),
                AgentNode(
                    node_id=UNKNOWN_NODE,
                    state="database-value-that-must-not-be-a-label",
                    contact=AgentNodeContact(protocol_version=None),
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
                kind="fleet.revoke",
                state="waiting-for-operator",
                actor="actor-secret-value",
                authority_revision="a" * 64,
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
                    kind="recipe.start",
                    payload_digest="c" * 64,
                    payload={"content": "operation-payload-secret"},
                    authority_revision="a" * 64,
                    state="running",
                    current_attempt=1,
                    created_at=NOW - timedelta(minutes=1),
                    updated_at=NOW - timedelta(seconds=12),
                ),
                AgentOperation(
                    id="failed-operation-id-secret",
                    parent_job_id="job-id-secret-value",
                    node_id=NODE,
                    kind="recipe.install",
                    payload_digest="d" * 64,
                    payload={},
                    authority_revision="a" * 64,
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
                    authority_revision="a" * 64,
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
                progress={
                    "phase": "copying",
                    "completed_bytes": 10,
                    "total_bytes": 100,
                    "total_bytes_known": True,
                    "observed_at": (NOW - timedelta(seconds=200)).isoformat(),
                    "last_progress_at": (NOW - timedelta(seconds=200)).isoformat(),
                },
            )
        )
    return sessions


@pytest.mark.usefixtures("damaged_json_rows")
def test_operational_metrics_project_existing_agent_state_with_bounded_labels(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path)
    metrics = MetricsRegistry()
    collector = OperationalMetricsCollector(metrics, sessions, clock=lambda: NOW)

    collector.refresh()
    rendered = metrics.render()

    assert f'vonk_agent_state{{node_id="{NODE}",state="active"}} 1' in rendered
    assert (
        f'vonk_agent_version_compatibility{{node_id="{NODE}",version_bucket="supported"}} 1'
        in rendered
    )
    assert (
        f'vonk_agent_version_compatibility{{node_id="{NEW_NODE}",version_bucket="new"}} 1'
        in rendered
    )
    assert (
        f'vonk_agent_version_compatibility{{node_id="{OLD_NODE}",version_bucket="old"}} 1'
        in rendered
    )
    assert (
        f'vonk_agent_version_compatibility{{node_id="{UNKNOWN_NODE}",version_bucket="incompatible"}} 1'
        in rendered
    )
    assert f'vonk_agent_certificate_expiry_seconds{{node_id="{NODE}"}} 3600' in rendered
    assert f'vonk_agent_last_seen_age_seconds{{node_id="{NODE}"}} 90' in rendered
    assert (
        'vonk_agent_operations{operation="recipe.install",state="failed"} 1' in rendered
    )
    assert (
        'vonk_agent_operations{operation="recipe.start",state="running"} 1' in rendered
    )
    assert 'vonk_agent_operations{operation="other",state="other"} 1' in rendered
    assert (
        f'vonk_agent_operation_lease_age_seconds{{node_id="{NODE}",operation="recipe.start"}} 12'
        in rendered
    )
    # The running operation declared a byte total and stopped advancing, so the
    # advisory stall verdict is exposed as a metric an alert can use.
    assert 'vonk_stalled_operations{operation="recipe.start"} 1' in rendered

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


@pytest.mark.usefixtures("damaged_json_rows")
def test_operational_metrics_scrape_reads_current_contact_and_lease_owners(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path)
    metrics = MetricsRegistry()
    collector = OperationalMetricsCollector(metrics, sessions, clock=lambda: NOW)
    collector.refresh()

    with sessions.begin() as session:
        node = session.get(AgentNode, NODE)
        assert node is not None
        node.state = "retired"
        contact_row(node).last_seen_at = NOW - timedelta(seconds=5)
        operation = session.get(AgentOperation, "running-operation-id-secret")
        assert operation is not None
        operation.updated_at = NOW - timedelta(seconds=2)
    rendered = metrics.render()

    assert f'vonk_agent_state{{node_id="{NODE}",state="active"}}' not in rendered
    assert f'vonk_agent_state{{node_id="{NODE}",state="retired"}} 1' in rendered
    assert f'vonk_agent_last_seen_age_seconds{{node_id="{NODE}"}} 5' in rendered

    assert (
        f'vonk_agent_operation_lease_age_seconds{{node_id="{NODE}",operation="recipe.start"}} 2'
        in rendered
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_only_failures_within_the_window_count_as_repeated(tmp_path) -> None:
    # 2026-10-17: the failure alerts were on for 17 days because every stored
    # failed row counted forever, sweep recipe outcomes included.
    sessions = _sessions(tmp_path)

    def job(identity: str, kind: str, ended: datetime) -> Job:
        return Job(
            id=identity,
            request_id=f"request-{identity}",
            kind=kind,
            state="failed",
            actor="sweep",
            authority_revision="a" * 64,
            targets=[NODE],
            payload_digest="b" * 64,
            payload={},
            created_at=ended - timedelta(minutes=10),
            updated_at=ended,
        )

    def operation(identity: str, kind: str, ended: datetime) -> AgentOperation:
        return AgentOperation(
            id=identity,
            parent_job_id="job-id-secret-value",
            node_id=NODE,
            kind=kind,
            payload_digest="c" * 64,
            payload={},
            authority_revision="a" * 64,
            state="failed",
            current_attempt=1,
            created_at=ended - timedelta(minutes=10),
            updated_at=ended,
        )

    recent, old = NOW - timedelta(minutes=50), NOW - timedelta(minutes=70)
    with sessions.begin() as session:
        session.add_all(
            [
                job("recent-switch", "recipe.run-switch.v2", recent),
                job("recent-unknown", "unique-kind-secret", recent),
                job("old-switch", "recipe.run-switch.v2", old),
                operation("recent-start", "recipe.start", recent),
                operation("old-start", "recipe.start", old),
            ]
        )
    metrics = MetricsRegistry()

    OperationalMetricsCollector(metrics, sessions, clock=lambda: NOW).refresh()
    rendered = metrics.render()

    assert 'vonk_recent_job_failures{kind="recipe.run-switch.v2"} 1' in rendered
    assert 'vonk_recent_job_failures{kind="other"} 1' in rendered
    # The fixture's recipe.install failure ended 20 seconds ago.
    assert (
        'vonk_recent_agent_operation_failures{operation="recipe.install"} 1' in rendered
    )
    assert (
        'vonk_recent_agent_operation_failures{operation="recipe.start"} 1' in rendered
    )
    # Every bounded kind is emitted, so no series vanishes between scrapes.
    assert 'vonk_recent_job_failures{kind="fleet.revoke"} 0' in rendered
    assert 'vonk_recent_agent_operation_failures{operation="recipe.stop"} 0' in rendered
    assert "unique-kind-secret" not in rendered

    later = NOW + timedelta(minutes=30)
    OperationalMetricsCollector(metrics, sessions, clock=lambda: later).refresh()
    rendered = metrics.render()

    assert 'vonk_recent_job_failures{kind="recipe.run-switch.v2"} 0' in rendered
    assert (
        'vonk_recent_agent_operation_failures{operation="recipe.start"} 0' in rendered
    )
