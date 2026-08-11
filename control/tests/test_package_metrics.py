from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.dashboard import DashboardService
from vonk_control.metrics import MetricsRegistry, OperationalMetricsCollector
from vonk_control.models import (
    AgentNode,
    Base,
    PackageCandidate,
    PackageResolution,
    PackageRollout,
    PackageRolloutNode,
    PackageValidationRun,
)

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)
NODE = "spk_" + "1" * 32


def test_package_metrics_aggregate_operational_state_without_unbounded_labels(tmp_path) -> None:
    # Break caught: package metrics expose release, family, node, URL, or
    # credential-bearing values as labels instead of bounded aggregate state.
    engine = create_engine(f"sqlite:///{tmp_path / 'package-metrics.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    digest = "a" * 64
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE, state="active", capabilities=[]))
        candidate = PackageCandidate(
            id="10000000-0000-4000-8000-000000000001",
            family_id="customer-model-secret",
            upstream_identity_digest=digest,
            metadata_digest="b" * 64,
            upstream_version="2026.08-secret",
            channel="candidate-secret",
            source_provider="private-provider-secret",
            source_reference="https://credential-secret@example.invalid/model",
            state="unsupported",
            reason_code="incomplete_checksum_metadata",
            reason_detail={"token": "credential-secret"},
            summary={"model": "customer-model-secret"},
            discovered_by="discovery-secret",
            first_seen_at=NOW,
            last_seen_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(candidate)
        session.flush()
        resolution = PackageResolution(
            id="10000000-0000-4000-8000-000000000002",
            candidate_id=candidate.id,
            resolver_id="resolver-secret",
            resolver_schema_version=1,
            state="resolved",
            release_digest="c" * 64,
            resolved_by="resolver-secret",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(resolution)
        session.flush()
        session.add(PackageValidationRun(
            id="10000000-0000-4000-8000-000000000003",
            candidate_id=candidate.id,
            resolution_id=resolution.id,
            validation_kind="artifact",
            release_digest="c" * 64,
            policy_digest="d" * 64,
            fleet_digest="e" * 64,
            state="failed",
            actor="validator-secret",
            reason_code="trust_or_provenance_failure",
            failure_detail={"url": "https://credential-secret@example.invalid"},
            created_at=NOW,
            updated_at=NOW,
        ))
        rollout = PackageRollout(
            id="10000000-0000-4000-8000-000000000004",
            deployment_id="deployment-secret",
            deployment_digest="f" * 64,
            release_digest="c" * 64,
            base_commit="1" * 40,
            policy_digest="d" * 64,
            tuf_target_digest="2" * 64,
            fleet_digest="e" * 64,
            topology_digest="3" * 64,
            plan_digest="4" * 64,
            state="rolling-back",
            actor="operator-secret",
            progress={"phase": "canary", "reason_code": "canary-failed"},
            failure_reason="customer-model-secret failed on private host",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(rollout)
        session.flush()
        session.add(PackageRolloutNode(
            id="10000000-0000-4000-8000-000000000005",
            rollout_id=rollout.id,
            node_id=NODE,
            batch_index=-1,
            node_order=0,
            is_canary=False,
            state="offline-pending",
            expected_payload_digest="5" * 64,
            operation_history=[],
            created_at=NOW,
            updated_at=NOW,
        ))

    metrics = MetricsRegistry()
    OperationalMetricsCollector(metrics, sessions, clock=lambda: NOW).refresh()
    rendered = metrics.render()

    assert 'vonk_package_candidates{provider="other",state="unsupported"} 1' in rendered
    assert 'vonk_package_validations{backend="artifact",reason="trust-failure",state="failed"} 1' in rendered
    assert 'vonk_package_rollouts{phase="canary",reason="canary-failure",state="rolling-back"} 1' in rendered
    assert 'vonk_package_rollout_nodes{phase="offline-pending",state="offline-pending"} 1' in rendered
    package_metrics = "\n".join(
        line for line in rendered.splitlines() if line.startswith("vonk_package_")
    )
    for secret in (
        "customer-model-secret",
        "credential-secret",
        "private-provider-secret",
        "deployment-secret",
        NODE,
        "resolver-secret",
        "validator-secret",
        "operator-secret",
    ):
        assert secret not in package_metrics

    # Break caught: fleet package summaries expose package identifiers or
    # discard the bounded operator alerts used by dashboard consumers.
    summary = DashboardService(object(), sessions).package_summary()
    assert summary["candidates"] == {"unsupported": 1}
    assert summary["validations"] == {"failed": 1}
    assert summary["rollouts"] == {"rolling-back": 1}
    assert summary["alerts"] == {"canary-failure": 1, "trust-failure": 1}
    assert "customer-model-secret" not in repr(summary)
    assert NODE not in repr(summary)
