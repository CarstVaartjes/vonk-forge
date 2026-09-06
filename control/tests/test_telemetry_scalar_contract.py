from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import AgentProtocolError, TelemetryReport
from vonk_control.telemetry_contract import TelemetrySeries


OBSERVED_AT = "2026-09-05T12:00:00+00:00"


def _series(value: object) -> dict[str, object]:
    return {
        "key": "runtime.test_value",
        "scope": "runtime",
        "run_id": "run-1",
        "value": value,
        "unit": "value",
        "source": "test-producer",
        "measurement_kind": "measured",
        "observed_at": OBSERVED_AT,
        "freshness": "fresh",
        "freshness_threshold_seconds": 6.0,
        "support_status": "available",
        "aggregation": "last",
    }


def _report(value: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "samples": [
            {
                "boot_id": "00000000-0000-4000-8000-000000000001",
                "sequence": 0,
                "observed_at": OBSERVED_AT,
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
                    "series": [_series(value)],
                    "capabilities": [],
                    "runtimes": [],
                    "workloads": [],
                    "provenance": {
                        "collector": "test-producer",
                        "collector_version": "1",
                    },
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "value",
    [-(2**63), 1.7976931348623157e308, "", "測\x00定"],
)
def test_scalar_producer_survives_report_parse_and_controller_validation(
    value: object,
) -> None:
    parsed = TelemetryReport.parse(_report(value))
    parsed_series = parsed.samples[0]["metrics"]["series"][0]  # type: ignore[index]

    validated = TelemetrySeries.model_validate(parsed_series)

    assert validated.value == value


@pytest.mark.parametrize(
    "value",
    [[], {}, float("nan"), float("inf"), 2**63, "x" * 257],
)
def test_scalar_producer_rejections_match_report_and_controller(
    value: object,
) -> None:
    with pytest.raises(AgentProtocolError):
        TelemetryReport.parse(_report(value))
    with pytest.raises(ValidationError):
        TelemetrySeries.model_validate(_series(value))


def test_controller_preserves_scalar_unicode_and_empty_string() -> None:
    for value in ("", "é\x00"):
        validated = TelemetrySeries.model_validate(
            _series(value) | {"observed_at": datetime.fromisoformat(OBSERVED_AT)}
        )
        assert validated.value == value
        assert validated.observed_at.tzinfo == UTC
