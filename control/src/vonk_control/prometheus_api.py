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
    counts down), `counter` is the per-second rate of a gateway counter by alias.
    """

    family: str
    kind: Literal["gauge", "last", "counter"]

    @property
    def scrape(self) -> int:
        return (
            GATEWAY_SCRAPE_SECONDS if self.kind == "counter" else CONTROL_SCRAPE_SECONDS
        )

    @property
    def floor(self) -> int:
        """Four scrapes: enough samples for a rate to exist and not flap."""
        return 4 * self.scrape

    def window(self, step: int) -> int:
        """Never smaller than the step, so no sample falls between two points."""
        return max(step, self.floor)

    def _by_alias(self, expression: str) -> str:
        return (
            f"sum by (alias) (label_replace({expression}, "
            '"alias", "$1", "requested_model", "(.+)"))'
        )

    def series(self, selector: str, step: int) -> str:
        window = self.window(step)
        match self.kind:
            case "gauge":
                return f"avg_over_time({self.family}{selector}[{window}s])"
            case "last":
                return f"last_over_time({self.family}{selector}[{window}s])"
            case "counter":
                return self._by_alias(f"rate({self.family}{selector}[{window}s])")

    def peak(self, selector: str, duration: int) -> str:
        """The highest raw value in the range; averaged points would understate it."""
        if self.kind != "counter":
            return f"max_over_time({self.family}{selector}[{duration}s])"
        raw = self._by_alias(f"rate({self.family}{selector}[{self.floor}s])")
        return f"max_over_time({raw}[{duration}s:{self.floor}s])"

    def total(self, selector: str, duration: int) -> str | None:
        if self.kind != "counter":
            return None
        return self._by_alias(f"increase({self.family}{selector}[{duration}s])")


QUERIES: dict[Metric, MetricQuery] = {
    "gpu_utilization": MetricQuery(
        "vonk_node_telemetry_gpu_utilization_percent", "gauge"
    ),
    "gpu_memory_used": MetricQuery(
        "vonk_node_telemetry_gpu_memory_used_bytes", "gauge"
    ),
    "host_memory_used": MetricQuery(
        "vonk_node_telemetry_host_memory_used_bytes", "gauge"
    ),
    "gpu_temperature": MetricQuery("vonk_node_telemetry_gpu_temperature_c", "gauge"),
    "request_rate": MetricQuery("litellm_requests_metric_total", "counter"),
    "output_token_rate": MetricQuery("litellm_output_tokens_metric_total", "counter"),
    "certificate_expiry": MetricQuery("vonk_agent_certificate_expiry_seconds", "last"),
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
) -> MetricSeries:
    """A series with its summary; a series without its label is the whole fleet."""
    key = name or ""
    found = [point.value for point in points if point.value is not None]
    return MetricSeries(
        labels={label.value: name} if name else {},
        points=points,
        summary=SeriesSummary(
            latest=found[-1] if found else None,
            average=sum(found) / len(found) if found else None,
            peak=peaks.get(key),
            total=totals.get(key),
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
        aliases: Callable[[str], list[str]] | None = None,
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
                if label is SeriesLabel.NODE_ID or aliases is None
                else await asyncio.to_thread(aliases, node)
            )
            if not values:
                return _response(spec, range, end, step, [])
            selector = _selector(label, values)
        total = query.total(selector, duration)
        matrix, peaks, totals = await asyncio.gather(
            self.read(
                "/api/v1/query_range",
                _RangeQuery(
                    query=query.series(selector, step),
                    start=end - duration,
                    end=end,
                    step=step,
                ).model_dump(),
            ),
            self._instant(query.peak(selector, duration), end, label),
            self._instant(total, end, label) if total else _nothing(),
        )
        if not isinstance(matrix, _Matrix):
            raise PrometheusUnavailable("Prometheus returned no matrix")
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
                )
                for row in matrix.result
            ]
        except ValueError as error:
            raise PrometheusUnavailable(
                "Prometheus returned invalid samples"
            ) from error
        return _response(spec, range, end, step, series)

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
    served_aliases: Callable[[str], list[str]] | None = None,
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
