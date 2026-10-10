"""Shared operator contracts for bounded Prometheus reads."""

from typing import Literal, Self

from pydantic import Field, model_validator

from .strict_json import StrictJSONModel

Metric = Literal[
    "gpu_utilization",
    "gpu_memory_used",
    "host_memory_used",
    "gpu_temperature",
    "request_rate",
    "certificate_expiry",
]
MetricRange = Literal["1h", "6h", "24h", "7d"]


class MetricsSeriesRequest(StrictJSONModel):
    metric: Metric
    range: MetricRange
    node: str | None = Field(default=None, pattern=r"^spk_[0-9a-f]{32}$")

    @model_validator(mode="after")
    def _scope(self) -> Self:
        if self.node is not None and self.metric == "request_rate":
            raise ValueError("Request rates are per model, not per Spark")
        return self


class MetricPoint(StrictJSONModel):
    timestamp: float = Field(allow_inf_nan=False)
    value: float | None = Field(allow_inf_nan=False)


class MetricSeries(StrictJSONModel):
    labels: dict[str, str]
    points: list[MetricPoint]


class MetricsSeriesResponse(StrictJSONModel):
    metric: Metric
    range: MetricRange
    start: float
    end: float
    step_seconds: int
    series: list[MetricSeries]


class PrometheusAttention(StrictJSONModel):
    source: Literal["prometheus"] = "prometheus"
    name: str
    summary: str
    severity: str
    labels: dict[str, str]
    active_at: str
