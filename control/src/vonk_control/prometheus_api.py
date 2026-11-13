"""Read-only fixed-query adapter to the internal Prometheus service."""

import asyncio
import math
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import httpx2 as httpx
from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, ValidationError
from vonk_agent_protocol import UnknownOutcomeError

from .auth import Actor
from .metrics_contract import (
    METRIC_SPECS,
    SERIES_LABEL,
    Metric,
    MetricPoint,
    MetricRange,
    MetricSeries,
    MetricSpec,
    MetricsSeriesRequest,
    MetricsSeriesResponse,
    PrometheusAttention,
    SeriesLabel,
    SeriesSummary,
)
from .operation_api import _ADMIN_OPERATION_IDS, bounded_error_responses

# Scrape intervals of the jobs in deploy/compose/prometheus/prometheus.yml.
CONTROL_SCRAPE_SECONDS = 15
GATEWAY_SCRAPE_SECONDS = 30
RANGES: dict[MetricRange, int] = {
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
    "30d": 2592000,
}


@dataclass(frozen=True)
class MetricQuery:
    """One metric's fixed PromQL, rendered from its family; no client text reaches it.

    `gauge` averages a gauge, `last` keeps its newest sample (a value that only
    counts down), `rate` is a counter's per-second rate (`over` divides it by
    another counter's rate, times `scale`), `increase` counts what a counter
    gained, `energy` is a power gauge's Wh per step. A `gateway` metric reads
    LiteLLM's families and is summed by alias; any other keeps its `node_id`.
    """

    family: str
    kind: Literal["gauge", "last", "rate", "increase", "energy"]
    gateway: bool = False
    over: str | None = None
    scale: int = 1

    @property
    def scrape(self) -> int:
        return GATEWAY_SCRAPE_SECONDS if self.gateway else CONTROL_SCRAPE_SECONDS

    @property
    def floor(self) -> int:
        """Four scrapes: enough samples for a rate to exist and not flap."""
        return 4 * self.scrape

    @property
    def counted(self) -> bool:
        return self.kind in ("rate", "increase")

    def window(self, step: int) -> int:
        """Never smaller than the step, so no sample falls between two points.

        A counter needs two samples inside its window, so (Grafana's rate
        interval) it also spans the step plus one scrape.
        """
        if self.counted:
            return max(step + self.scrape, self.floor)
        return max(step, self.floor)

    def _group(self, expression: str, fleet: bool) -> str:
        if fleet:
            return f"sum({expression})"
        if not self.gateway:
            return expression
        return (
            f"sum by (alias) (label_replace({expression}, "
            '"alias", "$1", "requested_model", "(.+)"))'
        )

    def series(self, selector: str, step: int, fleet: bool = False) -> str:
        window = self.window(step)

        def read(function: str, family: str) -> str:
            return self._group(f"{function}({family}{selector}[{window}s])", fleet)

        match self.kind:
            case "gauge":
                return read("avg_over_time", self.family)
            case "last":
                return read("last_over_time", self.family)
            case "increase":
                return read("increase", self.family)
            case "energy":
                return f"{read('avg_over_time', self.family)} * {step} / 3600"
            case "rate":
                rate = read("rate", self.family)
                if self.over is None:
                    return rate
                scaled = f"{self.scale} * " if self.scale != 1 else ""
                return f"{scaled}{rate} / {read('rate', self.over)}"

    def peak(self, selector: str, duration: int, fleet: bool = False) -> str | None:
        """The highest raw value in the range; averaged points would understate it."""
        if self.kind == "energy":
            return None
        if self.kind in ("gauge", "last") and not fleet:
            return f"max_over_time({self.family}{selector}[{duration}s])"
        # Evaluated every floor seconds over a window that leaves no gap between them.
        raw = self.series(selector, self.floor, fleet)
        return f"max_over_time(({raw})[{duration}s:{self.floor}s])"

    def total(self, selector: str, duration: int, fleet: bool = False) -> str | None:
        """What the range added up to; only for counted and energy metrics."""
        if self.kind == "energy":
            window = f"sum_over_time({self.family}{selector}[{duration}s])"
            return f"{self._group(window, fleet)} * {self.scrape} / 3600"
        if self.counted and self.over is None:
            return self._group(f"increase({self.family}{selector}[{duration}s])", fleet)
        return None


