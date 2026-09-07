"""Linux-only Rust producer to shared Python wire to Controller bridge."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import TelemetryReport
from vonk_control.models import NodeTelemetrySample

from .test_agent_api import NODE_A, agent_headers
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system


@pytest.fixture(scope="session")
def telemetry_wire_probe() -> Path:
    configured = os.environ.get("VONK_TELEMETRY_WIRE_PROBE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        path = path.resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AssertionError(f"configured telemetry wire probe is not executable: {path}")
        return path

    repository = Path(__file__).resolve().parents[2]
    target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
    if not target_root.is_absolute():
        target_root = repository / target_root
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--package",
            "vonk-agent",
            "--example",
            "telemetry_wire_probe",
        ],
        cwd=repository,
        check=True,
    )
    target = target_root / "debug" / "examples" / "telemetry_wire_probe"
    if not target.is_file() or not os.access(target, os.X_OK):
        raise AssertionError(f"cargo did not produce an executable wire probe: {target}")
    return target


def _sample(observed_at: str) -> dict[str, object]:
    return {
        "boot_id": "00000000-0000-4000-8000-000000000001",
        "sequence": 1,
        "observed_at": observed_at,
        "cpu_utilization_percent": 12.5,
        "load_average_1m": 1.25,
        "memory_total_bytes": 128_000_000_000,
        "memory_available_bytes": 64_000_000_000,
        "disk_total_bytes": 1_000_000_000_000,
        "disk_free_bytes": 750_000_000_000,
        "gpu_utilization_percent": 25.0,
        "gpu_memory_total_bytes": 128_000_000_000,
        "gpu_memory_free_bytes": 63_000_000_000,
        "temperature_c": 41.5,
        "power_watts": 17.25,
        "network_receive_bytes_per_second": 1024.5,
        "network_transmit_bytes_per_second": 512.25,
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
                    "value": 25.0,
                    "unit": "%",
                    "source": "nvidia-smi",
                    "measurement_kind": "measured",
                    "observed_at": observed_at,
                    "freshness": "fresh",
                    "freshness_threshold_seconds": 6.0,
                    "support_status": "available",
                    "aggregation": "last",
                }
            ],
            "capabilities": [],
            "runtimes": [],
            "workloads": [],
            "provenance": {
                "collector": "vonk-native",
                "collector_version": "2",
                "host_uptime_seconds": 3600,
                "source_observed_at": observed_at,
            },
        },
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
    report = TelemetryReport.parse(json.loads(produced.stdout))
    assert report.samples[0].metrics.series[0].value == 25.0

    response = client.post(
        "/agent/v1/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=report.document(),
    )
    assert response.status_code == 204
    with services.sessions() as session:
        row = session.scalar(select(NodeTelemetrySample))
        assert row is not None
        assert row.metrics["series"][0]["key"] == "gpu.utilization_percent"
        assert row.metrics["series"][0]["value"] == 25.0
