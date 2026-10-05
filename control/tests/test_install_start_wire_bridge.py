from __future__ import annotations

import json
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    DistributionAssignment,
    RecipeStartResult,
)
from vonk_control.agent_outcome import stored_report
from vonk_control.distribution import DistributionService, MemoryObjectSource
from vonk_control.distribution_assignment import NodeDistributionAssignment
from vonk_control.models import AgentOperation, InstallationNode, RunNode

from tests.wire_probes import prebuilt_probe

from .test_agent_api import NODE_A, agent_headers
from .test_agent_api import agent_system as _agent_system
from .test_distribution import _assignment
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
)

agent_system = _agent_system


@pytest.fixture(scope="session")
def install_start_wire_probe() -> Path:
    return prebuilt_probe("VONK_INSTALL_START_WIRE_PROBE")


def _claim(row: AgentOperation) -> dict[str, Any]:
    return {
        "fence": str(uuid.uuid4()),
        "operation": row.kind,
        "payload": dict(row.payload),
        "deadline": (NOW + timedelta(hours=1)).isoformat(),
    }


def _queued_children(sessions, operation_id: str) -> tuple[AgentOperation, ...]:
    with sessions() as session:
        return tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == operation_id)
                .where(AgentOperation.state == "queued")
                .order_by(AgentOperation.node_id, AgentOperation.id)
            )
        )


def _bridge(probe: Path, rows: tuple[AgentOperation, ...]) -> tuple[AgentResult, ...]:
    assert rows
    input_document = "".join(
        json.dumps(_claim(row), separators=(",", ":")) + "\n" for row in rows
    )
    completed = subprocess.run(
        [str(probe)],
        input=input_document,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(output_lines) == len(rows), completed.stdout
    # The real agent speaks the typed outcome; the Controller stores it in the
    # shape its consumers read, exactly as its ingress does.
    parsed = tuple(
        stored_report(row.kind, AgentResult.parse(json.loads(line)))[0]
        for row, line in zip(rows, output_lines, strict=True)
    )
    for row, result in zip(rows, parsed, strict=True):
        assert result.state == "succeeded"
        document = result.result.model_dump(mode="json", exclude_none=True)
        if row.kind == "recipe.install":
            assert set(document) == {"installed_bytes"}
        elif row.kind in {"recipe.stop", "recipe.uninstall"}:
            assert document == {}
    return parsed


def _project(
    service,
    sessions,
    rows: tuple[AgentOperation, ...],
    results: tuple[AgentResult, ...],
) -> None:
    for row, result in zip(rows, results, strict=True):
        with sessions.begin() as session:
            operation = session.get(AgentOperation, row.id)
            assert operation is not None
            operation.state = result.state
            operation.updated_at = NOW
            service.consume_agent_result(session, operation, None, result)


def test_controller_queued_install_and_start_payloads_cross_rust_and_back(
    tmp_path: Path, install_start_wire_probe: Path
) -> None:
    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)

    install_plan = service.preview_install(mapping_id, build_id)
    install_operation = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="wire-bridge-single-install",
    )
    install_rows = _queued_children(sessions, install_operation.id)
    assert len(install_rows) == 1
    install_results = _bridge(install_start_wire_probe, install_rows)
    _project(service, sessions, install_rows, install_results)
    assert service.get(install_operation.id).state == "succeeded"
    with sessions() as session:
        node = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == install_operation.owner_id,
                InstallationNode.node_id == install_rows[0].node_id,
            )
        )
        assert node is not None and node.state == "installed"

    start_plan = service.preview_run(install_operation.owner_id, "qwen")
    start_operation = service.start(
        start_plan,
        plan_digest=start_plan.plan_digest,
        actor="admin",
        request_id="wire-bridge-single-start",
    )
    start_rows = _queued_children(sessions, start_operation.id)
    assert len(start_rows) == 1
    start_results = _bridge(install_start_wire_probe, start_rows)
    assert RecipeStartResult.model_validate(start_results[0].result).endpoint
    _project(service, sessions, start_rows, start_results)
    assert service.get(start_operation.id).state == "succeeded"
    with sessions() as session:
        node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start_operation.owner_id,
                RunNode.node_id == start_rows[0].node_id,
            )
        )
        assert node is not None and node.state == "running"

    stop_plan = service.preview_stop(start_operation.owner_id)
    stop_operation = service.stop(
        start_operation.owner_id,
        plan_digest=stop_plan.plan_digest,
        actor="admin",
        request_id="wire-bridge-single-stop",
    )
    stop_rows = _queued_children(sessions, stop_operation.id)
    assert len(stop_rows) == 1
    stop_results = _bridge(install_start_wire_probe, stop_rows)
    _project(service, sessions, stop_rows, stop_results)
    assert service.get(stop_operation.id).state == "succeeded"
    with sessions() as session:
        node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start_operation.owner_id,
                RunNode.node_id == stop_rows[0].node_id,
            )
        )
        assert node is not None and node.state == "stopped"