def _node(
    family: str,
    kind: Literal["gauge", "rate", "increase", "energy"] = "gauge",
    over: str | None = None,
    scale: int = 1,
) -> MetricQuery:
    return MetricQuery(f"vonk_node_{family}", kind, False, over, scale)


def _gateway(family: str) -> MetricQuery:
    return MetricQuery(f"litellm_{family}", "rate", True)


QUERIES: dict[Metric, MetricQuery] = {
    "gpu_utilization": _node("telemetry_gpu_utilization_percent"),
    "gpu_memory_used": _node("telemetry_gpu_memory_used_bytes"),
    "host_memory_used": _node("telemetry_host_memory_used_bytes"),
    "gpu_temperature": _node("telemetry_gpu_temperature_c"),
    "request_rate": _gateway("requests_metric_total"),
    "output_token_rate": _gateway("output_tokens_metric_total"),
    "certificate_expiry": MetricQuery("vonk_agent_certificate_expiry_seconds", "last"),
    "gpu_power": _node("telemetry_gpu_power_watts"),
    "gpu_energy": _node("telemetry_gpu_power_watts", "energy"),
    "models_memory": _node("telemetry_gpu_process_memory_bytes"),
    "system_memory": _node("telemetry_system_memory_bytes"),
    "gpu_clock": _node("telemetry_gpu_graphics_clock_mhz"),
    "swap_used": _node("inventory_swap_used_bytes"),
    "disk_used": _node("inventory_disk_used_bytes"),
    "cpu_utilization": _node(
        "cpu_busy_seconds_total", "rate", "vonk_node_cpu_seconds_total", 100
    ),
    "load_average": _node("load_average_1m"),
    "board_temperature": _node("board_temperature_c"),
    "nvme_temperature": _node("nvme_temperature_c"),
    "nic_temperature": _node("nic_temperature_c"),
    "fan_speed": _node("fan_rpm"),
    "network_lan_rx": _node("network_lan_rx_bytes_total", "rate"),
    "network_lan_tx": _node("network_lan_tx_bytes_total", "rate"),
    "fabric_rx": _node("fabric_rx_bytes_total", "rate"),
    "fabric_tx": _node("fabric_tx_bytes_total", "rate"),
    "disk_read": _node("disk_read_bytes_total", "rate"),
    "disk_write": _node("disk_written_bytes_total", "rate"),
    "oom_kills": _node("oom_kills_total", "increase"),
}


def _selector(label: SeriesLabel, values: list[str]) -> str:
    """A label filter from validated Spark ids or published aliases, never client PromQL."""
    if label is SeriesLabel.NODE_ID:
        return f'{{node_id="{values[0]}"}}'
    pattern = "^(" + "|".join(re.escape(value) for value in values) + ")$"
    # PromQL strings treat a backslash as an escape, so each one is doubled.
    quoted = pattern.replace("\\", "\\\\").replace('"', '\\"')
    return '{requested_model=~"' + quoted + '"}'


