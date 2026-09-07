from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    AgentResult,
    RecipeJobFile,
    RecipeJobRunRequest,
    RecipeJobRunResult,
    canonical_message,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.models import AgentNode, AgentOperation

from .runtime_identity_support import claim_agent
from .test_artifact_jobs import running_artifact_service
from .test_recipe_operations import NOW


@pytest.fixture(scope="session")
def recipe_job_wire_probe() -> Path:
    configured = os.environ.get("VONK_RECIPE_JOB_WIRE_PROBE")
    repository = Path(__file__).resolve().parents[2]
    if configured:
        probe = Path(configured)
        if not probe.is_absolute():
            probe = repository / probe
    else:
        target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
        if not target_root.is_absolute():
            target_root = repository / target_root
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--package",
                "vonk-agent-protocol",
                "--example",
                "recipe_job_wire_probe",
            ],
            cwd=repository,
            check=True,
        )
        probe = target_root / "debug" / "examples" / "recipe_job_wire_probe"
    probe = probe.resolve()
    if not probe.is_file() or not os.access(probe, os.X_OK):
        raise AssertionError(
            f"configured recipe-job wire probe is not executable: {probe}"
        )
    return probe


def test_controller_artifact_job_result_crosses_rust_and_python(
    tmp_path: Path, recipe_job_wire_probe: Path
) -> None:
    sessions, operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path
    )
    agent_jobs = AgentJobService(sessions, clock=lambda: NOW)
    operations._agent_jobs = agent_jobs
    request = {
        "run_id": run_id,
        "interface": "image-job",
        "parameters": {"prompt": "fox", "seed": 0},
        "inputs": [
            {
                "slot": "input",
                "name": "input.png",
                "media_type": "image/png",
                "size_bytes": 3,
                "sha256": hashlib.sha256(b"png").hexdigest(),
            }
        ],
        "output_limits": {
            "max_files": 1,
            "max_file_bytes": 1024,
            "max_total_bytes": 4096,
            "allowed_media_types": ["image/png"],
        },
        "timeout_seconds": 3600,
        "actor": "operator",
        "request_id": "00000000-0000-0000-0000-000000000152",
    }
    job = service.create(**request)
    service.put_input(
        job.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=request["inputs"][0]["sha256"],
        content=b"png",
    )
    service.finalize(job.id)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.capabilities = [*node.capabilities, "recipe.job.run.v1"]
    service.submit(
        job.id,
        actor="operator",
        request_id="00000000-0000-0000-0000-000000000153",
    )
    claim = claim_agent(agent_jobs, node_id, "serial-0", 3600)
    assert claim is not None
    claim_document = json.loads(canonical_message(claim))
    typed_request = RecipeJobRunRequest.parse(claim.payload)
    child_operation_id = claim.operation_id

    output = b"done"
    output_sha256 = hashlib.sha256(output).hexdigest()
    service.put_output(
        job.id,
        node_id=node_id,
        name="output.png",
        media_type="image/png",
        expected_sha256=output_sha256,
        content=output,
    )
    output_file = RecipeJobFile(
        name="output.png",
        media_type="image/png",
        size_bytes=len(output),
        sha256=output_sha256,
    )
    result_document = {
        "schema_version": 1,
        "job_id": claim.job_id,
        "operation_id": claim.operation_id,
        "attempt": 1,
        "fence": claim.fence,
        "node_id": claim.node_id,
        "deadline": claim_document["deadline"],
        "state": "succeeded",
        "result": {
            "schema_version": 1,
            "job_id": job.id,
            "run_id": run_id,
            "exit_code": 0,
            "output_manifest": {
                **recipe_job_manifest_document((output_file,)),
                "manifest_sha256": recipe_job_manifest_sha256((output_file,)),
            },
            "evidence": {"elapsed_milliseconds": 2, "peak_memory_bytes": None},
        },
    }
    input_document = json.dumps(
        {"claim": claim_document, "result": result_document}, separators=(",", ":")
    )
    completed = subprocess.run(
        [str(recipe_job_wire_probe)],
        input=input_document + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    bridge = json.loads(completed.stdout)
    rust_request = RecipeJobRunRequest.parse(bridge["request"])
    rust_result = RecipeJobRunResult.parse(bridge["result"])
    assert rust_request == typed_request
    assert rust_result.output_manifest_sha256 == recipe_job_manifest_sha256(
        (output_file,)
    )

    with sessions.begin() as session:
        operation = session.get(AgentOperation, child_operation_id)
        assert operation is not None
        operation.state = "succeeded"
        service.consume_agent_result(
            session, operation, None, AgentResult.parse(result_document)
        )
    assert service.get(job.id).state == "succeeded"


def test_recipe_job_result_accepts_omitted_or_explicit_null_reason() -> None:
    file = RecipeJobFile(
        name="output.png", media_type="image/png", size_bytes=0, sha256="a" * 64
    )
    base = {
        "schema_version": 1,
        "job_id": "00000000-0000-4000-8000-000000000011",
        "run_id": "00000000-0000-4000-8000-000000000012",
        "exit_code": 0,
        "output_manifest": {
            **recipe_job_manifest_document((file,)),
            "manifest_sha256": recipe_job_manifest_sha256((file,)),
        },
        "evidence": {"elapsed_milliseconds": 1, "peak_memory_bytes": None},
    }
    omitted = RecipeJobRunResult.parse(base)
    explicit = RecipeJobRunResult.parse({**base, "reason": None})
    assert omitted.reason is explicit.reason is None
    assert "reason" not in omitted.to_mapping() == explicit.to_mapping()
