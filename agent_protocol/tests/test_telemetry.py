from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from vonk_agent_protocol import (
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
