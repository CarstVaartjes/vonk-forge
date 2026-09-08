from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.compiled_execution_plan import CompiledExecutionPlan
from vonk_control.agent_jobs import AgentJobService
from vonk_control.artifact_job_api import install_artifact_job_routes
from vonk_control.auth import Actor
from vonk_control.models import RecipeInstallation, RecipeRun

from .runtime_identity_support import claim_agent
from .test_artifact_jobs import running_artifact_service
from .test_recipe_operations import NOW


@pytest.fixture(scope="session")
def job_invocation_probe() -> Path:
    configured = os.environ.get("VONK_JOB_INVOCATION_WIRE_PROBE")
    if configured:
        return Path(configured)
    repository = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "-p",
            "vonk-agent",
            "--example",
            "job_invocation_wire_probe",
        ],
        cwd=repository,
        check=True,
    )
    return (
        Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
        / "debug/examples/job_invocation_wire_probe"
    )


@pytest.mark.parametrize("has_input", [True, False])
def test_job_api_compiles_changed_seed_through_production_runtime(
    tmp_path: Path, job_invocation_probe: Path, has_input: bool
) -> None:
    compound = {"engine_extension": {"enabled": True, "values": [1, "opaque / value"]}}

    def configure(recipe):
        if not has_input:
            recipe["interfaces"][0]["input"] = None
            recipe["validation"]["serving"]["checks"][0]["request"]["input_path"] = None
        recipe["runtime"]["arguments"].append(
            {"name": "engine-config", "value": compound, "setting": None}
        )

    sessions, _, _, service, run_id, node_id = running_artifact_service(
        tmp_path, recipe_transform=configure
    )
    app = FastAPI()

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = str(uuid.uuid4())
        return await call_next(request)

    install_artifact_job_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
    )
    client = TestClient(app)
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        installed = session.get(RecipeInstallation, run.installation_id).plan[
            "compiled_execution_plans"
        ][node_id]
    assert (
        installed["runtime"]["argv"][installed["runtime"]["argv"].index("--seed") + 1]
        == "0"
    )
    assert installed["lifecycle"]["stop_timeout_seconds"] < 600
    payload = {
        "interface": "image-job",
        "parameters": {"prompt": "fox", "seed": 12345},
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
        "timeout_seconds": 600,
    }
    if not has_input:
        payload["inputs"] = []
    assert (
        client.post(
            f"/api/v1/recipes/runs/{run_id}/artifact-jobs",
            json={**payload, "timeout_seconds": 3601},
        ).status_code
        == 422
    )
    created = client.post(f"/api/v1/recipes/runs/{run_id}/artifact-jobs", json=payload)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    if has_input:
        upload = client.put(
            f"/api/v1/artifact-jobs/{job_id}/inputs/input.png",
            content=b"png",
            headers={
                "content-type": "image/png",
                "x-content-sha256": hashlib.sha256(b"png").hexdigest(),
            },
        )
        assert upload.status_code == 200, upload.text
    assert client.post(f"/api/v1/artifact-jobs/{job_id}/finalize").status_code == 200
    submitted = client.post(f"/api/v1/artifact-jobs/{job_id}/submit")
    assert submitted.status_code == 202, submitted.text
    claim = claim_agent(
        AgentJobService(sessions, clock=lambda: NOW), node_id, "serial-0", 60
    )
    document = json.loads(canonical_message(claim))
    request = document["payload"]
    invocation = request["compiled_execution_plan"]
    assert "parameters" not in request
    assert (
        json.loads(
            invocation["runtime"]["argv"][
                invocation["runtime"]["argv"].index("--engine-config") + 1
            ]
        )
        == compound
    )
    assert (
        invocation["identity"]["execution_sha256"]
        != installed["identity"]["execution_sha256"]
    )
    assert (
        invocation["runtime"]["argv"][invocation["runtime"]["argv"].index("--seed") + 1]
        == "12345"
    )
    probe_input = {
        "claim": document,
        "installed": installed,
        "input_contents": {"input.png": list(b"png")} if has_input else {},
    }

    def invoke(value):
        return subprocess.run(
            [str(job_invocation_probe)],
            input=json.dumps(value),
            text=True,
            capture_output=True,
            check=False,
        )

    result = invoke(probe_input)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    arguments = output["arguments"]
    image_index = arguments.index(installed["runtime_image"]["local_image_reference"])
    assert arguments[image_index + 1 :] == [
        invocation["runtime"]["executable"],
        *invocation["runtime"]["argv"],
    ]
    assert CompiledExecutionPlan.model_validate(output["runtime"]) == (
        CompiledExecutionPlan.model_validate(invocation)
    )
    assert output["input_manifest"] == {
        "schema_version": 1,
        "total_bytes": 3 if has_input else 0,
        "files": request["inputs"],
    }
    assert any("dst=/inputs,readonly" in argument for argument in arguments)
    assert "VONK_JOB_TIMEOUT_SECONDS=600" in arguments
    if has_input:
        for include_null in (False, True):
            equivalent = copy.deepcopy(probe_input)
            job_input = equivalent["claim"]["payload"]["compiled_execution_plan"][
                "job"
            ]["input"]
            if include_null:
                job_input["slots"] = None
            else:
                job_input.pop("slots", None)
            # Keep the actual Controller digest: optional omission and explicit
            # null must verify as the same signed invocation, without rehashing.
            equivalent_result = invoke(equivalent)
            assert equivalent_result.returncode == 0, equivalent_result.stderr
            assert json.loads(equivalent_result.stdout)["arguments"] == arguments
    for change in (
        "image",
        "mount",
        "environment",
        "executable",
        "timeout",
        "memory",
        "missing_nullable_endpoint",
        "missing_nullable_engine_version",
    ):
        invalid = copy.deepcopy(probe_input)
        plan = invalid["claim"]["payload"]["compiled_execution_plan"]
        if change == "image":
            plan["runtime"]["image_digest"] = "sha256:" + "0" * 64
        elif change == "mount":
            plan["security"]["mounts"][0]["target"] = "/etc"
        elif change == "environment":
            plan["runtime"]["env"].append({"name": "UNAUTHORIZED", "value": "yes"})
        elif change == "executable":
            plan["runtime"]["executable"] = "/opt/vonk/bin/other"
        elif change == "timeout":
            plan["job"]["timeout_seconds"] = 3601
            invalid["claim"]["payload"]["timeout_seconds"] = 3601
        elif change == "memory":
            plan["runtime"]["placement"]["reserved_memory_bytes"] += 1
        elif change == "missing_nullable_endpoint":
            del plan["runtime"]["placement"]["endpoint_address"]
        else:
            del plan["runtime"]["telemetry"]["engine_version"]
        invalid["claim"]["payload_digest"] = hashlib.sha256(
            canonical_message(invalid["claim"]["payload"])
        ).hexdigest()
        assert invoke(invalid).returncode != 0, change
