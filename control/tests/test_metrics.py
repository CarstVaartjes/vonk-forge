from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from vonk_control import api as control_api
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import TokenCodec
from vonk_control.fleet_projection import (
    CapacityReservations,
    FleetNode,
    FleetSnapshot,
    InventoryState,
    NodeConnection,
    TelemetryDetails,
    TelemetryPoint,
    TelemetryState,
)
from vonk_control.metrics import MetricsRegistry

from .telemetry_fixtures import telemetry_metrics

NODE = "spk_00000000000000000000000000000001"
NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)


def _fleet_snapshot(
    *,
    inventory: str | None = "fresh",
    telemetry: str | None = "live",
    online_state: str = "online",
    certificate_state: str = "valid",
) -> FleetSnapshot:
    point = TelemetryPoint(
        id="00000000-0000-4000-8000-000000000001",
        node_id=NODE,
        boot_id="00000000-0000-4000-8000-000000000002",
        sequence=1,
        observed_at=NOW,
        received_at=NOW,
        gap_samples=0,
        details=TelemetryDetails(),
        metrics=telemetry_metrics(),
    )
    return FleetSnapshot(
        event_cursor=1,
        generated_at=NOW,
        authority_revision="a" * 64,
        nodes=[
            FleetNode(
                id=NODE,
                display_name="Alpha",
                hostname="alpha",
                lifecycle="ready",
                labels={},
                connection=NodeConnection(
                    agent_state="active",
                    certificate_state=certificate_state,
                    online_state=online_state,
                    offline_reason=(
                        None
                        if online_state == "online"
                        else "certificate-expired"
                        if certificate_state == "expired"
                        else "stale"
                    ),
                    last_seen_at=NOW,
                    last_seen_age_seconds=0,
                ),
                inventory=None if inventory is None else InventoryState(
                    observed_at=NOW,
                    received_at=NOW,
                    age_seconds=0,
                    freshness=inventory,
                    disk_total_bytes=10_000,
                    disk_free_bytes=7_000,
                    host_memory_total_bytes=20_000,
                    host_memory_free_bytes=15_000,
                    gpu_memory_total_bytes=20_000,
                    gpu_memory_free_bytes=14_000,
                    gpu_count=1,
                    artifact_store_read_only=False,
                    capabilities=["recipe.operations.v1"],
                    nvidia_driver_version="580.65",
                    container_runtime_version="5.4.2",
                ),
                telemetry=None if telemetry is None else TelemetryState(
                    age_seconds=0,
                    freshness=telemetry,
                    sample=point,
                ),
                installed=[],
                loaded=[],
                reservations=CapacityReservations(
                    disk_bytes=0,
                    unified_memory_bytes=0,
                    host_memory_bytes=0,
                    gpu_memory_bytes=0,
                    port_count=0,
                ),
                warnings=[],
            )
        ],
    )


def test_metrics_use_typed_fleet_evidence_without_health_or_probe_fields() -> None:
    metrics = MetricsRegistry()
    metrics.update_fleet(_fleet_snapshot())
    text = metrics.render()
    assert f'vonk_node_connection_state{{node_id="{NODE}",state="online"}} 1' in text
    assert f'vonk_node_certificate_state{{node_id="{NODE}",state="valid"}} 1' in text
    assert f'vonk_node_inventory_freshness{{node_id="{NODE}",state="fresh"}} 1' in text
    assert f'vonk_node_telemetry_freshness{{node_id="{NODE}",state="live"}} 1' in text
    assert f'vonk_node_inventory_host_memory_free_bytes{{node_id="{NODE}"}} 15000' in text
    assert "vonk_node_ready" not in text
    assert "probe" not in text.lower()
    assert "192.168." not in text and "node.local" not in text


