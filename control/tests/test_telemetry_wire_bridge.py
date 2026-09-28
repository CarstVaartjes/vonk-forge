"""Linux-only Rust producer to shared Python wire to Controller bridge."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import TelemetryRequest
from vonk_control.models import NodeTelemetrySample

from tests.wire_probes import prebuilt_probe

from .test_agent_api import NODE_A, agent_headers
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system


@pytest.fixture(scope="session")
def telemetry_wire_probe() -> Path:
    return prebuilt_probe("VONK_TELEMETRY_WIRE_PROBE")


def _sample(observed_at: str) -> dict[str, object]:
    return {
        "boot_id": "00000000-0000-4000-8000-000000000001",
        "observed_at": observed_at,
        "memory_total_bytes": 128_000_000_000,
        "memory_available_bytes": 64_000_000_000,
        "disk_total_bytes": 1_000_000_000_000,
        "disk_free_bytes": 750_000_000_000,
        "gpu_utilization_percent": 25.0,
        "gpu_memory_total_bytes": 128_000_000_000,
        "gpu_memory_free_bytes": 63_000_000_000,
    }


def test_rust_telemetry_json_crosses_shared_python_and_controller_ack(
    agent_system, telemetry_wire_probe: Path
) -> None:
    client, services, _, clock = agent_system
    sample = _sample(clock.now.isoformat())
    produced = subprocess.run(
        [str(telemetry_wire_probe)],
        input=json.dumps(sample, separators=(",", ":")),
        text=True,
        capture_output=True,
        check=True,
    )
    report = TelemetryRequest.parse(json.loads(produced.stdout))
    assert report.samples[0].gpu_utilization_percent == 25.0

    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=report.document(),
    )
    assert response.status_code == 204
    with services.sessions() as session:
        row = session.scalar(select(NodeTelemetrySample))
        assert row is not None
        assert row.gpu_utilization_percent == 25.0
