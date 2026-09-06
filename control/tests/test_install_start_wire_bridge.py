from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentResult

from vonk_control.models import AgentOperation

from control.tests.test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
)


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
    if not target.is_file():
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
        raise AssertionError(f"cargo did not produce an executable wire probe: {target}")
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


def _bridge(
    probe: Path, rows: tuple[AgentOperation, ...]
) -> tuple[AgentResult, ...]:
    assert rows
    input_document = "".join(
        json.dumps(_claim(row), separators=(",", ":")) + "\n" for row in rows
    )
    completed = subprocess.run(
        [str(probe)],
        input=input_document,
        text=True,
        capture_output=True,
        check=True,
    )
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(output_lines) == len(rows), completed.stdout
    parsed = tuple(AgentResult.parse(json.loads(line)) for line in output_lines)
    for result in parsed:
        assert result.state == "succeeded"
        evidence = result.result["evidence"]
        if "image_digest" in evidence:
            assert not str(evidence["image_digest"]).startswith("sha256:")
        if "model_identity" in evidence:
            assert "@" in evidence["model_identity"]
    return parsed


def _project(
    service,
    operation_id: str,
    rows: tuple[AgentOperation, ...],
    results: tuple[AgentResult, ...],
) -> None:
    for row, result in zip(rows, results, strict=True):
        assert result.operation_id == row.id
        service.record_node_result(
            operation_id,
            row.node_id,
            succeeded=True,
            evidence=result.result["evidence"],
        )


def test_controller_queued_install_and_start_payloads_cross_rust_and_back(
    tmp_path: Path, install_start_wire_probe: Path
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)

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
    _project(service, install_operation.id, install_rows, install_results)
    assert service.get(install_operation.id).state == "succeeded"

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
    _project(service, start_operation.id, start_rows, start_results)
    assert service.get(start_operation.id).state == "succeeded"


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
    _project(service, start_operation.id, rank_rows, rank_results)
    assert service.get(start_operation.id).state == "running"

    readiness_rows = _queued_children(sessions, start_operation.id)
    assert len(readiness_rows) == 1
    assert readiness_rows[0].payload.get("phase") == "collective-readiness"
    readiness_results = _bridge(install_start_wire_probe, readiness_rows)
    _project(service, start_operation.id, readiness_rows, readiness_results)
    assert service.get(start_operation.id).state == "succeeded"
