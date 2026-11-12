from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    AgentEvidenceCode,
    AgentProtocolError,
    TelemetryRequest,
    canonical_message,
)

BOOT_ID = "00000000-0000-4000-8000-000000000001"


def sample(*, observed_at: datetime) -> dict[str, object]:
    return {
        "boot_id": BOOT_ID,
        "observed_at": observed_at.isoformat(),
        "memory_total_bytes": 128_000_000_000,
        "memory_available_bytes": 64_000_000_000,
        "disk_total_bytes": 1_000_000_000_000,
        "disk_free_bytes": 750_000_000_000,
        "gpu_utilization_percent": 95.0,
        "gpu_memory_total_bytes": 16_000_000_000,
        "gpu_memory_free_bytes": 4_000_000_000,
    }


def report(*, sample_count: int = 1) -> dict[str, object]:
    start = datetime(2026, 9, 5, 12, tzinfo=UTC)
    return {
        "samples": [
            sample(observed_at=start + timedelta(seconds=index))
            for index in range(sample_count)
        ],
    }


def test_report_parses_and_canonically_round_trips() -> None:
    parsed = TelemetryRequest.parse(report(sample_count=2))

    assert len(parsed.document()["samples"]) == 2
    assert parsed.samples[0].memory_available_bytes == 64_000_000_000
    assert canonical_message(parsed.document()) == canonical_message(
        TelemetryRequest.parse(parsed.document()).document()
    )


def test_report_rejects_duplicate_or_out_of_order_samples() -> None:
    duplicate = report(sample_count=2)
    duplicate["samples"][1]["observed_at"] = duplicate["samples"][0]["observed_at"]  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="ordered"):
        TelemetryRequest.parse(duplicate)

    out_of_order = report(sample_count=2)
    out_of_order["samples"][1]["observed_at"] = "2026-09-05T11:59:59+00:00"  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="ordered"):
        TelemetryRequest.parse(out_of_order)


@pytest.mark.parametrize(
    "changes",
    [
        {"unexpected": True},
        {"memory_available_bytes": 200_000_000_000},
        {"disk_free_bytes": None},
        {"boot_id": "00000000-0000-0000-0000-000000000000"},
        {"observed_at": "2026-09-05T12:00:00"},
    ],
)
def test_sample_rejects_unknown_or_inconsistent_values(
    changes: dict[str, object],
) -> None:
    invalid = report()
    invalid["samples"][0].update(changes)  # type: ignore[index]
    with pytest.raises(AgentProtocolError, match="schema is invalid"):
        TelemetryRequest.parse(invalid)


def test_cpu_frequency_and_temperature_are_optional_and_bounded() -> None:
    older = TelemetryRequest.parse(report())
    assert "cpu_frequency_avg_mhz" not in older.document()["samples"][0]

    newer = report()
    newer["samples"][0].update(  # type: ignore[attr-defined]
        {
            "cpu_frequency_avg_mhz": 2100,
            "cpu_frequency_min_mhz": 1800,
            "cpu_frequency_max_mhz": 3900,
            "gpu_temperature_c": 84,
        }
    )
    parsed = TelemetryRequest.parse(newer)
    assert parsed.samples[0].cpu_frequency_max_mhz == 3900

    inverted = report()
    inverted["samples"][0].update(  # type: ignore[attr-defined]
        {"cpu_frequency_avg_mhz": 1000, "cpu_frequency_min_mhz": 2000}
    )
    # An inconsistent optional reading is dropped and named, not refused.
    parsed = TelemetryRequest.parse(inverted)
    assert parsed.samples[0].cpu_frequency_avg_mhz is None
    assert (
        AgentEvidenceCode.TELEMETRY_READING_DROPPED
        in parsed.samples[0].evidence_warnings
    )


VECTOR = json.loads(
    (
        Path(__file__).parents[1]
        / "src/vonk_agent_protocol/vectors/telemetry-sample-v1.json"
    ).read_text()
)
NEW_READINGS = ("gpu_power_watts", "uptime_seconds", "gpu_process_memory_bytes")


def test_power_uptime_and_process_memory_round_trip_and_older_agents_omit_them() -> (
    None
):
    parsed = TelemetryRequest.parse({"samples": [VECTOR["sample"]]})
    document = parsed.document()["samples"][0]
    assert {k: document[k] for k in NEW_READINGS} == {
        k: VECTOR["sample"][k] for k in NEW_READINGS
    }

    older = {k: v for k, v in VECTOR["sample"].items() if k not in NEW_READINGS}
    omitted = TelemetryRequest.parse({"samples": [older]}).document()["samples"][0]
    assert not set(NEW_READINGS) & set(omitted)


