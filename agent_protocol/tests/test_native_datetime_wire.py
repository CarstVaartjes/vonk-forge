"""Native datetime canonical bytes across the actual Python and Rust models."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from vonk_agent_protocol import AgentClaim, DistributionAssignment, canonical_message
from vonk_agent_protocol.inventory import InventoryRequest
from vonk_agent_protocol.recipe_observations import RecipeRunObservationsWire
from vonk_agent_protocol.telemetry import TelemetrySeries

ROOT = Path(__file__).parents[2]


def probe(model: str, document: bytes) -> bytes:
    executable = os.environ.get("VONK_CANONICAL_WIRE_PROBE")
    if not executable:
        pytest.skip(
            "VONK_CANONICAL_WIRE_PROBE is required for the connected Rust boundary"
        )
    return subprocess.run(
        [executable, model], input=document, capture_output=True, check=True
    ).stdout


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-08T15:00:00Z",
        "2026-09-08T15:00:00+00:00",
        "2026-09-08T15:00:00.000000Z",
        "2026-09-08T15:00:00.1Z",
        "2026-09-08T15:00:00.120000Z",
        "2026-09-08T15:00:00.123000Z",
        "2026-09-08T15:00:00.123456Z",
        "2026-09-08T15:00:00.123456789Z",
    ],
)
def test_native_claim_deadlines_have_identical_canonical_bytes(timestamp: str) -> None:
    document = json.loads(
        (
            ROOT
            / "agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
        ).read_text()
    )
    document["deadline"] = timestamp
    value = AgentClaim.model_validate(document)
    expected = canonical_message(value)
    assert probe("AgentClaim", canonical_message(document)) == expected
    assert probe("AgentClaim", expected) == expected


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-08T15:00:00.123000+05:30",
        "2026-09-08T15:00:00.120000-03:30",
        "2026-09-08T15:00:00+02:00",
    ],
)
def test_inventory_native_datetimes_preserve_declared_offset(timestamp: str) -> None:
    document = {
        "schema_version": 1,
        "observed_at": timestamp,
        "disk_total_bytes": 1,
        "disk_free_bytes": 0,
        "host_memory_total_bytes": 1,
        "host_memory_free_bytes": 0,
        "gpu_memory_total_bytes": 1,
        "gpu_memory_free_bytes": 0,
        "gpu_count": 0,
        "artifact_store_read_only": False,
        "capabilities": [],
        "nvidia_driver_version": "unavailable",
        "container_runtime_version": "podman",
    }
    expected = canonical_message(InventoryRequest.model_validate(document))
    assert json.loads(expected)["observed_at"] == timestamp
    assert probe("InventoryRequest", canonical_message(document)) == expected


def test_optional_native_timestamps_keep_presence_policy_and_microseconds() -> None:
    document = json.loads(
        (ROOT / "agent_protocol/fixtures/typify-telemetry-series.json").read_text()
    )
    for received_at in [None, "2026-09-08T15:00:00.123000Z"]:
        document["received_at"] = received_at
        value = TelemetrySeries.model_validate(document)
        expected = canonical_message(value)
        assert probe("TelemetrySeries", canonical_message(document)) == expected
        assert ("received_at" in json.loads(expected)) == (received_at is not None)


def test_actual_snapshot_producer_uses_the_same_native_timestamp_bytes() -> None:
    value = RecipeRunObservationsWire.model_validate(
        {
            "schema_version": 2,
            "observed_at": "2026-09-08T15:00:00.123000+02:00",
            "runs": [],
        }
    )
    expected = canonical_message(value)
    assert probe("RecipeRunObservationsWire", expected) == expected


def test_actual_distribution_producer_uses_the_same_native_timestamp_bytes() -> None:
    value = DistributionAssignment.parse(
        {
            "schema_version": 2,
            "assignment_id": "10000000-0000-4000-8000-000000000001",
            "plan_digest": "a" * 64,
            "generation": 1,
            "node_id": "spk_" + "b" * 32,
            "expires_at": "2026-09-08T15:00:00.123000+00:00",
            "model_artifact_set_sha256": "c" * 64,
            "objects": [
                {"name": "model.bin", "sha256": "d" * 64, "bytes": 1, "kind": "model"},
                {
                    "name": "image.tar",
                    "sha256": "e" * 64,
                    "bytes": 1,
                    "kind": "oci-archive",
                },
            ],
            "oci_image_digest": "sha256:" + "f" * 64,
            "oci_archive_sha256": "e" * 64,
        }
    )
    expected = canonical_message(value)
    assert probe("DistributionAssignment", expected) == expected
