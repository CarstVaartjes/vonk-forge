"""Prometheus producer-to-operator seams; no live monitoring dependency."""

import asyncio
import base64
import json
import re
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import get_args

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from starlette import status
from vonk_agent_protocol.gpu_clock import GpuClockObservation
from vonk_agent_protocol.inventory import HostSettings
from vonk_agent_protocol.swap import SwapObservation
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.litellm import PROMETHEUS_EXCLUDE_METRICS
from vonk_control.metrics import MetricsRegistry
from vonk_control.metrics_contract import (
    METRIC_SPECS,
    Metric,
    MetricRange,
    MetricsSeriesResponse,
)
from vonk_control.prometheus_api import QUERIES, RANGES, PrometheusReader

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


@pytest.mark.parametrize("metric", get_args(Metric))
@pytest.mark.parametrize("range", get_args(MetricRange))
def test_every_query_window_covers_the_step_and_scrapes(metric, range):
    """Catches a rate window shorter than its step, which drops bursts between points."""
    query, step = QUERIES[metric], max(15, RANGES[range] // 480)
    for selector in ("", '{node_id="x"}'):
        expressions = [
            query.series(selector, step),
            query.series(selector, step, fleet=True),
            query.peak(selector, RANGES[range]),
            query.peak(selector, RANGES[range], fleet=True),
            query.total(selector, RANGES[range]),
        ]
        for expression in filter(None, expressions):
            assert (
                "{" not in expression.replace(selector, "")
                if selector
                else "{" not in expression
            )
    windows = {int(w) for w in re.findall(r"\[(\d+)s\]", query.series("", step))}
    assert len(windows) == 1
    window = windows.pop()
    assert window >= step and window >= 2 * query.scrape
    if query.counted:
        assert window >= step + query.scrape


def test_every_metric_has_one_spec_and_one_query():
    """Catches a metric added to the contract without its description or query."""
    assert set(METRIC_SPECS) == set(QUERIES) == set(get_args(Metric))
    assert {s.metric for s in METRIC_SPECS.values() if s.summary_total} == {
        m for m, q in QUERIES.items() if q.total("", 1)
    }


def test_no_query_reads_a_gateway_family_the_gateway_does_not_export():
    """Catches a Trends query on a LiteLLM family dropped by PROMETHEUS_EXCLUDE_METRICS."""
    excluded = set(PROMETHEUS_EXCLUDE_METRICS)
    suffixes = ("_total", "_bucket", "_sum", "_count")

    def base(name: str) -> str:
        return next((name.removesuffix(x) for x in suffixes if name.endswith(x)), name)

    hits = [
        family
        for q in QUERIES.values()
        for family in (q.family, q.over)
        if family
        and family.startswith("litellm_")
        and {family, base(family)} & excluded
    ]
    assert not hits


def test_gateway_estimates_read_buckets_and_divide_summed_rates():
    """Catches a quantile over unsummed buckets (one value per key) or a speed of mismatched aliases."""
    p95 = QUERIES["gateway_ttft_p95"].series("", 300)
    assert p95.startswith("histogram_quantile(0.95, sum by (alias, le) (label_replace(")
    assert "litellm_llm_api_time_to_first_token_metric_bucket" in p95
    speed = QUERIES["gateway_generation_speed"].series("", 300)
    assert speed.count("sum by (alias)") == 2 and " / " in speed


def test_catalogue_lists_every_metric(client):
    """Catches a page or vonkctl having to hard-code what a metric is."""
    response = client.get("/api/metrics/catalogue")
    assert response.status_code == 200
    assert {item["metric"] for item in response.json()} == set(get_args(Metric))


@pytest.mark.parametrize("metric", ["gpu_utilization", "output_token_rate"])
def test_series_carry_their_summary(client, monkeypatch, metric):
    """Catches averaged points standing in for the peak and the total, and NaN as 0."""
    label = "node_id" if metric == "gpu_utilization" else "alias"
    seen = []
    now = time.time()
    monkeypatch.setattr("vonk_control.prometheus_api.time.time", lambda: now)

    def handle(request):
        params = request.url.params
        seen.append(request.url.path)
        fleet = "sum(" in params["query"] and "sum by" not in params["query"]
        if request.url.path == "/api/v1/query_range":
            rows = [
                {
                    "metric": {label: "a"},
                    "values": [[now - 30, "2"], [now - 15, "NaN"], [now, "6"]],
                },
                {"metric": {label: "empty"}, "values": [[1, "NaN"]]},
            ]
            if fleet:
                rows = [{"metric": {}, "values": [[now, "8"]]}]
            return reply(resultType="matrix", result=rows)
        value = "9" if "max_over_time" in params["query"] else "100"
        row = {"metric": {} if fleet else {label: "a"}, "value": [3, value]}
        if fleet:
            row["value"] = [3, "400" if value == "100" else "11"]
        return reply(resultType="vector", result=[row])

    mock_prometheus(monkeypatch, handle)
    body = client.get(
        "/api/metrics/series", params={"metric": metric, "range": "1h"}
    ).json()
    series = MetricsSeriesResponse.model_validate(body).series
    if metric == "output_token_rate":
        # The fleet line leads, has no label and carries the fleet total the shares divide by.
        line, *series = series
        assert line.labels == {} and (line.summary.latest, line.summary.total) == (
            8,
            400,
        )
        assert line.summary.share is None
    full, empty = series
    assert full.labels == {label: "a"}
    assert (full.summary.latest, full.summary.average, full.summary.peak) == (6, 4, 9)
    assert full.summary.total == (100 if metric == "output_token_rate" else None)
    assert full.summary.share == (0.25 if metric == "output_token_rate" else None)
    assert full.summary.coverage == 2 / (3600 // 15 + 1)
    assert (empty.summary.latest, empty.summary.average, empty.summary.peak) == (
        None,
        None,
        None,
    )
    assert empty.summary.coverage == 0
    assert seen.count("/api/v1/query") == (4 if metric == "output_token_rate" else 1)


def test_node_filter_on_a_model_metric_is_the_aliases_it_serves(reader, monkeypatch):
    """Catches a Spark view of per-model metrics that is refused, unfiltered or injectable."""
    seen = []

    def handle(request):
        seen.append(request.url.params["query"])
        return reply(
            resultType="matrix" if "range" in request.url.path else "vector", result=[]
        )

    mock_prometheus(monkeypatch, handle)
    asyncio.run(
        reader.series("request_rate", "1h", NODE, lambda node: ["glm-5.1", 'a"b'])
    )
    expected = r'{requested_model=~"^(glm\\-5\\.1|a\"b)$"}'
    assert seen and all(expected in query for query in seen)
    seen.clear()
    response = asyncio.run(reader.series("request_rate", "1h", NODE, lambda node: []))
    assert response.series == [] and not seen


@pytest.mark.parametrize(
    "params",
    [
        {"metric": "up", "range": "1h"},
        {"metric": "gpu_utilization", "range": "2h"},
        {"metric": "gpu_utilization", "range": "1h", "node": 'spk_"'},
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
    mock_prometheus(
        monkeypatch,
        lambda request: reply(
            resultType="matrix" if request.url.path.endswith("_range") else "vector",
            result=[],
        ),
    )
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
            await asyncio.wait_for(started.wait(), timeout=30)
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
            await asyncio.wait_for(waiting.wait(), timeout=30)
            assert reader.cached_attention() == ([], True)
            mock_prometheus(monkeypatch, lambda request: reply(alerts=[]))
            waiting.clear()
            retry_due.set()
            await asyncio.wait_for(waiting.wait(), timeout=30)
            assert reader.cached_attention() == ([], False)
            clock[0] += 16
            assert reader.cached_attention() == ([], True)

    asyncio.run(exercise())


def test_exporter_publishes_memory_and_temperature_without_inventing_missing_values():
    """Catches queries pointing at absent gauges or unknown readings becoming zero."""
    snapshot = _fleet_snapshot()
    assert snapshot.nodes[0].telemetry is not None
    sample = snapshot.nodes[0].telemetry.sample
    snapshot.nodes[0].telemetry.sample = sample.model_copy(
        update={
            "gpu_memory_total_bytes": 100,
            "gpu_memory_free_bytes": 30,
            "memory_total_bytes": 200,
            "memory_available_bytes": 40,
            "gpu_temperature_c": 65,
        }
    )
    registry = MetricsRegistry()
    registry.set_fleet_source(lambda: snapshot)
    for suffix, value in (
        ("gpu_memory_used_bytes", 70),
        ("host_memory_used_bytes", 160),
        ("gpu_temperature_c", 65),
    ):
        assert (
            f'vonk_node_telemetry_{suffix}{{node_id="{NODE}"}} {value}'
            in registry.render()
        )
    registry.set_fleet_source(lambda: _fleet_snapshot(telemetry=None))
    assert (
        f'vonk_node_telemetry_gpu_memory_used_bytes{{node_id="{NODE}"}}'
        not in registry.render()
    )


def test_exporter_publishes_the_spark_facts_that_trend():
    """Catches a fact the Fleet row shows but Prometheus never sees, and a gap read as zero."""
    snapshot = _fleet_snapshot(
        readings={"gpu_power_watts": 35.5, "gpu_process_memory_bytes": 7}
    )
    node = snapshot.nodes[0]
    inventory = node.inventory
    assert inventory is not None and node.telemetry is not None
    clock = GpuClockObservation(graphics_mhz=2190)
    node.telemetry.sample = node.telemetry.sample.model_copy(
        update={"gpu_clock": clock, "system_memory_bytes": 5}
    )
    node.inventory = inventory.model_copy(
        update={
            "host_settings": HostSettings(
                display_manager_active=False,
                graphical_default_target=False,
                swap=SwapObservation(total_bytes=64, free_bytes=60),
            )
        }
    )
    registry = MetricsRegistry()
    registry.set_fleet_source(lambda: snapshot)
    text = registry.render()
    label = f'{{node_id="{NODE}"}}'
    for series, value in (
        ("telemetry_gpu_power_watts", "35.5"),
        ("telemetry_gpu_process_memory_bytes", "7"),
        ("telemetry_system_memory_bytes", "5"),
        ("telemetry_gpu_graphics_clock_mhz", "2190"),
        ("inventory_swap_used_bytes", "4"),
        ("inventory_disk_used_bytes", "3000"),
    ):
        assert f"vonk_node_{series}{label} {value}" in text
    node.inventory = inventory
    node.telemetry.sample = node.telemetry.sample.model_copy(
        update={"gpu_clock": None, "gpu_power_watts": None}
    )
    text = registry.render()
    for series in (
        "inventory_swap_used_bytes",
        "telemetry_gpu_graphics_clock_mhz",
        "telemetry_gpu_power_watts",
    ):
        assert f"vonk_node_{series}" not in text

    node.telemetry.sample = node.telemetry.sample.model_copy(
        update={"gpu_power_watts": 35.5, "gpu_clock": clock}
    )
    node.telemetry.freshness = "stale"
    text = registry.render()
    # A silent Spark contributes no value, so fleet power and energy skip it.
    assert "vonk_node_telemetry_gpu_power_watts" not in text
    assert "vonk_node_telemetry_gpu_graphics_clock_mhz" not in text


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
        assert observed.wait(timeout=30)
    assert lifecycle == [app, app]


def test_latest_is_only_a_value_from_the_last_step(reader, monkeypatch):
    """Catches a stale last sample shown as the current value."""
    now = 1_000_000.0
    monkeypatch.setattr("vonk_control.prometheus_api.time.time", lambda: now)

    def handle(request):
        if request.url.path.endswith("_range"):
            rows = [
                {"metric": {"node_id": "a"}, "values": [[now - 5, "1"]]},
                {"metric": {"node_id": "b"}, "values": [[now - 600, "2"]]},
            ]
            return reply(resultType="matrix", result=rows)
        return reply(resultType="vector", result=[])

    mock_prometheus(monkeypatch, handle)
    fresh, stale = asyncio.run(
        reader.series("gpu_utilization", "1h", None, lambda node: [])
    ).series
    assert (fresh.summary.latest, stale.summary.latest) == (1, None)
