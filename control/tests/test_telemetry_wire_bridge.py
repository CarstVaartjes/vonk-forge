"""Linux-only Rust producer to shared Python wire to Controller bridge."""

from __future__ import annotations

import json
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import TelemetryRequest
from vonk_control.models import NodeEngineSample, NodeTelemetrySample

from tests.wire_probes import prebuilt_probe

from .test_agent_api import NODE_A, agent_headers
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system


@pytest.fixture(scope="session")
def telemetry_wire_probe() -> Path:
    return prebuilt_probe("VONK_TELEMETRY_WIRE_PROBE")


def _sample(observed_at: str) -> dict[str, object]:
    vector = json.loads(
        (
            files("vonk_agent_protocol") / "vectors" / "telemetry-sample-v2.json"
        ).read_text()
    )
    return {**vector["sample"], "observed_at": observed_at}


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
    assert report.samples[0].gpu_power_watts == 9.14
    assert report.samples[0].gpu_process_memory_bytes == 113_668_784_128
    assert report.document()["samples"][0]["engines"] == sample["engines"]

    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=report.document(),
    )
    assert response.status_code == 204
    with services.sessions() as session:
        row = session.scalar(select(NodeTelemetrySample))
        assert row is not None
        assert (row.gpu_power_watts, row.uptime_seconds) == (9.14, 1_804_535)
        assert row.gpu_process_memory_bytes == 113_668_784_128
        assert row.fan_rpm_max == sample["fan_rpm_max"]
        assert len(session.scalars(select(NodeEngineSample)).all()) == 2
