"""Prometheus producer-to-operator seams; no live monitoring dependency."""

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette import status
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.metrics import MetricsRegistry
from vonk_control.metrics_contract import MetricsSeriesResponse

from .test_api import Jobs
from .test_metrics import NODE, _fleet_snapshot

_HTTP_CLIENT = httpx.Client


@pytest.fixture
def client():
    codec = TokenCodec(b"k" * 32)

    class Projection:
        def read(self):
            return _fleet_snapshot()

    app = create_app(
        jobs=Jobs(), tokens=codec, fleet_projection=Projection(), now=lambda: 10
    )
    return TestClient(
        app,
        headers={
            "Authorization": "Bearer "
            + codec.issue(Actor("reader", "viewer"), ttl_seconds=100, now=10)
        },
    )


def mock_prometheus(monkeypatch, handler):
    monkeypatch.setattr(
        "vonk_control.prometheus_api.httpx.Client",
        lambda **kwargs: _HTTP_CLIENT(**kwargs, transport=httpx.MockTransport(handler)),
    )


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
        assert request.url.path == "/api/v1/query_range"
        assert request.url.params["query"] == query + (
            f'{{node_id="{NODE}"}}' if node else ""
        )
        assert (
            float(request.url.params["end"]) - float(request.url.params["start"])
            == duration
        )
        assert int(request.url.params["step"]) == max(15, duration // 480)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"node_id": NODE},
                            "values": [[1, "2.5"], [2, "NaN"]],
                        }
                    ],
                },
            },
        )

    mock_prometheus(monkeypatch, handle)
    response = client.get(
        "/api/metrics/series",
        params={"metric": metric, "range": range, **({"node": node} if node else {})},
    )
    assert response.status_code == 200, response.text
    parsed = MetricsSeriesResponse.model_validate_json(response.content)
    assert parsed.series[0].points[0].value == 2.5
    assert parsed.series[0].points[1].value is None


def test_series_auth_and_closed_input(client, monkeypatch):
    """Catches unauthenticated reads and PromQL injection through selectors."""
    mock_prometheus(
        monkeypatch, lambda request: pytest.fail("invalid input reached Prometheus")
    )
    assert (
        client.get("/api/metrics/series?metric=up&range=1h").status_code
        == status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    assert (
        client.get("/api/metrics/series?metric=gpu_utilization&range=2h").status_code
        == status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    assert (
        client.get(
            '/api/metrics/series?metric=gpu_utilization&range=1h&node=spk_"'
        ).status_code
        == status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    assert (
        client.get(
            "/api/metrics/series",
            params={"metric": "request_rate", "range": "1h", "node": NODE},
        ).status_code
        == status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    client.headers.pop("Authorization")
    assert (
        client.get("/api/metrics/series?metric=gpu_utilization&range=1h").status_code
        == 401
    )


@pytest.mark.parametrize("failure", ["connection", "status", "malformed"])
def test_prometheus_down_is_retryable(client, monkeypatch, failure):
    """Catches upstream failure becoming a 500 or a permanent refusal."""

    def handle(request):
        if failure == "connection":
            raise httpx.ConnectError("down", request=request)
        return (
            httpx.Response(503)
            if failure == "status"
            else httpx.Response(200, json={"status": "success", "data": {}})
        )

    mock_prometheus(monkeypatch, handle)
    response = client.get("/api/metrics/series?metric=gpu_utilization&range=1h")
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "5"


def test_firing_alerts_reach_fleet_attention_and_outage_is_visible(client, monkeypatch):
    """Catches lost firing alerts, pending alerts shown as firing, and false all-clear."""

    def handle(request):
        assert request.url.path == "/api/v1/alerts"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "alerts": [
                        {
                            "state": state,
                            "labels": {
                                "alertname": "NodeHot",
                                "node_id": NODE,
                                "severity": "critical",
                            },
                            "annotations": {"summary": "GPU is hot"},
                            "activeAt": "2026-10-10T00:00:00Z",
                            "value": "1",
                        }
                        for state in ("firing", "pending")
                    ]
                },
            },
        )

    mock_prometheus(monkeypatch, handle)
    response = client.get("/api/fleet")
    assert response.status_code == 200, response.text

    # The observation transport owns serialization, including its chunked document.
    def _fleet_document(response):
        records = [json.loads(line) for line in response.iter_lines()]
        return json.loads(
            b"".join(base64.b64decode(record["data"]) for record in records[1:-1])
        )

    document = _fleet_document(response)
    assert [item["summary"] for item in document["attention"]] == ["GPU is hot"]
    assert document["attention"][0]["source"] == "prometheus"
    assert document["attention_unavailable"] is False
    mock_prometheus(monkeypatch, lambda request: httpx.Response(503))
    document = _fleet_document(client.get("/api/fleet"))
    assert document["attention_unavailable"] is True
    assert document["nodes"][0]["id"] == NODE


def test_exporter_publishes_used_memory_and_temperature_without_inventing_missing_values():
    """Catches queries pointing at absent gauges or unknown readings becoming zero."""
    snapshot = _fleet_snapshot()
    assert snapshot.nodes[0].telemetry is not None
    sample = snapshot.nodes[0].telemetry.sample
    sample.gpu_memory_total_bytes = 100
    sample.gpu_memory_free_bytes = 30
    sample.memory_total_bytes = 200
    sample.memory_available_bytes = 40
    sample.gpu_temperature_c = 65
    registry = MetricsRegistry()
    registry.update_fleet(snapshot)
    text = registry.render()
    for suffix, value in (
        ("gpu_memory_used_bytes", 70),
        ("host_memory_used_bytes", 160),
        ("gpu_temperature_c", 65),
    ):
        assert f'vonk_node_telemetry_{suffix}{{node_id="{NODE}"}} {value}' in text
    registry.update_fleet(_fleet_snapshot(telemetry=None))
    assert (
        f'vonk_node_telemetry_gpu_memory_used_bytes{{node_id="{NODE}"}}'
        not in registry.render()
    )
