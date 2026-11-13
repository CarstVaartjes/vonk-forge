"""Prometheus text for the host and engine readings of telemetry protocol 4.4.

One table row per reading names its wire field, its series and its kind. A
reading the agent did not report has no line, so an older agent (or an engine
that does not export it) leaves a gap, never a zero. Counters are exported as
they arrived; the rate is computed once, in Prometheus.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from .fleet_projection import FleetSnapshot, TelemetryPoint
    from .telemetry import LiveEngineReading


class Reading(NamedTuple):
    field: str
    series: str
    kind: str  # "gauge" or "counter"
    help: str


_HOST = (
    Reading("gpu_power_watts", "vonk_node_telemetry_gpu_power_watts", "gauge", "GPU power draw in watts (not the whole Spark)"),
    Reading("gpu_process_memory_bytes", "vonk_node_telemetry_gpu_process_memory_bytes", "gauge", "Memory held by GPU processes in bytes"),
    Reading("system_memory_bytes", "vonk_node_telemetry_system_memory_bytes", "gauge", "Memory in use outside GPU processes in bytes"),
    Reading("cpu_busy_seconds", "vonk_node_cpu_busy_seconds_total", "counter", "Cumulative CPU time spent not idle, in seconds"),
    Reading("cpu_total_seconds", "vonk_node_cpu_seconds_total", "counter", "Cumulative CPU time in seconds, idle included"),
    Reading("load_average_1m", "vonk_node_load_average_1m", "gauge", "One-minute load average"),
    Reading("network_lan_rx_bytes", "vonk_node_network_lan_rx_bytes_total", "counter", "Bytes received on the default-route interface"),
    Reading("network_lan_tx_bytes", "vonk_node_network_lan_tx_bytes_total", "counter", "Bytes sent on the default-route interface"),
    Reading("fabric_rx_bytes", "vonk_node_fabric_rx_bytes_total", "counter", "Bytes received on the RDMA fabric ports"),
    Reading("fabric_tx_bytes", "vonk_node_fabric_tx_bytes_total", "counter", "Bytes sent on the RDMA fabric ports"),
    Reading("fabric_ports_active", "vonk_node_fabric_ports_active", "gauge", "RDMA fabric ports in the ACTIVE state"),
    Reading("disk_read_bytes", "vonk_node_disk_read_bytes_total", "counter", "Bytes read from the model-store disk"),
    Reading("disk_written_bytes", "vonk_node_disk_written_bytes_total", "counter", "Bytes written to the model-store disk"),
    Reading("oom_kills", "vonk_node_oom_kills_total", "counter", "Processes killed by the kernel out-of-memory killer since boot"),
    Reading("board_temperature_max_c", "vonk_node_board_temperature_c", "gauge", "Hottest unlabelled board sensor in Celsius"),
    Reading("nvme_temperature_c", "vonk_node_nvme_temperature_c", "gauge", "NVMe composite temperature in Celsius"),
    Reading("nic_temperature_c", "vonk_node_nic_temperature_c", "gauge", "Hottest network adapter ASIC in Celsius"),
    Reading("fan_rpm_max", "vonk_node_fan_rpm", "gauge", "Fastest board fan in revolutions per minute"),
)  # fmt: skip

_ENGINE = (
    Reading("prompt_tokens", "vonk_engine_prompt_tokens_total", "counter", "Prompt tokens of finished requests"),
    Reading("generation_tokens", "vonk_engine_generation_tokens_total", "counter", "Generated tokens of finished requests"),
    Reading("cached_prompt_tokens", "vonk_engine_cached_prompt_tokens_total", "counter", "Prompt tokens served from the prefix cache"),
    Reading("preemptions", "vonk_engine_preemptions_total", "counter", "Requests preempted under KV cache pressure"),
    Reading("spec_decode_draft_tokens", "vonk_engine_spec_decode_draft_total", "counter", "Speculative draft tokens proposed"),
    Reading("spec_decode_accepted_tokens", "vonk_engine_spec_decode_accepted_total", "counter", "Speculative draft tokens accepted"),
    Reading("requests_running", "vonk_engine_requests_running", "gauge", "Requests being served"),
    Reading("requests_waiting", "vonk_engine_requests_waiting", "gauge", "Requests queued"),
    Reading("kv_cache_usage_ratio", "vonk_engine_kv_cache_usage_ratio", "gauge", "Share of the KV cache in use"),
    *(
        Reading(f"{name}_{part}", f"vonk_engine_{short}_{part}", "counter", f"{what} ({part})")
        for name, short, what in (
            ("ttft_seconds", "ttft_seconds", "Seconds to first token of finished requests"),
            ("time_per_output_token_seconds", "tpot_seconds", "Seconds per output token of finished requests"),
            ("prefill_seconds", "prefill_seconds", "Prefill seconds of finished requests"),
            ("decode_seconds", "decode_seconds", "Decode seconds of finished requests"),
            ("e2e_seconds", "e2e_seconds", "End-to-end seconds of finished requests"),
        )
        for part in ("sum", "count")
    ),
)  # fmt: skip


class NodeFacts(NamedTuple):
    """Gauges that ride on a Spark's clock observation and inventory, not a sample."""

    gpu_graphics_clock_mhz: int | None
    swap_used_bytes: int | None
    disk_used_bytes: int | None


