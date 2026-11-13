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
    "gpu_power",
    "gpu_energy",
    "models_memory",
    "system_memory",
    "gpu_clock",
    "swap_used",
    "disk_used",
    "cpu_utilization",
    "load_average",
    "board_temperature",
    "nvme_temperature",
    "nic_temperature",
    "fan_speed",
    "network_lan_rx",
    "network_lan_tx",
    "fabric_rx",
    "fabric_tx",
    "disk_read",
    "disk_write",
    "oom_kills",
]
MetricRange = Literal["1h", "6h", "24h", "7d", "30d"]
MetricUnit = Literal[
    "percent",
    "bytes",
    "celsius",
    "watts",
    "watt_hours",
    "megahertz",
    "rpm",
    "per_second",
    "bytes_per_second",
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
    # True when the response also carries the fleet line: one series without a
    # label that sums the others, with its own summary (and each series' share).
    fleet_sum: bool
    hint: str


def _spark(
    metric: Metric,
    title: str,
    unit: MetricUnit,
    hint: str,
    *,
    total: bool = False,
    fleet: bool = False,
    basis: Literal["measured", "estimated"] = "measured",
) -> MetricSpec:
    """A Spark reading: one series per Spark, measured unless said otherwise."""
    return MetricSpec(
        metric=metric,
        title=title,
        unit=unit,
        scope=MetricScope.NODE,
        basis=basis,
        source="spark",
        summary_total=total,
        fleet_sum=fleet,
        hint=hint,
    )


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
            fleet_sum=False,
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
            fleet_sum=False,
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
            fleet_sum=False,
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
            fleet_sum=False,
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
            fleet_sum=True,
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
            fleet_sum=True,
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
            fleet_sum=False,
            hint="Seconds until the Spark's certificate expires.",
        ),
        _spark(
            "gpu_power",
            "GPU power",
            "watts",
            "GPU draw only; the whole Spark's power is not readable on GB10.",
            fleet=True,
        ),
        _spark(
            "gpu_energy",
            "GPU energy",
            "watt_hours",
            "GPU only. Estimate: GPU power summed over the samples the Spark reported.",
            total=True,
            fleet=True,
            basis="estimated",
        ),
        _spark(
            "models_memory",
            "Models memory",
            "bytes",
            "Memory held by GPU processes: the loaded models.",
            fleet=True,
        ),
        _spark(
            "system_memory",
            "System memory",
            "bytes",
            "Memory in use outside the loaded models.",
            fleet=True,
        ),
        _spark(
            "gpu_clock",
            "GPU clock",
            "megahertz",
            "Graphics clock as the driver reports it.",
        ),
        _spark(
            "swap_used",
            "Swap used",
            "bytes",
            "Read with the Spark's inventory, so it moves every few minutes.",
            fleet=True,
        ),
        _spark(
            "disk_used",
            "Model-store disk used",
            "bytes",
            "Read with the Spark's inventory, so it moves every few minutes.",
            fleet=True,
        ),
        _spark(
            "cpu_utilization",
            "CPU utilisation",
            "percent",
            "Share of CPU time not idle.",
        ),
        _spark("load_average", "Load average", "count", "One-minute load average."),
        _spark(
            "board_temperature",
            "Hottest board sensor",
            "celsius",
            "The hottest of the board's unlabelled sensors; not the CPU.",
        ),
        _spark(
            "nvme_temperature",
            "NVMe temperature",
            "celsius",
            "Composite temperature of the model-store disk.",
        ),
        _spark(
            "nic_temperature",
            "Network adapter temperature",
            "celsius",
            "Hottest ConnectX network adapter.",
        ),
        _spark("fan_speed", "Fan speed", "rpm", "Fastest board fan."),
        _spark(
            "network_lan_rx",
            "LAN received",
            "bytes_per_second",
            "Default-route interface.",
            total=True,
            fleet=True,
        ),
        _spark(
            "network_lan_tx",
            "LAN sent",
            "bytes_per_second",
            "Default-route interface.",
            total=True,
            fleet=True,
        ),
        _spark(
            "fabric_rx",
            "Fabric received",
            "bytes_per_second",
            "RDMA fabric ports, which bypass the network interface counters.",
            total=True,
            fleet=True,
        ),
        _spark(
            "fabric_tx",
            "Fabric sent",
            "bytes_per_second",
            "RDMA fabric ports, which bypass the network interface counters.",
            total=True,
            fleet=True,
        ),
        _spark(
            "disk_read",
            "Disk read",
            "bytes_per_second",
            "The disk that holds the model store.",
            total=True,
            fleet=True,
        ),
        _spark(
            "disk_write",
            "Disk written",
            "bytes_per_second",
            "The disk that holds the model store.",
            total=True,
            fleet=True,
        ),
        _spark(
            "oom_kills",
            "Out-of-memory kills",
            "count",
            "Processes the kernel killed for lack of memory; the total counts the range.",
            total=True,
            fleet=True,
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
    # This series' total over the fleet line's total; None without both.
    share: float | None = Field(ge=0, le=1, allow_inf_nan=False)
    # Share of the range's steps that have a value: how far to trust a total.
    coverage: float = Field(ge=0, le=1, allow_inf_nan=False)


class MetricSeries(StrictJSONModel):
    # One label (`node_id` or `alias`); the fleet line has none.
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
