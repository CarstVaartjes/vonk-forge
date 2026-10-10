"""Read-only fixed-query adapter to the internal Prometheus service."""

import math
import time
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, ValidationError
from vonk_agent_protocol import UnknownOutcomeError

from .auth import Actor
from .metrics_contract import (
    Metric,
    MetricPoint,
    MetricRange,
    MetricSeries,
    MetricsSeriesRequest,
    MetricsSeriesResponse,
    PrometheusAttention,
)
from .operation_api import _ADMIN_OPERATION_IDS, bounded_error_responses

QUERIES: dict[Metric, str] = {
    "gpu_utilization": "vonk_node_telemetry_gpu_utilization_percent{filter}",
    "gpu_memory_used": "vonk_node_telemetry_gpu_memory_used_bytes{filter}",
    "host_memory_used": "vonk_node_telemetry_host_memory_used_bytes{filter}",
    "gpu_temperature": "vonk_node_telemetry_gpu_temperature_c{filter}",
    "request_rate": "sum by (requested_model) (rate(litellm_requests_metric_total[5m]))",
    "certificate_expiry": "vonk_agent_certificate_expiry_seconds{filter}",
}
RANGES: dict[MetricRange, int] = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}


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


class _Alert(_External):
    labels: dict[str, str]
    annotations: dict[str, str]
    state: Literal["pending", "firing", "inactive"]
    activeAt: str


class _Alerts(_External):
    alerts: list[_Alert]


class _Reply(_External):
    status: Literal["success"]
    data: _Matrix | _Alerts


class PrometheusUnavailable(UnknownOutcomeError):
    """An observation can be retried without changing operator intent."""


class PrometheusReader:
    def read(
        self, path: str, params: dict[str, str | int | float]
    ) -> _Matrix | _Alerts:
        try:
            with httpx.Client(
                base_url="http://prometheus:9090", timeout=5, trust_env=False
            ) as client:
                response = client.get(path, params=params)
                response.raise_for_status()
                return _Reply.model_validate_json(response.content).data
        except (httpx.HTTPError, ValueError) as error:
            raise PrometheusUnavailable("Prometheus observation unavailable") from error

    def series(
        self, metric: Metric, range: MetricRange, node: str | None
    ) -> MetricsSeriesResponse:
        duration = RANGES[range]
        end = time.time()
        step = max(15, duration // 480)
        # Spark ids are validated by the route, never client-supplied PromQL.
        selector = "" if node is None else f'{{node_id="{node}"}}'
        data = self.read(
            "/api/v1/query_range",
            _RangeQuery(
                query=QUERIES[metric].replace("{filter}", selector),
                start=end - duration,
                end=end,
                step=step,
            ).model_dump(),
        )
        if not isinstance(data, _Matrix):
            raise PrometheusUnavailable("Prometheus returned no matrix")
        try:
            series = [
                MetricSeries(
                    labels=row.metric,
                    points=[
                        MetricPoint(
                            timestamp=timestamp,
                            value=value if math.isfinite(value := float(raw)) else None,
                        )
                        for timestamp, raw in row.values
                    ],
                )
                for row in data.result
            ]
        except ValueError as error:
            raise PrometheusUnavailable(
                "Prometheus returned invalid samples"
            ) from error
        return MetricsSeriesResponse(
            metric=metric,
            range=range,
            start=end - duration,
            end=end,
            step_seconds=step,
            series=series,
        )

    def attention(self) -> list[PrometheusAttention]:
        data = self.read("/api/v1/alerts", {})
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
    app: FastAPI, authenticated: Any, reader: PrometheusReader
) -> None:
    _ADMIN_OPERATION_IDS[("get", "/api/metrics/series")] = "getMetricsSeries"

    @app.get(
        "/api/metrics/series",
        response_model=MetricsSeriesResponse,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="getMetricsSeries",
    )
    def metrics_series(
        metric: Metric,
        range: MetricRange,
        node: Annotated[str | None, Query(pattern=r"^spk_[0-9a-f]{32}$")] = None,
        _actor: Actor = authenticated,
    ) -> MetricsSeriesResponse:
        try:
            query = MetricsSeriesRequest(metric=metric, range=range, node=node)
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from error
        return reader.series(query.metric, query.range, query.node)
