import pytest
from fastapi.testclient import TestClient
from vonk_control import api as control_api
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import TokenCodec
from vonk_control.metrics import MetricsRegistry


def test_metrics_use_node_id_not_hostname_or_address() -> None:
    metrics = MetricsRegistry()
    metrics.update_node("spk_00000000000000000000000000000001", ready=True, memory_available_bytes=1200, disk_available_bytes=3400, probe_age_seconds=5)
    text = metrics.render()
    assert 'node_id="spk_00000000000000000000000000000001"' in text
    assert "192.168." not in text and "node.local" not in text


def test_metrics_do_not_contain_request_content_or_credentials() -> None:
    metrics = MetricsRegistry()
    metrics.observe_api("POST", 202, 0.25)
    metrics.set_job_count("reconcile", "running", 1)
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


def test_invalid_node_id_is_rejected() -> None:
    try:
        MetricsRegistry().update_node("node.local", ready=True, memory_available_bytes=1, disk_available_bytes=1, probe_age_seconds=1)
    except ValueError as error:
        assert "node ID" in str(error)
    else:
        raise AssertionError("unsafe node label was accepted")


def test_nonboolean_node_readiness_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="boolean"):
        MetricsRegistry().update_node(
            "spk_00000000000000000000000000000001",
            ready=1,
            memory_available_bytes=1,
            disk_available_bytes=1,
            probe_age_seconds=1,
        )


def test_metrics_endpoint_is_separately_authenticated() -> None:
    class Jobs:
        def list(self): return []
        def get(self, _): raise KeyError
        def enqueue(self, *_args, **_kwargs): raise AssertionError
    metrics = MetricsRegistry()
    app = create_app(
        jobs=Jobs(), tokens=TokenCodec(b"k" * 32), audits=MemoryAuditStore(),
        fleet=lambda: {"nodes": []}, metrics=metrics, metrics_token="metrics-token-long",
    )
    client = TestClient(app)
    assert client.get("/metrics").status_code == 401
    response = client.get("/metrics", headers={"Authorization": "Bearer metrics-token-long"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/openmetrics-text")


def test_metrics_endpoint_omits_probe_age_for_unobserved_node() -> None:
    class Jobs:
        def list(self): return []
        def get(self, _): raise KeyError
        def enqueue(self, *_args, **_kwargs): raise AssertionError

    refresh_fleet_metrics = getattr(control_api, "refresh_fleet_metrics", None)
    assert callable(refresh_fleet_metrics)
    node_id = "spk_00000000000000000000000000000001"
    metrics = MetricsRegistry()
    fleet_state = {
        "nodes": [
            {
                "id": node_id,
                "healthy": None,
                "memory_available_bytes": 1200,
                "disk_available_bytes": 3400,
                "probe_age_seconds": None,
            }
        ]
    }
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(),
        fleet=lambda: fleet_state,
        metrics=metrics,
        metrics_token="metrics-token-long",
        metrics_refresh=lambda: refresh_fleet_metrics(metrics, fleet_state),
    )

    response = TestClient(app).get(
        "/metrics",
        headers={"Authorization": "Bearer metrics-token-long"},
    )

    assert response.status_code == 200
    assert f'vonk_node_ready{{node_id="{node_id}"}} 0' in response.text
    assert f'vonk_node_memory_available_bytes{{node_id="{node_id}"}} 1200' in response.text
    assert f'vonk_node_disk_available_bytes{{node_id="{node_id}"}} 3400' in response.text
    assert f'vonk_node_probe_age_seconds{{node_id="{node_id}"}}' not in response.text