class _External(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _RangeQuery(_External):
    query: str
    start: float
    end: float
    step: int


class _MatrixRow(_External):
    metric: dict[str, str]
    values: list[tuple[float, str]]


class _Matrix(_External):
    resultType: Literal["matrix"]
    result: list[_MatrixRow]


class _VectorRow(_External):
    metric: dict[str, str]
    value: tuple[float, str]


class _Vector(_External):
    resultType: Literal["vector"]
    result: list[_VectorRow]


class _Alert(_External):
    labels: dict[str, str]
    annotations: dict[str, str]
    state: Literal["pending", "firing", "inactive"]
    activeAt: str


class _Alerts(_External):
    alerts: list[_Alert]


class _Reply(_External):
    status: Literal["success"]
    data: _Matrix | _Vector | _Alerts


class PrometheusUnavailable(UnknownOutcomeError):
    """An observation can be retried without changing operator intent."""


async def _nothing() -> dict[str, float]:
    return {}


def _series(
    name: str | None,
    points: list[MetricPoint],
    expected: int,
    peaks: dict[str, float],
    totals: dict[str, float],
    label: SeriesLabel,
    fresh_after: float,
) -> MetricSeries:
    """A series with its summary; a series without its label is the fleet line."""
    key = name or ""
    total, fleet_total = totals.get(key), totals.get("")
    found = [point.value for point in points if point.value is not None]
    return MetricSeries(
        labels={label.value: name} if name else {},
        points=points,
        summary=SeriesSummary(
            # Only a value from the last step is "now"; an older one is a gap.
            latest=found[-1]
            if found
            and points[-1].value is not None
            and points[-1].timestamp > fresh_after
            else None,
            average=sum(found) / len(found) if found else None,
            peak=peaks.get(key),
            total=total,
            share=min(1.0, total / fleet_total)
            if name and total is not None and fleet_total
            else None,
            coverage=min(1.0, len(found) / expected),
        ),
    )


def _response(
    spec: MetricSpec,
    range: MetricRange,
    end: float,
    step: int,
    series: list[MetricSeries],
) -> MetricsSeriesResponse:
    return MetricsSeriesResponse(
        metric=spec.metric,
        spec=spec,
        range=range,
        start=end - RANGES[range],
        end=end,
        step_seconds=step,
        series=series,
    )


class PrometheusReader:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url
        self._attention: tuple[list[PrometheusAttention], bool, float] = ([], True, 0)

    def cached_attention(self) -> tuple[list[PrometheusAttention], bool]:
        items, unavailable, expires = self._attention
        return items, unavailable or time.monotonic() >= expires

    async def _refresh_attention(self) -> None:
        try:
            self._attention = (await self.attention(), False, time.monotonic() + 15)
        except PrometheusUnavailable:
            self._attention = ([], True, time.monotonic() + 15)

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        # Only the production server owns polling; reads never dispatch work.
        async def refresh() -> None:
            while True:
                next_attempt = self._attention[2]
                if time.monotonic() >= next_attempt:
                    await self._refresh_attention()
                await asyncio.sleep(max(0, self._attention[2] - time.monotonic()))

        task = asyncio.create_task(refresh()) if self.base_url else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def read(
        self, path: str, params: dict[str, str | int | float]
    ) -> _Matrix | _Vector | _Alerts:
        if not self.base_url:
            raise PrometheusUnavailable("Prometheus is not configured")
        try:
            async with (
                asyncio.timeout(5),
                httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(5, connect=1),
                    trust_env=False,
                ) as client,
                client.stream("GET", path, params=params) as response,
            ):
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > 4 * 1024 * 1024:
                        raise PrometheusUnavailable("Prometheus response exceeds 4 MiB")
                    body.extend(chunk)
                return _Reply.model_validate_json(bytes(body)).data
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            raise PrometheusUnavailable("Prometheus observation unavailable") from error

    async def series(
        self,
        metric: Metric,
        range: MetricRange,
        node: str | None,
        aliases: Callable[[str], list[str]],
    ) -> MetricsSeriesResponse:
        """One range read plus an instant read each for the peak and the total.

        `aliases` maps a Spark id to the aliases it serves, for model metrics.
        """
        spec, query = METRIC_SPECS[metric], QUERIES[metric]
        label = SERIES_LABEL[spec.scope]
        duration = RANGES[range]
        end = time.time()
        step = max(15, duration // 480)
        selector = ""
        if node is not None:
            values = (
                [node]
                if label is SeriesLabel.NODE_ID
                else await asyncio.to_thread(aliases, node)
            )
            if not values:
                return _response(spec, range, end, step, [])
            selector = _selector(label, values)
        # The fleet line sums the Sparks' (or models') series, so only an unfiltered read has one.
        lines = (False, True) if spec.fleet_sum and node is None else (False,)
        reads = await asyncio.gather(
            *(
                self._read_one(query, selector, duration, step, end, label, fleet)
                for fleet in lines
            )
        )
        # The fleet line leads; its peak and total are keyed by no label.
        matrix = [row for read in reversed(reads) for row in read[0]]
        peaks = {key: value for read in reads for key, value in read[1].items()}
        totals = {key: value for read in reads for key, value in read[2].items()}
        expected = duration // step + 1
        try:
            series = [
                _series(
                    row.metric.get(label),
                    [
                        MetricPoint(
                            timestamp=timestamp,
                            value=value if math.isfinite(value := float(raw)) else None,
                        )
                        for timestamp, raw in row.values
                    ],
                    expected,
                    peaks,
                    totals,
                    label,
                    end - step,
                )
                for row in matrix
            ]
        except ValueError as error:
            raise PrometheusUnavailable(
                "Prometheus returned invalid samples"
            ) from error
        return _response(spec, range, end, step, series)

    async def _read_one(
        self,
        query: MetricQuery,
        selector: str,
        duration: int,
        step: int,
        end: float,
        label: SeriesLabel,
        fleet: bool,
    ) -> tuple[list[_MatrixRow], dict[str, float], dict[str, float]]:
        """The range read with its peak and total reads, for the series or the fleet line."""
        peak, total = (
            query.peak(selector, duration, fleet),
            query.total(selector, duration, fleet),
        )
        matrix, peaks, totals = await asyncio.gather(
            self.read(
                "/api/v1/query_range",
                _RangeQuery(
                    query=query.series(selector, step, fleet),
                    start=end - duration,
                    end=end,
                    step=step,
                ).model_dump(),
            ),
            self._instant(peak, end, label) if peak else _nothing(),
            self._instant(total, end, label) if total else _nothing(),
        )
        if not isinstance(matrix, _Matrix):
            raise PrometheusUnavailable("Prometheus returned no matrix")
        return matrix.result, peaks, totals

    async def _instant(
        self, expression: str, at: float, label: SeriesLabel
    ) -> dict[str, float]:
        """Each series' value at `at`, keyed by its label value (`""` when it has none)."""
        data = await self.read("/api/v1/query", {"query": expression, "time": at})
        if not isinstance(data, _Vector):
            raise PrometheusUnavailable("Prometheus returned no vector")
        values: dict[str, float] = {}
        for row in data.result:
            try:
                value = float(row.value[1])
            except ValueError as error:
                raise PrometheusUnavailable(
                    "Prometheus returned invalid samples"
                ) from error
            if math.isfinite(value):
                values[row.metric.get(label, "")] = value
        return values

    async def attention(self) -> list[PrometheusAttention]:
        data = await self.read("/api/v1/alerts", {})
        if not isinstance(data, _Alerts):
            raise PrometheusUnavailable("Prometheus returned no alerts")
        return [
            PrometheusAttention(
                name=alert.labels.get("alertname", "Prometheus alert"),
                summary=alert.annotations.get(
                    "summary", alert.labels.get("alertname", "Prometheus alert")
                ),
                severity=alert.labels.get("severity", "unspecified"),
                labels=alert.labels,
                active_at=alert.activeAt,
            )
            for alert in data.alerts
            if alert.state == "firing"
        ]


def install_metrics_routes(
    app: FastAPI,
    authenticated: Any,
    reader: PrometheusReader,
    served_aliases: Callable[[str], list[str]],
) -> None:
    """`served_aliases` maps a Spark id to the aliases its loaded runs serve."""
    _ADMIN_OPERATION_IDS[("get", "/api/metrics/series")] = "getMetricsSeries"
    _ADMIN_OPERATION_IDS[("get", "/api/metrics/catalogue")] = "getMetricsCatalogue"

    @app.get(
        "/api/metrics/catalogue",
        response_model=list[MetricSpec],
        responses=bounded_error_responses(401),
        operation_id="getMetricsCatalogue",
    )
    def metrics_catalogue(_actor: Actor = authenticated) -> list[MetricSpec]:
        return list(METRIC_SPECS.values())

    @app.get(
        "/api/metrics/series",
        response_model=MetricsSeriesResponse,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="getMetricsSeries",
    )
    async def metrics_series(
        metric: Metric,
        range: MetricRange,
        node: Annotated[str | None, Query(pattern=r"^spk_[0-9a-f]{32}$")] = None,
        _actor: Actor = authenticated,
    ) -> MetricsSeriesResponse:
        try:
            query = MetricsSeriesRequest(metric=metric, range=range, node=node)
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from error
        return await reader.series(
            query.metric, query.range, query.node, served_aliases
        )