def test_controller_routine_uninstall_payload_crosses_rust_and_back(
    tmp_path: Path, install_start_wire_probe: Path
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service,
        mapping_id,
        build_id,
        nodes,
        request_id="70000000-0000-4000-8000-000000000001",
    )

    preview = service.preview_uninstall(installation.owner_id)
    operation = service.uninstall(
        installation.owner_id,
        plan_digest=preview.plan_digest,
        actor="admin",
        request_id="70000000-0000-4000-8000-000000000002",
    )
    rows = _queued_children(sessions, operation.id)
    assert len(rows) == 1
    assert rows[0].kind == "recipe.uninstall"
    assert rows[0].payload["cleanup_model_content_sha256"] == (
        preview.model_impact.model_content_sha256
    )

    results = _bridge(install_start_wire_probe, rows)
    _project(service, sessions, rows, results)
    assert service.get(operation.id).state == "succeeded"


def test_controller_distributed_rank_and_collective_payloads_cross_rust_and_back(
    tmp_path: Path, install_start_wire_probe: Path
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service,
        mapping_id,
        build_id,
        nodes,
        request_id="wire-bridge-distributed-install",
    )
    start_plan = service.preview_run(installation.owner_id, "qwen")
    start_operation = service.start(
        start_plan,
        plan_digest=start_plan.plan_digest,
        actor="admin",
        request_id="wire-bridge-distributed-start",
    )
    rank_rows = _queued_children(sessions, start_operation.id)
    assert len(rank_rows) == 2
    assert {row.payload.get("phase") for row in rank_rows} == {"rank-launch"}
    rank_results = _bridge(install_start_wire_probe, rank_rows)
    # A rank launch only starts its process; no rank reports an endpoint yet.
    assert [
        RecipeStartResult.model_validate(result.result).endpoint
        for result in rank_results
    ] == [None, None]
    _project(service, sessions, rank_rows, rank_results)
    assert service.get(start_operation.id).state == "running"
    with sessions() as session:
        nodes_after_launch = tuple(
            session.scalars(
                select(RunNode).where(RunNode.run_id == start_operation.owner_id)
            )
        )
        assert {node.state for node in nodes_after_launch} == {"starting"}

    readiness_rows = _queued_children(sessions, start_operation.id)
    assert len(readiness_rows) == 1
    assert readiness_rows[0].payload.get("phase") == "collective-readiness"
    readiness_results = _bridge(install_start_wire_probe, readiness_rows)
    # Only the serving rank reports the endpoint, once the collective is ready.
    assert RecipeStartResult.model_validate(readiness_results[0].result).endpoint
    _project(service, sessions, readiness_rows, readiness_results)
    assert service.get(start_operation.id).state == "succeeded"
    with sessions() as session:
        nodes_after_readiness = tuple(
            session.scalars(
                select(RunNode).where(RunNode.run_id == start_operation.owner_id)
            )
        )
        assert {node.state for node in nodes_after_readiness} == {"running"}


def test_controller_distribution_http_response_round_trips_through_rust(
    agent_system, install_start_wire_probe: Path
) -> None:
    client, services, _tokens, clock = agent_system
    source = MemoryObjectSource()
    assignment = _assignment(
        NODE_A,
        source.put(b"model payload"),
        source.put(b"config!"),
        source.put(b"oci archive"),
    )
    # The same descriptor must survive both the API model and Rust parser.
    document = assignment.to_mapping()
    objects = document["objects"]
    assert isinstance(objects, list)
    weights = objects[0]
    assert isinstance(weights, dict)
    weights["name"] = "weights/模型 weights.bin"
    initializer = objects[1]
    assert isinstance(initializer, dict)
    initializer["name"] = "__init__.py"
    assignment = NodeDistributionAssignment.parse(document)
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(
        assignment.oci_image_digest, assignment.oci_archive_sha256
    )
    service = DistributionService(source, clock=clock)
    service.register(assignment)
    object.__setattr__(services, "distribution", service)
    response = client.get(
        f"/agent/distribution/manifests/{assignment.plan_digest}",
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert response.status_code == 200
    result = subprocess.run(
        [str(install_start_wire_probe), "--distribution"],
        input=response.text + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert DistributionAssignment.model_validate_json(result.stdout) == (
        assignment.wire()
    )
