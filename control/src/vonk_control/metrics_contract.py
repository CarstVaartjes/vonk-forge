"""Shared operator contracts for bounded Prometheus reads."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from .strict_json import StrictJSONModel

Metric = Literal[
    "gpu_utilization",
    "gpu_memory_used",
    "host_memory_used",
    "gpu_temperature",
    "request_rate",
    "output_token_rate",
    "certificate_expiry",
]
MetricRange = Literal["1h", "6h", "24h", "7d", "30d"]
MetricUnit = Literal[
    "percent",
    "bytes",
    "celsius",
    "watts",
    "watt_hours",
    "rpm",
    "per_second",
    "tokens_per_second",
    "seconds",
    "ratio",
    "count",
]


class MetricScope(StrEnum):
    """Which label a metric's series carry: `node_id` or `alias`."""

    NODE = "node"
    ALIAS = "alias"


class SeriesLabel(StrEnum):
    """The one label each series carries, by its metric's scope."""

    NODE_ID = "node_id"
    ALIAS = "alias"


SERIES_LABEL = {
    MetricScope.NODE: SeriesLabel.NODE_ID,
    MetricScope.ALIAS: SeriesLabel.ALIAS,
}


class MetricSpec(StrictJSONModel):
    """What one metric is. The page renders it and hard-codes none of it; vonkctl will in M7."""

    metric: Metric
    title: str
    unit: MetricUnit
    scope: MetricScope
    # Gateway counters are estimates: the gateway counts what it forwarded.
    basis: Literal["measured", "estimated"]
    source: Literal["spark", "engine", "gateway"]
    # True when each series' summary carries a range total.
    summary_total: bool
    hint: str


# The one table of what each metric means; the queries live in prometheus_api.
METRIC_SPECS: dict[Metric, MetricSpec] = {
    spec.metric: spec
    for spec in (
        MetricSpec(
            metric="gpu_utilization",
            title="GPU utilisation",
            unit="percent",
            scope=MetricScope.NODE,
            basis="measured",
            source="spark",
            summary_total=False,
            hint="Share of time the GPU was busy.",
        ),
        MetricSpec(
            metric="gpu_memory_used",
            title="GPU memory used",
            unit="bytes",
            scope=MetricScope.NODE,
            basis="measured",
            source="spark",
            summary_total=False,
            hint="Always empty on GB10: the GPU shares unified memory.",
        ),
        MetricSpec(
            metric="host_memory_used",
            title="Unified memory used",
            unit="bytes",
            scope=MetricScope.NODE,
            basis="measured",
            source="spark",
            summary_total=False,
            hint="Total minus available memory on the Spark.",
        ),
        MetricSpec(
            metric="gpu_temperature",
            title="GPU temperature",
            unit="celsius",
            scope=MetricScope.NODE,
            basis="measured",
            source="spark",
            summary_total=False,
            hint="GPU die temperature.",
        ),
        MetricSpec(
            metric="request_rate",
            title="Requests per model",
            unit="per_second",
            scope=MetricScope.ALIAS,
            basis="estimated",
            source="gateway",
            summary_total=True,
            hint="Counted by the gateway; a Spark shows the models it serves.",
        ),
        MetricSpec(
            metric="output_token_rate",
            title="Generated tokens per model",
            unit="tokens_per_second",
            scope=MetricScope.ALIAS,
            basis="estimated",
            source="gateway",
            summary_total=True,
            hint="Counted by the gateway; a Spark shows the models it serves.",
        ),
        MetricSpec(
            metric="certificate_expiry",
            title="Certificate expiry",
            unit="seconds",
            scope=MetricScope.NODE,
            basis="measured",
            source="spark",
            summary_total=False,
            hint="Seconds until the Spark's certificate expires.",
        ),
    )
}


class MetricsSeriesRequest(StrictJSONModel):
    metric: Metric
    range: MetricRange
    node: str | None = Field(default=None, pattern=r"^spk_[0-9a-f]{32}$")


class MetricPoint(StrictJSONModel):
    timestamp: float = Field(allow_inf_nan=False)
    value: float | None = Field(allow_inf_nan=False)


class SeriesSummary(StrictJSONModel):
    """Every number the page shows beside a chart; `None` means no data in the range."""

    latest: float | None = Field(allow_inf_nan=False)
    average: float | None = Field(allow_inf_nan=False)
    peak: float | None = Field(allow_inf_nan=False)
    total: float | None = Field(allow_inf_nan=False)
    # Share of the range's steps that have a value: how far to trust a total.
    coverage: float = Field(ge=0, le=1, allow_inf_nan=False)


class MetricSeries(StrictJSONModel):
    # Exactly one label: `node_id` or `alias`.
    labels: dict[str, str]
    points: list[MetricPoint]
    summary: SeriesSummary


class MetricsSeriesResponse(StrictJSONModel):
    metric: Metric
    spec: MetricSpec
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
