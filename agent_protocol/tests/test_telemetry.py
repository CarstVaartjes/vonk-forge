from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from vonk_agent_protocol import (
    AgentProtocolError,
    TelemetryReport,
    canonical_message,
    validate_schema_message,
)

NODE_ID = "spk_0123456789abcdef0123456789abcdef"
BOOT_ID = "00000000-0000-4000-8000-000000000001"


def sample(*, sequence: int, observed_at: datetime) -> dict[str, object]:
    return {
        "boot_id": BOOT_ID,
        "sequence": sequence,
        "observed_at": observed_at.isoformat(),
        "cpu_utilization_percent": 42.0,
        "load_average_1m": 1.25,
        "memory_total_bytes": 128_000_000_000,
        "memory_available_bytes": 64_000_000_000,
        "disk_total_bytes": 1_000_000_000_000,
        "disk_free_bytes": 750_000_000_000,
        "gpu_utilization_percent": 95.0,
        "gpu_memory_total_bytes": 16_000_000_000,
        "gpu_memory_free_bytes": 4_000_000_000,
        "temperature_c": 61.5,
        "power_watts": 185.0,
        "network_receive_bytes_per_second": 1024.0,
        "network_transmit_bytes_per_second": 512.0,
        "gap_samples": 0,
        "details": {
            "accelerator_name": "NVIDIA GB10",
            "accelerator_performance_state": "P0",
        },
        "metrics": {
            "schema_version": 2,
            "series": [
                {
                    "key": "gpu.utilization_percent",
                    "scope": "accelerator",
                    "device_id": "0",
                    "value": 95.0,
                    "unit": "%",
                    "source": "nvidia-smi",
                    "measurement_kind": "measured",
                    "observed_at": observed_at.isoformat(),
                    "freshness": "fresh",
                    "freshness_threshold_seconds": 6.0,
                    "support_status": "available",
                    "aggregation": "last",
                }
            ],
            "capabilities": [
                {
                    "key": "runtime.ttft_p95_ms",
                    "scope": "runtime",
                    "run_id": "run-1",
                    "unit": "ms",
                    "source": "runtime-adapter",
                    "measurement_kind": "derived",
                    "supported": False,
                    "freshness_threshold_seconds": 30.0,
                    "reason": "runtime adapter is not configured",
                }
            ],
            "runtimes": [],
            "workloads": [],
            "provenance": {
                "collector": "vonk-native",
                "collector_version": "2",
                "host_uptime_seconds": 3600,
                "source_observed_at": observed_at.isoformat(),
            },
        },
    }


def report(*, sample_count: int = 1) -> dict[str, object]:
    start = datetime(2026, 9, 5, 12, tzinfo=UTC)
    return {
        "schema_version": 1,
        "samples": [
            sample(
                sequence=index,
                observed_at=start + timedelta(seconds=index),
            )
            for index in range(sample_count)
        ],
    }


def test_rich_report_is_schema_validated_and_canonically_copied() -> None:
    raw = report(sample_count=2)
    parsed = validate_schema_message("telemetry-report.schema.json", raw)

    assert isinstance(parsed, TelemetryReport)
    assert parsed.schema_version == 1
    assert parsed.samples[0]["metrics"]["schema_version"] == 2  # type: ignore[index]
    assert parsed.document() == raw
    assert canonical_message(parsed.document()) == canonical_message(raw)

    raw["samples"][0]["sequence"] = 99  # type: ignore[index]
    assert parsed.samples[0]["sequence"] == 0


def test_report_rejects_duplicate_or_out_of_order_samples() -> None:
    duplicate = report(sample_count=2)
    duplicate["samples"][1]["sequence"] = 0  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="duplicated|ordered"):
        TelemetryReport.parse(duplicate)

    out_of_order = report(sample_count=2)
    out_of_order["samples"][1]["observed_at"] = "2026-09-05T11:59:59+00:00"  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="ordered"):
        TelemetryReport.parse(out_of_order)


def test_report_rejects_unversioned_rich_metrics_and_unknown_fields() -> None:
    bad_version = report()
    bad_version["samples"][0]["metrics"]["schema_version"] = 1  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="schema (validation|is invalid)"):
        TelemetryReport.parse(bad_version)

    unknown = report()
    unknown["samples"][0]["metrics"]["unexpected"] = True  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="schema (validation|is invalid)"):
        TelemetryReport.parse(unknown)


@pytest.mark.parametrize("path", [("samples",), ("samples", 0, "metrics", "series")])
def test_report_rejects_malformed_collection_shapes(path: tuple[object, ...]) -> None:
    malformed = report()
    target: object = malformed
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = "invalid"  # type: ignore[index]

    with pytest.raises(AgentProtocolError, match="schema (validation|is invalid)"):
        TelemetryReport.parse(malformed)
