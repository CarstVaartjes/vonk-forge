"""Prometheus producer-to-operator seams; no live monitoring dependency."""

import asyncio
import base64
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2 as httpx
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

_HTTP_CLIENT = httpx.AsyncClient


@pytest.fixture
def reader():
    return PrometheusReader("http://monitoring.test")


@pytest.fixture
def client(reader, monkeypatch):
    codec = TokenCodec(b"k" * 32)

    class Projection:
        def read(self):
            return _fleet_snapshot()

    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        fleet_projection=Projection(),
        prometheus=reader,
        now=lambda: 10,
    )
    token = codec.issue(Actor("reader", "viewer"), ttl_seconds=100, now=10)
    return TestClient(app, headers={"Authorization": "Bearer " + token})


def mock_prometheus(monkeypatch, handler):
    monkeypatch.setattr(
        "vonk_control.prometheus_api.httpx.AsyncClient",
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
    asyncio.run(reader._refresh_attention())
    document = fleet_document(client)
    assert [item["summary"] for item in document["attention"]] == ["GPU is hot"]
    assert document["attention"][0]["source"] == "prometheus"
    assert document["attention_unavailable"] is False
    mock_prometheus(monkeypatch, lambda request: httpx.Response(503))
    asyncio.run(reader._refresh_attention())
    document = fleet_document(client)
    assert document["attention_unavailable"] is True
    assert document["nodes"][0]["id"] == NODE


def test_cached_attention_does_not_start_threads(monkeypatch):
    """Catches per-application background threads dispatched by Fleet polling."""
    monkeypatch.setattr(
        "threading.Thread.start",
        lambda worker: pytest.fail("Fleet read dispatched a background thread"),
    )
    assert PrometheusReader().cached_attention() == ([], True)


def test_app_reads_never_dispatch_prometheus_workers(monkeypatch):
    """Catches implicit network/worker creation in every test application."""
    monkeypatch.setattr(
        "vonk_control.prometheus_api.httpx.AsyncClient",
        lambda **kwargs: pytest.fail("unconfigured application contacted Prometheus"),
    )

    class Projection:
        def read(self):
            return _fleet_snapshot()

    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(), tokens=codec, fleet_projection=Projection(), now=lambda: 10
    )
    token = codec.issue(Actor("reader", "viewer"), ttl_seconds=100, now=10)
    with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
        assert fleet_document(client)["attention_unavailable"] is True
        assert (
            client.get(
                "/api/metrics/series?metric=gpu_utilization&range=1h"
            ).status_code
            == status.HTTP_503_SERVICE_UNAVAILABLE
        )


def test_fleet_never_waits_and_shutdown_cancels_inflight_refresh(
    client, reader, monkeypatch
):
    """Catches polling on reads, blocked Fleet responses and leaked shutdown I/O."""

    async def exercise():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(request):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        mock_prometheus(monkeypatch, blocked)
        async with reader.lifespan():
            await asyncio.wait_for(started.wait(), timeout=1)
            assert fleet_document(client)["attention_unavailable"] is True
            assert fleet_document(client)["attention_unavailable"] is True
        assert cancelled.is_set()

    asyncio.run(exercise())


def test_alert_cache_retries_outage_and_reports_stale_observation(reader, monkeypatch):
    """Catches permanent outage caching and stale observations reported as current."""
    clock = [100.0]
    monkeypatch.setattr(
        "vonk_control.prometheus_api.time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    async def exercise():
        waiting, retry_due = asyncio.Event(), asyncio.Event()

        async def advance(delay):
            waiting.set()
            await retry_due.wait()
            retry_due.clear()
            clock[0] += delay

        monkeypatch.setattr("vonk_control.prometheus_api.asyncio.sleep", advance)
        mock_prometheus(monkeypatch, lambda request: httpx.Response(503))
        async with reader.lifespan():
            await asyncio.wait_for(waiting.wait(), timeout=1)
            assert reader.cached_attention() == ([], True)
            mock_prometheus(monkeypatch, lambda request: reply(alerts=[]))
            waiting.clear()
            retry_due.set()
            await asyncio.wait_for(waiting.wait(), timeout=1)
            assert reader.cached_attention() == ([], False)
            clock[0] += 16
            assert reader.cached_attention() == ([], True)

    asyncio.run(exercise())


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


def test_application_owns_configured_monitoring_and_preserves_service_lifespan(
    reader, monkeypatch
):
    """Catches polling wired only in production or replacing service cleanup."""
    observed = threading.Event()
    lifecycle = []

    def alerts(request):
        assert request.url.host == "monitoring.test"
        observed.set()
        return reply(alerts=[])

    @asynccontextmanager
    async def service_lifespan(app):
        lifecycle.append(app)
        try:
            yield
        finally:
            lifecycle.append(app)

    mock_prometheus(monkeypatch, alerts)
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
        prometheus=reader,
        lifespan=service_lifespan,
    )
    with TestClient(app):
        assert lifecycle == [app]
        assert observed.wait(timeout=1)
    assert lifecycle == [app, app]