_FACTS = (
    Reading("gpu_graphics_clock_mhz", "vonk_node_telemetry_gpu_graphics_clock_mhz", "gauge", "GPU graphics clock in MHz"),
    Reading("swap_used_bytes", "vonk_node_inventory_swap_used_bytes", "gauge", "Swap in use in bytes"),
    Reading("disk_used_bytes", "vonk_node_inventory_disk_used_bytes", "gauge", "Model-store disk in use in bytes"),
)  # fmt: skip


def node_facts(snapshot: FleetSnapshot) -> dict[str, NodeFacts]:
    facts: dict[str, NodeFacts] = {}
    for node in snapshot.nodes:
        clock = None if node.telemetry is None else node.telemetry.sample.gpu_clock
        inventory = node.inventory
        swap = (
            None
            if inventory is None or inventory.host_settings is None
            else inventory.host_settings.swap
        )
        facts[node.id] = NodeFacts(
            None if clock is None else clock.graphics_mhz,
            None if swap is None else max(0, swap.total_bytes - swap.free_bytes),
            None
            if inventory is None
            else max(0, inventory.disk_total_bytes - inventory.disk_free_bytes),
        )
    return facts


def _number(value: float) -> str:
    return repr(value) if isinstance(value, float) else str(value)


def _lines(table: Sequence[Reading], rows: Sequence[tuple[str, object]]) -> list[str]:
    """``rows`` are (label set, object carrying the wire fields); one family per
    reading, and a reading nobody has produces no family."""

    lines: list[str] = []
    for reading in table:
        samples = [
            f"{reading.series}{{{labels}}} {_number(value)}"
            for labels, source in rows
            if (value := getattr(source, reading.field, None)) is not None
        ]
        if samples:
            lines.extend(
                (
                    f"# HELP {reading.series} {reading.help}.",
                    f"# TYPE {reading.series} {reading.kind}",
                    *samples,
                )
            )
    return lines


def host_reading_lines(samples: Mapping[str, TelemetryPoint]) -> list[str]:
    return _lines(
        _HOST,
        [(f'node_id="{node}"', sample) for node, sample in sorted(samples.items())],
    )


def node_fact_lines(facts: Mapping[str, NodeFacts]) -> list[str]:
    return _lines(
        _FACTS, [(f'node_id="{node}"', fact) for node, fact in sorted(facts.items())]
    )


def engine_reading_lines(readings: Sequence[LiveEngineReading]) -> list[str]:
    """Labels are the node and the run's alias (a validated name, so it needs
    no escaping). A route name is published by one run, so a (node, alias) pair
    is one run; if a switch ever shows two, one reading stands for the pair and
    the series is never repeated."""

    by_series = {(item.node_id, item.alias): item.reading for item in readings}
    return _lines(
        _ENGINE,
        [
            (f'node_id="{node}",alias="{alias}"', reading)
            for (node, alias), reading in sorted(by_series.items())
        ],
    )
