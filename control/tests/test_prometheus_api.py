"""Prometheus producer-to-operator seams; no live monitoring dependency."""

import base64
import json
from threading import Event, Thread
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette import status
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.metrics import MetricsRegistry
from vonk_control.metrics_contract import MetricsSeriesResponse
from vonk_control.prometheus_api import PrometheusReader

from .test_api import Jobs
from .test_metrics import NODE, _fleet_snapshot

_HTTP_CLIENT = httpx.Client


@pytest.fixture
def reader():
    return PrometheusReader()


@pytest.fixture
def client(reader, monkeypatch):
    monkeypatch.setattr(
        "vonk_control.operator_projection_api.routes.PrometheusReader", lambda: reader
    )
    codec = TokenCodec(b"k" * 32)

    class Projection:
        def read(self):
            return _fleet_snapshot()

    app = create_app(
        jobs=Jobs(), tokens=codec, fleet_projection=Projection(), now=lambda: 10
    )
    token = codec.issue(Actor("reader", "viewer"), ttl_seconds=100, now=10)
    return TestClient(app, headers={"Authorization": "Bearer " + token})


def mock_prometheus(monkeypatch, handler):
    monkeypatch.setattr(
        "vonk_control.prometheus_api.httpx.Client",
        lambda **kwargs: _HTTP_CLIENT(**kwargs, transport=httpx.MockTransport(handler)),
    )


def reply(**data):
    return httpx.Response(200, json={"status": "success", "data": data})