@pytest.mark.parametrize("invalid", VECTOR["invalid_readings"])
def test_an_invalid_new_reading_is_dropped_alone(invalid: dict[str, int]) -> None:
    ((name, _),) = invalid.items()
    parsed = TelemetryRequest.parse({"samples": [{**VECTOR["sample"], **invalid}]})
    kept = parsed.document()["samples"][0]
    assert name not in kept
    assert {k: kept[k] for k in NEW_READINGS if k != name} == {
        k: VECTOR["sample"][k] for k in NEW_READINGS if k != name
    }
    assert kept["memory_total_bytes"] == VECTOR["sample"]["memory_total_bytes"]
    assert (
        AgentEvidenceCode.TELEMETRY_READING_DROPPED
        in parsed.samples[0].evidence_warnings
    )


VECTOR_V2 = json.loads(
    (
        Path(__file__).parents[1]
        / "src/vonk_agent_protocol/vectors/telemetry-sample-v2.json"
    ).read_text()
)
HOST_READINGS_V2 = tuple(
    name for name in VECTOR_V2["sample"] if name not in VECTOR["sample"]
)


def test_host_and_engine_readings_round_trip_and_older_agents_omit_them() -> None:
    sample = VECTOR_V2["sample"]
    parsed = TelemetryRequest.parse({"samples": [sample]})
    document = parsed.document()["samples"][0]
    assert {k: document[k] for k in sample} == sample
    assert not parsed.samples[0].evidence_warnings

    older = dict(VECTOR["sample"])
    omitted = TelemetryRequest.parse({"samples": [older]}).document()["samples"][0]
    assert not set(HOST_READINGS_V2) & set(omitted)


@pytest.mark.parametrize("invalid", VECTOR_V2["invalid_readings"])
def test_an_invalid_host_reading_is_dropped_alone(invalid: dict[str, float]) -> None:
    ((name, _),) = invalid.items()
    parsed = TelemetryRequest.parse({"samples": [{**VECTOR_V2["sample"], **invalid}]})
    kept = parsed.document()["samples"][0]
    assert name not in kept
    assert kept["memory_total_bytes"] == VECTOR_V2["sample"]["memory_total_bytes"]
    assert kept["engines"] == VECTOR_V2["sample"]["engines"]
    assert (
        AgentEvidenceCode.TELEMETRY_READING_DROPPED
        in parsed.samples[0].evidence_warnings
    )


@pytest.mark.parametrize("invalid", VECTOR_V2["invalid_engine_entries"])
def test_an_invalid_engine_entry_is_dropped_alone(invalid: dict[str, str]) -> None:
    engines = [
        invalid,
        *VECTOR_V2["sample"]["engines"],
        VECTOR_V2["sample"]["engines"][0],
    ]
    parsed = TelemetryRequest.parse(
        {"samples": [{**VECTOR_V2["sample"], "engines": engines}]}
    )
    assert parsed.document()["samples"][0]["engines"] == VECTOR_V2["sample"]["engines"]


@pytest.mark.parametrize("invalid", VECTOR_V2["invalid_engine_readings"])
def test_an_invalid_engine_reading_is_dropped_alone(invalid: dict[str, float]) -> None:
    first, second = VECTOR_V2["sample"]["engines"]
    parsed = TelemetryRequest.parse(
        {
            "samples": [
                {**VECTOR_V2["sample"], "engines": [{**first, **invalid}, second]}
            ]
        }
    )
    kept_first, kept_second = parsed.document()["samples"][0]["engines"]
    assert kept_second == second
    assert not set(invalid) & set(kept_first)
    assert kept_first["generation_tokens"] == first["generation_tokens"]


def test_many_invalid_readings_at_once_are_dropped_without_a_search() -> None:
    """A subset search over the groups takes seconds for eight bad readings."""

    invalid = {k: v for item in VECTOR_V2["invalid_readings"] for k, v in item.items()}
    started = time.monotonic()
    parsed = TelemetryRequest.parse({"samples": [{**VECTOR_V2["sample"], **invalid}]})
    assert time.monotonic() - started < 1
    assert not set(invalid) & set(parsed.document()["samples"][0])
