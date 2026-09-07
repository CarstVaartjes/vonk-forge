from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentOperation as ProtocolAgentOperation,
)
from vonk_agent_protocol import (
    AgentProtocolError,
    AgentResult,
    DistributionAssignment,
    RecipeOperationRequest,
    canonical_message,
)
from vonk_control.distribution import DistributionService, MemoryVerifiedObjectSource
from vonk_control.models import AgentOperation, InstallationNode, RunNode

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
    configured = os.environ.get("VONK_INSTALL_START_WIRE_PROBE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        path = path.resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AssertionError(f"configured wire probe is not executable: {path}")
        return path

    repository = Path(__file__).resolve().parents[2]
    target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
    if not target_root.is_absolute():
        target_root = repository / target_root
    target = target_root / "debug" / "examples" / "install_start_wire_probe"
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--package",
            "vonk-agent",
            "--example",
            "install_start_wire_probe",
        ],
        cwd=repository,
        check=True,
    )
    if not target.is_file() or not os.access(target, os.X_OK):
        raise AssertionError(
            f"cargo did not produce an executable wire probe: {target}"
        )
    return target


def _claim(row: AgentOperation) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": row.parent_job_id,
        "operation_id": row.id,
        "attempt": 1,
        "fence": str(uuid.uuid4()),
        "node_id": row.node_id,
        "operation": row.kind,
        "authority_revision": row.authority_revision,
        "payload_digest": row.payload_digest,
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
    parsed = tuple(AgentResult.parse(json.loads(line)) for line in output_lines)
    for row, result in zip(rows, parsed, strict=True):
        assert result.state == "succeeded"
        evidence = result.result.get("evidence", result.result)
        if row.kind == "recipe.install":
            assert set(result.result) == {"installed_bytes"}
        elif row.kind == "recipe.stop":
            assert result.result.model_dump(mode="json") == {"stopped": True}
        elif row.kind == "recipe.uninstall":
            assert set(result.result) == {"uninstalled", "removed_model_bytes"}
            assert result.result.model_dump(mode="json") == {
                "uninstalled": True,
                "removed_model_bytes": 0,
            }
        elif row.kind == "recipe.model-uninstall.v1":
            assert set(result.result) == {
                "uninstalled_installations",
                "removed_model_bytes",
            }
            assert result.result["removed_model_bytes"] == 0
        else:
            assert "evidence" in result.result
        if "image_digest" in evidence:
            assert not str(evidence["image_digest"]).startswith("sha256:")
        if "model_identity" in evidence:
            assert "@" in evidence["model_identity"]
    return parsed


def _assert_omitted_start_field_rejected(
    probe: Path, row: AgentOperation, field: str
) -> None:
    payload = dict(row.payload)
    payload.pop(field)
    claim = _claim(row)
    claim["payload"] = payload
    claim["payload_digest"] = hashlib.sha256(canonical_message(payload)).hexdigest()
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(ProtocolAgentOperation.RECIPE_START, payload)
    completed = subprocess.run(
        [str(probe)],
        input=json.dumps(claim, separators=(",", ":")) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert not completed.stdout


def _project(
    service,
    sessions,
    rows: tuple[AgentOperation, ...],
    results: tuple[AgentResult, ...],
) -> None:
    for row, result in zip(rows, results, strict=True):
        assert result.operation_id == row.id
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
    for field in ("local_address", "master_address", "master_port"):
        _assert_omitted_start_field_rejected(
            install_start_wire_probe, start_rows[0], field
        )
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


def test_controller_explicit_multi_installation_cleanup_payload_crosses_rust_and_back(
    tmp_path: Path, install_start_wire_probe: Path
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    first = installed_recipe(
        service,
        mapping_id,
        build_id,
        nodes,
        request_id="70000000-0000-4000-8000-000000000003",
    )
    second = installed_recipe(
        service,
        mapping_id,
        build_id,
        nodes,
        request_id="70000000-0000-4000-8000-000000000004",
    )

    model_digest = service.preview_uninstall(
        first.owner_id
    ).model_impact.model_content_sha256
    assert model_digest is not None
    preview = service.preview_model_deletion(model_digest)
    operation = service.delete_model(
        model_digest,
        plan_digest=preview.plan_digest,
        actor="admin",
        request_id="70000000-0000-4000-8000-000000000005",
    )
    rows = _queued_children(sessions, operation.id)
    assert len(rows) == 1
    assert rows[0].kind == "recipe.model-uninstall.v1"
    assert rows[0].payload["model_content_sha256"] == model_digest
    assert {item["installation_id"] for item in rows[0].payload["installations"]} == {
        first.owner_id,
        second.owner_id,
    }

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
    source = MemoryVerifiedObjectSource()
    assignment = _assignment(
        NODE_A,
        source.put(b"model payload"),
        source.put(b"config!"),
        source.put(b"oci archive"),
    )
    # The same descriptor must survive both the API model and Rust parser.
    document = assignment.to_mapping()
    document["objects"][0]["name"] = "weights/模型 weights.bin"
    document["objects"][1]["name"] = "__init__.py"
    assignment = DistributionAssignment.parse(document)
    source.register_artifact_set(assignment.model_artifact_set_sha256, assignment.objects)
    source.register_runtime_image(assignment.oci_image_digest, assignment.oci_archive_sha256)
    service = DistributionService(source, clock=clock)
    service.register(assignment)
    object.__setattr__(services, "distribution", service)
    response = client.get(
        f"/agent/v1/distribution/manifests/{assignment.plan_digest}",
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert response.status_code == 200
    result = subprocess.run(
        [str(install_start_wire_probe), "--distribution"],
        input=response.text + "\n", capture_output=True, text=True, check=True,
    )
    assert DistributionAssignment.model_validate_json(result.stdout) == assignment
