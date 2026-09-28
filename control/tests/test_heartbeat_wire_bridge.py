from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from vonk_agent_protocol import AgentDirective

from tests.wire_probes import prebuilt_probe

from .test_agent_api import NODE_A, STOP_PAYLOAD, agent_headers, parent
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system


@pytest.fixture(scope="session")
def heartbeat_wire_probe() -> Path:
    return prebuilt_probe("VONK_HEARTBEAT_WIRE_PROBE")


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
        "/agent/claim",
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
        "/agent/heartbeat",
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
