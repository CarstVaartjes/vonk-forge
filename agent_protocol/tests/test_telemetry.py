from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import (
    AgentProtocolError,
    TelemetryRequest,
    canonical_message,
    schema_validator,
    validate_schema_message,
)
from vonk_agent_protocol.telemetry import (
    MAX_TELEMETRY_SCALAR_INTEGER,
    MAX_TELEMETRY_SCALAR_STRING_CHARS,
    MIN_TELEMETRY_SCALAR_INTEGER,
    TelemetryRuntime,
    validate_telemetry_scalar,
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

    assert isinstance(parsed, TelemetryRequest)
    assert parsed.schema_version == 1
    assert parsed.samples[0].metrics.schema_version == 2
    assert parsed.document()["schema_version"] == 1
    assert len(parsed.document()["samples"]) == 2
    assert canonical_message(parsed.document()) == canonical_message(
        TelemetryRequest.parse(parsed.document()).document()
    )

    raw["samples"][0]["sequence"] = 99  # type: ignore[index]
    assert parsed.samples[0].sequence == 0


def test_telemetry_schema_validator_uses_the_registered_pydantic_model() -> None:
    assert schema_validator("telemetry-report.schema.json").schema == (
        TelemetryRequest.model_json_schema()
    )


def test_packaged_telemetry_schema_is_deterministically_generated() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/generate-telemetry-schema", "--check"],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_report_rejects_duplicate_or_out_of_order_samples() -> None:
    duplicate = report(sample_count=2)
    duplicate["samples"][1]["sequence"] = 0  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="duplicated|ordered"):
        TelemetryRequest.parse(duplicate)

    out_of_order = report(sample_count=2)
    out_of_order["samples"][1]["observed_at"] = "2026-09-05T11:59:59+00:00"  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="ordered"):
        TelemetryRequest.parse(out_of_order)


def test_report_rejects_unversioned_rich_metrics_and_unknown_fields() -> None:
    bad_version = report()
    bad_version["samples"][0]["metrics"]["schema_version"] = 1  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="schema (validation|is invalid)"):
        TelemetryRequest.parse(bad_version)

    unknown = report()
    unknown["samples"][0]["metrics"]["unexpected"] = True  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="schema (validation|is invalid)"):
        TelemetryRequest.parse(unknown)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        MIN_TELEMETRY_SCALAR_INTEGER,
        MAX_TELEMETRY_SCALAR_INTEGER,
        2**53 + 1,
        1.5,
        float("1.7976931348623157e308"),
        "",
        "café\u0000",
    ],
    ids=["null", "bool", "signed64-min", "signed64-max", "large-int", "float", "max-float", "empty-text", "unicode-control-text"],
)
def test_metric_scalar_boundary_accepts_json_scalars(value: object) -> None:
    assert validate_telemetry_scalar(value) == value
    valid = report()
    valid["samples"][0]["metrics"]["series"][0]["value"] = value  # type: ignore[index]
    parsed = TelemetryRequest.parse(valid)
    assert parsed.samples[0].metrics.series[0].value == value


@pytest.mark.parametrize(
    "value",
    [
        MIN_TELEMETRY_SCALAR_INTEGER - 1,
        MAX_TELEMETRY_SCALAR_INTEGER + 1,
        float("inf"),
        float("-inf"),
        float("nan"),
        [],
        {},
        "x" * (MAX_TELEMETRY_SCALAR_STRING_CHARS + 1),
    ],
    ids=["signed64-underflow", "signed64-overflow", "positive-infinity", "negative-infinity", "nan", "array", "object", "overlong-text"],
)
def test_metric_scalar_boundary_rejects_non_scalars_and_out_of_range_values(value: object) -> None:
    with pytest.raises(ValueError):
        validate_telemetry_scalar(value)
    invalid = report()
    invalid["samples"][0]["metrics"]["series"][0]["value"] = value  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="schema (validation|is invalid)|scalar"):
        TelemetryRequest.parse(invalid)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("samples", 0, "metrics", "series", 0, "key"), "GPU.utilization"),
        (("samples", 0, "metrics", "series", 0, "source"), "nvidia smi"),
        (("samples", 0, "metrics", "series", 0, "aggregation"), "Last"),
        (("samples", 0, "metrics", "provenance", "collector"), "vonk native"),
        (("samples", 0, "metrics", "provenance", "collector_version"), "2\n3"),
    ],
)
def test_schema_exposes_canonical_telemetry_identifier_patterns(
    path: tuple[object, ...], value: str
) -> None:
    invalid = report()
    target: object = invalid
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    assert list(schema_validator("telemetry-report.schema.json").iter_errors(invalid))
    with pytest.raises(AgentProtocolError, match="schema (validation|is invalid)"):
        TelemetryRequest.parse(invalid)


@pytest.mark.parametrize("value", [None, True, 2.0], ids=["missing", "bool", "float"])
def test_metrics_schema_version_requires_exact_integer_two(value: object) -> None:
    invalid = report()
    if value is None:
        del invalid["samples"][0]["metrics"]["schema_version"]  # type: ignore[index]
    else:
        invalid["samples"][0]["metrics"]["schema_version"] = value  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="schema (validation|is invalid)"):
        TelemetryRequest.parse(invalid)


@pytest.mark.parametrize("path", [("samples",), ("samples", 0, "metrics", "series")])
def test_report_rejects_malformed_collection_shapes(path: tuple[object, ...]) -> None:
    malformed = report()
    target: object = malformed
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = "invalid"  # type: ignore[index]

    with pytest.raises(AgentProtocolError, match="schema (validation|is invalid)"):
        TelemetryRequest.parse(malformed)


@pytest.mark.parametrize("rank", [-1, 2**32, True])
def test_runtime_rank_rejects_values_outside_native_placement_identity(rank: int) -> None:

    value = {
        "run_id": "run-1", "engine_id": "run-1", "backend": "future-engine",
        "serving_node_ids": [], "ranks": [rank], "readiness": "unknown",
        "adapter": "future-engine", "adapter_supported": False,
        "adapter_reason": "No supported metrics contract",
    }
    with pytest.raises(ValidationError):
        TelemetryRuntime.model_validate(value)


def test_runtime_rank_preserves_native_placement_upper_bound() -> None:

    value = TelemetryRuntime.model_validate({
        "run_id": "run-1", "engine_id": "run-1", "backend": "future-engine",
        "serving_node_ids": [], "ranks": [0, 2**32 - 1], "readiness": "unknown",
        "adapter": "future-engine", "adapter_supported": False,
        "adapter_reason": "No supported metrics contract",
    })
    assert value.ranks == [0, 2**32 - 1]