def test_metrics_keep_missing_and_stale_evidence_distinct() -> None:
    metrics = MetricsRegistry()
    metrics.update_fleet(_fleet_snapshot(inventory="stale", telemetry="stale"))
    text = metrics.render()
    assert f'vonk_node_inventory_freshness{{node_id="{NODE}",state="stale"}} 1' in text
    assert f'vonk_node_telemetry_freshness{{node_id="{NODE}",state="stale"}} 1' in text
    assert f'vonk_node_inventory_freshness{{node_id="{NODE}",state="missing"}} 0' in text
    assert f'vonk_node_telemetry_freshness{{node_id="{NODE}",state="missing"}} 0' in text

    snapshot = _fleet_snapshot()
    snapshot.nodes[0].inventory = None
    snapshot.nodes[0].telemetry = None
    metrics.update_fleet(snapshot)
    text = metrics.render()
    assert f'vonk_node_inventory_freshness{{node_id="{NODE}",state="missing"}} 1' in text
    assert f'vonk_node_telemetry_freshness{{node_id="{NODE}",state="missing"}} 1' in text
    assert f'vonk_node_inventory_host_memory_free_bytes{{node_id="{NODE}"}}' not in text


def test_metrics_keep_connection_and_certificate_validity_independent() -> None:
    metrics = MetricsRegistry()
    metrics.update_fleet(
        _fleet_snapshot(online_state="offline", certificate_state="expired")
    )
    text = metrics.render()
    assert f'vonk_node_connection_state{{node_id="{NODE}",state="offline"}} 1' in text
    assert f'vonk_node_connection_state{{node_id="{NODE}",state="online"}} 0' in text
    assert f'vonk_node_certificate_state{{node_id="{NODE}",state="expired"}} 1' in text
    assert f'vonk_node_certificate_state{{node_id="{NODE}",state="valid"}} 0' in text


def test_metrics_require_a_typed_fleet_snapshot() -> None:
    with pytest.raises(TypeError, match="typed FleetSnapshot"):
        MetricsRegistry().update_fleet({"nodes": []})


def test_metrics_do_not_contain_request_content_or_credentials() -> None:
    metrics = MetricsRegistry()
    metrics.observe_api("POST", 202, 0.25)
    metrics.set_job_count("fleet.revoke", "running", 1)
    metrics.set_route_state("maintenance")
    metrics.set_backup_age(60)
    text = metrics.render()
    assert "prompt" not in text.lower()
    assert "bearer" not in text.lower()
    assert "authorization" not in text.lower()
    assert 'method="POST",status_class="2xx"' in text
    assert "vonk_control_backup_age_seconds 60" in text


def test_metric_labels_are_allowlisted_and_unknown_values_collapse() -> None:
    metrics = MetricsRegistry()
    metrics.set_job_count("user-supplied-unique-kind", "surprise", 3)
    text = metrics.render()
    assert 'kind="other",state="other"' in text
    assert "user-supplied" not in text and "surprise" not in text


def test_metrics_endpoint_is_separately_authenticated() -> None:
    class Jobs:
        def list(self): return []
        def get(self, _): raise KeyError
        def enqueue(self, *_args, **_kwargs): raise AssertionError

    metrics = MetricsRegistry()
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(),
        metrics=metrics,
        metrics_token="metrics-token-long",
    )
    client = TestClient(app)
    assert client.get("/metrics").status_code == 401
    response = client.get(
        "/metrics", headers={"Authorization": "Bearer metrics-token-long"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/openmetrics-text")


def test_metrics_endpoint_projects_typed_fleet_snapshot() -> None:
    class Jobs:
        def list(self): return []
        def get(self, _): raise KeyError
        def enqueue(self, *_args, **_kwargs): raise AssertionError

    refresh_fleet_metrics = getattr(control_api, "refresh_fleet_metrics", None)
    assert callable(refresh_fleet_metrics)
    metrics = MetricsRegistry()
    fleet_state = _fleet_snapshot()
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(),
        metrics=metrics,
        metrics_token="metrics-token-long",
        metrics_refresh=lambda: refresh_fleet_metrics(metrics, fleet_state),
    )

    response = TestClient(app).get(
        "/metrics",
        headers={"Authorization": "Bearer metrics-token-long"},
    )

    assert response.status_code == 200
    assert f'vonk_node_connection_state{{node_id="{NODE}",state="online"}} 1' in response.text
    assert f'vonk_node_inventory_freshness{{node_id="{NODE}",state="fresh"}} 1' in response.text
    assert f'vonk_node_telemetry_freshness{{node_id="{NODE}",state="live"}} 1' in response.text
