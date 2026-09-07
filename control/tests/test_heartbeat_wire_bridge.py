from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from vonk_agent_protocol import AgentDirective

from .test_agent_api import NODE_A, STOP_PAYLOAD, agent_headers, parent
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system


@pytest.fixture(scope="session")
def heartbeat_wire_probe() -> Path:
    configured = os.environ.get("VONK_HEARTBEAT_WIRE_PROBE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        path = path.resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AssertionError(
                f"configured heartbeat wire probe is not executable: {path}"
            )
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
            "heartbeat_wire_probe",
        ],
        cwd=repository,
        check=True,
    )
    target = target_root / "debug" / "examples" / "heartbeat_wire_probe"
    if not target.is_file() or not os.access(target, os.X_OK):
        raise AssertionError(
            f"cargo did not produce an executable wire probe: {target}"
        )
    return target


def test_controller_heartbeat_response_crosses_rust_directive_parser(
    agent_system, heartbeat_wire_probe: Path
) -> None:
    client, services, _, clock = agent_system
    operation = services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 3},
    ).json()
    progress = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    } | {"progress": {"phase": "checking"}}

    response = client.post(
        "/agent/v1/heartbeat",
        headers={
            **agent_headers(NODE_A, "serial-a"),
            "x-vonk-agent-source": "10.0.0.43",
        },
        json=progress,
    )
    assert response.status_code == 200
    produced = AgentDirective.parse(response.json())
    assert produced.operation_id == operation.id

    parsed = subprocess.run(
        [str(heartbeat_wire_probe)],
        input=response.content + b"\n",
        capture_output=True,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stderr.decode()
    parsed_directive = AgentDirective.parse(json.loads(parsed.stdout))
    assert parsed_directive == produced
    assert set(json.loads(parsed.stdout)) == set(response.json())
