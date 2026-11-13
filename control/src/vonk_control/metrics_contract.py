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
    "input_token_rate",
    "error_rate",
    "gateway_ttft_p50",
    "gateway_ttft_p95",
    "gateway_latency_p50",
    "gateway_latency_p95",
    "gateway_generation_speed",
    "decode_throughput",
    "prefill_throughput",
    "decode_speed",
    "prefill_speed",
    "ttft_mean",
    "requests_running",
    "requests_waiting",
    "kv_cache_usage",
    "prefix_cache_hit_rate",
    "spec_decode_acceptance",
    "e2e_latency_mean",
    "time_per_token_mean",
    "preemption_rate",
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


def _gateway(
    metric: Metric,
    title: str,
    unit: MetricUnit,
    hint: str,
    *,
    total: bool = False,
    fleet: bool = False,
    basis: Literal["measured", "estimated"] = "estimated",
) -> MetricSpec:
    """A per-model figure from the gateway: estimated (percentiles, derived speed) unless it is a count."""
    return MetricSpec(
        metric=metric,
        title=title,
        unit=unit,
        scope=MetricScope.ALIAS,
        basis=basis,
        source="gateway",
        summary_total=total,
        fleet_sum=fleet,
        hint=hint,
    )


_ENGINE_COVERAGE = (
    "Only engines whose metric names are verified report; others show Not reported."
)
_COUNTED_HINT = "Counted at the gateway from each request's usage; a Spark shows the models it serves."
_TTFT_HINT = "Streaming requests only, seen at the gateway: includes network and queue. Histogram buckets are coarse, so it moves in steps."
_LATENCY_HINT = "Whole request as the gateway saw it, queue included. Histogram buckets are coarse, so it moves in steps."


def _engine(
    metric: Metric,
    title: str,
    unit: MetricUnit,
    hint: str,
    *,
    total: bool = False,
    fleet: bool = False,
) -> MetricSpec:
    """A per-model figure the engine reported through the Spark agent."""
    return MetricSpec(
        metric=metric,
        title=title,
        unit=unit,
        scope=MetricScope.ALIAS,
        basis="measured",
        source="engine",
        summary_total=total,
        fleet_sum=fleet,
        hint=f"{hint} {_ENGINE_COVERAGE}",
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
        _gateway(
            "request_rate",
            "Requests per model",
            "per_second",
            _COUNTED_HINT,
            total=True,
            fleet=True,
            basis="measured",
        ),
        _gateway(
            "output_token_rate",
            "Generated tokens per model",
            "tokens_per_second",
            _COUNTED_HINT,
            total=True,
            fleet=True,
            basis="measured",
        ),
        _gateway(
            "input_token_rate",
            "Prompt tokens per model",
            "tokens_per_second",
            _COUNTED_HINT,
            total=True,
            fleet=True,
            basis="measured",
        ),
        _gateway(
            "error_rate",
            "Failed requests per model",
            "per_second",
            "Counted at the gateway: requests answered with an error, retries included.",
            total=True,
            fleet=True,
            basis="measured",
        ),
        _gateway(
            "gateway_ttft_p50",
            "Time to first token, median",
            "seconds",
            _TTFT_HINT,
        ),
        _gateway(
            "gateway_ttft_p95",
            "Time to first token, 95th percentile",
            "seconds",
            _TTFT_HINT,
        ),
        _gateway(
            "gateway_latency_p50",
            "Request latency, median",
            "seconds",
            _LATENCY_HINT,
        ),
        _gateway(
            "gateway_latency_p95",
            "Request latency, 95th percentile",
            "seconds",
            _LATENCY_HINT,
        ),
        _gateway(
            "gateway_generation_speed",
            "Generation speed per request",
            "tokens_per_second",
            "Output tokens over the time the engine call took, so it includes prompt processing and queueing: it understates the engine's decode speed.",
        ),
        _engine(
            "decode_throughput",
            "Decode throughput",
            "tokens_per_second",
            "Tokens generated per second, all requests together.",
            total=True,
            fleet=True,
        ),
        _engine(
            "prefill_throughput",
            "Prefill throughput",
            "tokens_per_second",
            "Prompt tokens processed per second, all requests together.",
            total=True,
            fleet=True,
        ),
        _engine(
            "decode_speed",
            "Decode speed per request",
            "tokens_per_second",
            "Generated tokens per second of decode time: what one request sees.",
        ),
        _engine(
            "prefill_speed",
            "Prefill speed per request",
            "tokens_per_second",
            "Prompt tokens not served from cache, per second of prefill time.",
        ),
        _engine(
            "ttft_mean",
            "Time to first token, mean",
            "seconds",
            "Mean over finished requests.",
        ),
        _engine(
            "requests_running",
            "Requests running",
            "count",
            "Requests being served now.",
        ),
        _engine(
            "requests_waiting",
            "Requests waiting",
            "count",
            "Requests queued behind the running ones.",
        ),
        _engine(
            "kv_cache_usage",
            "KV cache in use",
            "ratio",
            "Share of the KV cache pool holding live requests.",
        ),
        _engine(
            "prefix_cache_hit_rate",
            "Prefix-cache hit rate",
            "ratio",
            "Share of prompt tokens served from the prefix cache.",
        ),
        _engine(
            "spec_decode_acceptance",
            "Speculative decoding acceptance",
            "ratio",
            "Share of drafted tokens accepted; engines without speculative decoding show Not reported.",
        ),
        _engine(
            "e2e_latency_mean",
            "Request time at the engine, mean",
            "seconds",
            "Mean over finished requests.",
        ),
        _engine(
            "time_per_token_mean",
            "Time per output token, mean",
            "seconds",
            "Mean gap between output tokens (inter-token latency).",
        ),
        _engine(
            "preemption_rate",
            "Preemptions",
            "per_second",
            "Requests the engine paused under KV cache pressure.",
            total=True,
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
