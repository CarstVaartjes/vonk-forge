from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.agent_api import InventoryRequest
from vonk_control.models import NodeInventorySnapshot

from .test_agent_api import NODE_A, agent_headers
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system


@pytest.fixture(scope="session")
def inventory_wire_probe() -> Path:
    configured = os.environ.get("VONK_INVENTORY_WIRE_PROBE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        path = path.resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AssertionError(f"configured wire probe is not executable: {path}")
        return path

    repository = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--package",
            "vonk-agent-protocol",
            "--example",
            "inventory_wire_probe",
        ],
        cwd=repository,
        check=True,
    )
    target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
    if not target_root.is_absolute():
        target_root = repository / target_root
    path = target_root / "debug" / "examples" / "inventory_wire_probe"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AssertionError(f"cargo did not produce an executable wire probe: {path}")
    return path


def test_rust_inventory_json_crosses_controller_route_and_repository(
    inventory_wire_probe: Path, agent_system
) -> None:
    client, services, _codec, _clock = agent_system
    completed = subprocess.run(
        [str(inventory_wire_probe)], capture_output=True, text=True, check=True
    )
    payload = json.loads(completed.stdout)
    request = InventoryRequest.model_validate(payload)
    response = client.post(
        "/agent/v1/inventory",
        json=payload,
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert response.status_code == 204

    with services.sessions() as session:
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == NODE_A
            )
        )
    assert snapshot is not None
    assert snapshot.disk_free_bytes == request.disk_free_bytes
    assert snapshot.capabilities == sorted(request.capabilities)

    malformed = dict(payload)
    malformed["gpu_count"] = "1"
    assert (
        client.post(
            "/agent/v1/inventory",
            json=malformed,
            headers=agent_headers(NODE_A, "serial-a"),
        ).status_code
        == 422
    )