@pytest.mark.parametrize(
    "metric,query",
    [
        ("gpu_utilization", "vonk_node_telemetry_gpu_utilization_percent"),
        ("gpu_memory_used", "vonk_node_telemetry_gpu_memory_used_bytes"),
        ("host_memory_used", "vonk_node_telemetry_host_memory_used_bytes"),
        ("gpu_temperature", "vonk_node_telemetry_gpu_temperature_c"),
        (
            "request_rate",
            "sum by (requested_model) (rate(litellm_requests_metric_total[5m]))",
        ),
        ("certificate_expiry", "vonk_agent_certificate_expiry_seconds"),
    ],
)
@pytest.mark.parametrize(
    "range,duration", [("1h", 3600), ("6h", 21600), ("24h", 86400), ("7d", 604800)]
)
def test_fixed_queries_and_ranges(client, monkeypatch, metric, query, range, duration):
    """Catches arbitrary query forwarding, wrong mappings and unbounded resolution."""
    node = None if metric == "request_rate" else NODE

    def handle(request):
        params = request.url.params
        assert request.url.path == "/api/v1/query_range"
        assert params["query"] == query + (f'{{node_id="{NODE}"}}' if node else "")
        assert float(params["end"]) - float(params["start"]) == duration
        assert int(params["step"]) == max(15, duration // 480)
        row = {"metric": {"node_id": NODE}, "values": [[1, "2.5"], [2, "NaN"]]}
        return reply(resultType="matrix", result=[row])

    mock_prometheus(monkeypatch, handle)
    params = dict(metric=metric, range=range, **({"node": node} if node else {}))
    response = client.get("/api/metrics/series", params=params)
    assert response.status_code == 200, response.text
    points = (
        MetricsSeriesResponse.model_validate_json(response.content).series[0].points
    )
    assert [point.value for point in points] == [2.5, None]


@pytest.mark.parametrize(
    "params",
    [
        {"metric": "up", "range": "1h"},
        {"metric": "gpu_utilization", "range": "2h"},
        {"metric": "gpu_utilization", "range": "1h", "node": 'spk_"'},
        {"metric": "request_rate", "range": "1h", "node": NODE},
    ],
)
def test_closed_input(client, monkeypatch, params):
    """Catches PromQL injection and unsupported Spark scope reaching the peer."""
    mock_prometheus(
        monkeypatch, lambda request: pytest.fail("invalid input reached Prometheus")
    )
    assert (
        client.get("/api/metrics/series", params=params).status_code
        == status.HTTP_422_UNPROCESSABLE_CONTENT
    )


def test_series_requires_authentication(client):
    """Catches unauthenticated reads of monitoring data."""
    client.headers.pop("Authorization")
    assert (
        client.get("/api/metrics/series?metric=gpu_utilization&range=1h").status_code
        == 401
    )


@pytest.mark.parametrize("failure", ["connection", "status", "malformed", "oversized"])
def test_prometheus_failure_is_retryable_and_recovers(client, monkeypatch, failure):
    """Catches 500s, unbounded buffering and permanently poisoned observations."""

    def handle(request):
        if failure == "connection":
            raise httpx.ConnectError("down", request=request)
        if failure == "oversized":
            return httpx.Response(200, content=b" " * (4 * 1024 * 1024 + 1))
        return httpx.Response(503) if failure == "status" else reply()

    mock_prometheus(monkeypatch, handle)
    response = client.get("/api/metrics/series?metric=gpu_utilization&range=1h")
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "5"
    mock_prometheus(monkeypatch, lambda request: reply(resultType="matrix", result=[]))
    assert (
        client.get("/api/metrics/series?metric=gpu_utilization&range=1h").status_code
        == 200
    )


def fleet_document(client):
    response = client.get("/api/fleet")
    assert response.status_code == 200
    records = [json.loads(line) for line in response.iter_lines()]
    return json.loads(
        b"".join(base64.b64decode(record["data"]) for record in records[1:-1])
    )


def test_firing_alerts_reach_fleet_attention_and_outage_is_visible(
    client, reader, monkeypatch
):
    """Catches lost firing alerts, pending alerts shown as firing, and false all-clear."""
    alerts = [
        {
            "state": state,
            "labels": {"alertname": "NodeHot", "node_id": NODE},
            "annotations": {"summary": "GPU is hot"},
            "activeAt": "2026-10-10T00:00:00Z",
        }
        for state in ("firing", "pending")
    ]
    mock_prometheus(monkeypatch, lambda request: reply(alerts=alerts))
    reader._refresh_lock.acquire(blocking=False)
    reader._refresh_attention()
    document = fleet_document(client)
    assert [item["summary"] for item in document["attention"]] == ["GPU is hot"]
    assert document["attention"][0]["source"] == "prometheus"
    assert document["attention_unavailable"] is False
    mock_prometheus(monkeypatch, lambda request: httpx.Response(503))
    reader._refresh_lock.acquire(blocking=False)
    reader._refresh_attention()
    document = fleet_document(client)
    assert document["attention_unavailable"] is True
    assert document["nodes"][0]["id"] == NODE


def test_fleet_never_waits_for_alert_refresh_and_cache_recovers(
    client, reader, monkeypatch
):
    """Catches synchronous polls, duplicate refreshes, stale all-clear and poisoned outages."""
    started, release = Event(), Event()
    clock, workers = [100.0], []
    monkeypatch.setattr(
        "vonk_control.prometheus_api.time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def spawn(**kwargs):
        worker = Thread(**kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr("vonk_control.prometheus_api.Thread", spawn)

    def blocked(request):
        started.set()
        assert release.wait(timeout=2)
        return httpx.Response(503)

    mock_prometheus(monkeypatch, blocked)
    try:
        assert fleet_document(client)["attention_unavailable"] is True
        assert started.wait(timeout=1)
        assert fleet_document(client)["attention_unavailable"] is True
        (first_worker,) = workers
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=1)
    assert not first_worker.is_alive()
    assert reader.cached_attention() == ([], True)
    mock_prometheus(monkeypatch, lambda request: reply(alerts=[]))
    clock[0] += 16
    assert reader.cached_attention() == ([], True)
    workers[-1].join(timeout=1)
    assert reader.cached_attention() == ([], False)
    clock[0] += 16
    assert reader.cached_attention() == ([], True)
    workers[-1].join(timeout=1)


def test_exporter_publishes_memory_and_temperature_without_inventing_missing_values():
    """Catches queries pointing at absent gauges or unknown readings becoming zero."""
    snapshot = _fleet_snapshot()
    assert snapshot.nodes[0].telemetry is not None
    sample = snapshot.nodes[0].telemetry.sample
    sample.gpu_memory_total_bytes, sample.gpu_memory_free_bytes = 100, 30
    sample.memory_total_bytes, sample.memory_available_bytes = 200, 40
    sample.gpu_temperature_c = 65
    registry = MetricsRegistry()
    registry.update_fleet(snapshot)
    for suffix, value in (
        ("gpu_memory_used_bytes", 70),
        ("host_memory_used_bytes", 160),
        ("gpu_temperature_c", 65),
    ):
        assert (
            f'vonk_node_telemetry_{suffix}{{node_id="{NODE}"}} {value}'
            in registry.render()
        )
    registry.update_fleet(_fleet_snapshot(telemetry=None))
    assert (
        f'vonk_node_telemetry_gpu_memory_used_bytes{{node_id="{NODE}"}}'
        not in registry.render()
    )
