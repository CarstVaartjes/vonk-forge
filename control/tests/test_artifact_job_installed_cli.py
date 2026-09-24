"""Installed CLI artifact transfers cross the registered API and durable service."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from vonk_agent_protocol import (
    RecipeJobFile,
    RecipeJobRunResult,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control.artifact_blob_store import ArtifactBlobStore
from vonk_control.artifact_job_api import install_artifact_job_routes
from vonk_control.artifact_jobs import ArtifactJobResponse, ArtifactJobService
from vonk_control.auth import Actor
from vonk_control.models import AgentOperation, ArtifactJob, Job
from vonk_control.strict_json import ControllerAPIRoute

from .test_artifact_jobs import _configure_artifact_recipe
from .test_profile_load_installed_cli import (
    _https_api_peer,
    _process_environment,
)
from .test_recipe_operations import NOW, installed_recipe, setup_services

pytest_plugins = ("tests.test_profile_load_installed_cli",)

CREATE_KEY = "00000000-0000-4000-8000-000000000103"
SUBMIT_KEY = "00000000-0000-4000-8000-000000000104"
OUTPUT_LIMITS: dict[str, object] = {
    "max_files": 1,
    "max_file_bytes": 1024,
    "max_total_bytes": 4096,
    "allowed_media_types": ["image/png"],
}


def _artifact_api(tmp_path: Path, postgres_engine, *, output_min_files: int = 1):
    def configure_recipe(document: dict[str, object]) -> None:
        _configure_artifact_recipe(document)
        interfaces = document.get("interfaces")
        assert isinstance(interfaces, list) and len(interfaces) == 1
        interface = interfaces[0]
        assert isinstance(interface, dict)
        output = interface.get("output")
        assert isinstance(output, dict)
        slots = output.get("slots")
        assert isinstance(slots, list) and len(slots) == 1
        slot = slots[0]
        assert isinstance(slot, dict)
        slot["min_files"] = output_min_files

    sessions, recipe_operations, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        recipe_transform=configure_recipe,
        engine=postgres_engine,
    )
    installed = installed_recipe(
        recipe_operations,
        mapping_id,
        build_id,
        nodes,
        request_id="00000000-0000-4000-8000-000000000101",
    )
    plan = recipe_operations.preview_run(installed.owner_id, "image-job")
    activated = recipe_operations.activate_job_run(
        plan,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000102",
    )
    service = ArtifactJobService(
        sessions,
        recipe_operations=recipe_operations,
        blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
        clock=lambda: NOW,
    )
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute
    install_artifact_job_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
    )
    return sessions, TestClient(app), service, activated.owner_id, nodes[0]


def _run_cli(
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    *arguments: str,
    json_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(executable),
            *(["--json"] if json_output else []),
            "recipe",
            "job",
            *arguments,
        ],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def test_installed_cli_recovers_submitted_job_and_publishes_only_verified_output(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
) -> None:
    sessions, api, service, run_id, _node_id = _artifact_api(tmp_path, postgres_engine)
    headers = {"Authorization": "Bearer installed-artifact-test-token"}
    input_content = b"declared image input"
    input_path = tmp_path / "input.png"
    input_path.write_bytes(input_content)
    binding_path = tmp_path / "artifact-binding.json"
    binding_path.write_text(
        json.dumps(
            {
                "create": {
                    "interface": "image-job",
                    "parameters": {"prompt": "fox", "seed": 0},
                    "inputs": [
                        {
                            "slot": "input",
                            "name": "input.png",
                            "media_type": "image/png",
                        }
                    ],
                    "output_limits": OUTPUT_LIMITS,
                    "timeout_seconds": 3600,
                },
                "input_paths": {"input.png": str(input_path)},
            }
        ),
        encoding="utf-8",
    )
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, peer):
        environment = _process_environment(tmp_path, url, certificate, headers)
        created = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "create",
            "--run",
            run_id,
            "--file",
            str(binding_path),
            "--request-key",
            CREATE_KEY,
        )
        assert created.returncode == 0, created.stderr
        draft = json.loads(created.stdout)
        job_id = draft.get("id")
        assert isinstance(job_id, str)
        assert draft["state"] == "ready"
        uploaded_inputs = draft.get("input_files")
        assert isinstance(uploaded_inputs, list) and len(uploaded_inputs) == 1
        assert uploaded_inputs[0]["name"] == "input.png"
        assert uploaded_inputs[0]["size_bytes"] == len(input_content)
        assert uploaded_inputs[0]["sha256"] == hashlib.sha256(input_content).hexdigest()
        assert peer.calls[0][0:2] == ("GET", "/api/artifact-jobs/capabilities")
        assert any(
            method == "PUT" and path == f"/api/artifact-jobs/{job_id}/inputs/input.png"
            for method, path, _document in peer.calls
        )

        submit_path = f"/api/artifact-jobs/{job_id}/submit"
        peer.drop_responses.add(("POST", submit_path))
        submitted = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "submit",
            job_id,
            "--request-key",
            SUBMIT_KEY,
        )
        assert submitted.returncode == 0, submitted.stderr
        receipt = json.loads(submitted.stdout)
        assert receipt["id"] == job_id
        assert receipt["state"] == "queued"
        operation_id = receipt.get("operation_id")
        assert isinstance(operation_id, str)
        assert peer.dropped_responses == [("POST", submit_path)]
        assert [
            (method, path)
            for method, path, _document in peer.calls
            if method == "POST" and path == submit_path
        ] == [("POST", submit_path)]
        assert ("GET", f"/api/artifact-jobs/{job_id}", None) in peer.calls

        assert receipt["submit_request_id"] == SUBMIT_KEY
        replayed = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "submit",
            job_id,
            "--request-key",
            SUBMIT_KEY,
        )
        assert replayed.returncode == 0, replayed.stdout + replayed.stderr
        replayed_receipt = json.loads(replayed.stdout)
        assert replayed_receipt["operation_id"] == operation_id
        assert replayed_receipt["submit_request_id"] == SUBMIT_KEY

        replacement_key = "00000000-0000-4000-8000-000000000105"
        before_refusal = len(peer.calls)
        replacement = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "submit",
            job_id,
            "--request-key",
            replacement_key,
        )
        assert replacement.returncode == 2
        refusal = json.loads(replacement.stdout)
        assert "error" in refusal
        assert "operation_id" not in refusal
        expected_detail = f"vonkctl recipe job detail {job_id}"
        assert refusal["reconcile"]["operation"] == expected_detail
        assert peer.calls[before_refusal:] == [("POST", submit_path, None)]
        before_human_refusal = len(peer.calls)
        human_refusal = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "submit",
            job_id,
            "--request-key",
            replacement_key,
            json_output=False,
        )
        assert human_refusal.returncode == 2
        assert f"Next: {expected_detail}" in human_refusal.stderr
        assert f"Next: vonkctl recipe job submit {job_id}" not in human_refusal.stderr
        assert peer.calls[before_human_refusal:] == [("POST", submit_path, None)]
        with sessions() as session:
            artifact_job = session.get(ArtifactJob, job_id)
            assert artifact_job is not None
            assert artifact_job.operation_id == operation_id
            parent = session.get(Job, operation_id)
            assert parent is not None
            assert parent.request_id == SUBMIT_KEY

        authoritative = api.get(f"/api/artifact-jobs/{job_id}").json()
        assert authoritative["operation_id"] == operation_id
        assert authoritative["submit_request_id"] == SUBMIT_KEY

        with sessions() as session:
            artifact_job = session.get(ArtifactJob, job_id)
            assert artifact_job is not None
            assert artifact_job.request_id == CREATE_KEY
            assert artifact_job.state == "queued"
            assert artifact_job.operation_id == operation_id
            parent = session.get(Job, operation_id)
            assert parent is not None
            assert parent.request_id == SUBMIT_KEY
            operation = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == operation_id
                )
            )
            assert operation is not None
            operation_key = operation.id
            node_id = operation.node_id

        # Exercise the durable result consumer and the registered download route
        # with an accepted service result; this does not claim a Spark execution.
        output_content = b"verified artifact result bytes"
        output_digest = hashlib.sha256(output_content).hexdigest()
        service.put_output(
            job_id,
            node_id=node_id,
            name="output.png",
            media_type="image/png",
            expected_sha256=output_digest,
            content=output_content,
        )
        output = RecipeJobFile(
            name="output.png",
            media_type="image/png",
            size_bytes=len(output_content),
            sha256=output_digest,
        )
        result = {
            "schema_version": 1,
            "job_id": job_id,
            "run_id": run_id,
            "exit_code": 0,
            "output_manifest": {
                **recipe_job_manifest_document((output,)),
                "manifest_sha256": recipe_job_manifest_sha256((output,)),
            },
            "evidence": {"elapsed_milliseconds": 1234, "peak_memory_bytes": None},
        }
        with sessions.begin() as session:
            stored_operation = session.get(AgentOperation, operation_key)
            assert stored_operation is not None
            service.consume_agent_result(
                session,
                stored_operation,
                object(),
                SimpleNamespace(state="succeeded", result=result),
            )
        assert service.result_metadata(job_id).state == "succeeded"

        output_directory = tmp_path / "downloaded-output"
        output_directory.mkdir()
        output_path = output_directory / "output.png"
        result_path = f"/api/artifact-jobs/{job_id}/results/output.png/{output_digest}"
        peer.corrupt_responses.add(("GET", result_path))
        refused = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "download",
            job_id,
            "--output",
            str(output_directory),
        )
        assert refused.returncode == 2
        assert ("GET", result_path) in peer.corrupted_responses
        assert not output_path.exists()
        assert list(output_directory.iterdir()) == []

        downloaded = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "download",
            job_id,
            "--output",
            str(output_directory),
        )
        human_download = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "download",
            job_id,
            "--output",
            str(output_directory),
            json_output=False,
        )
        assert human_download.returncode == 0, human_download.stderr
        assert f"Artifact job: {job_id}" in human_download.stdout
        assert f"Verified path: {output_path}" in human_download.stdout
        assert "File state: reused" in human_download.stdout

    assert downloaded.returncode == 0, downloaded.stderr
    transfer = json.loads(downloaded.stdout)
    assert transfer["job_id"] == job_id
    assert transfer["files"][0]["state"] == "downloaded"
    assert output_path.read_bytes() == output_content
    assert hashlib.sha256(output_path.read_bytes()).hexdigest() == output_digest
    assert not any(
        path.name.startswith(".output.png.") and path.name.endswith(".download")
        for path in output_directory.iterdir()
    )
    token_value = headers["Authorization"].removeprefix("Bearer ")
    assert token_value not in (
        created.stdout
        + created.stderr
        + submitted.stdout
        + submitted.stderr
        + refused.stdout
        + refused.stderr
        + downloaded.stdout
        + downloaded.stderr
    )


@pytest.mark.parametrize(
    ("output_min_files", "expected_state"),
    [(0, "succeeded"), (1, "failed")],
    ids=["optional-output", "required-output"],
)
def test_installed_cli_distinguishes_unavailable_from_empty_result_manifest(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    output_min_files: int,
    expected_state: str,
) -> None:
    sessions, api, service, run_id, _node_id = _artifact_api(
        tmp_path, postgres_engine, output_min_files=output_min_files
    )
    headers = {"Authorization": "Bearer installed-empty-result-test-token"}
    input_content = b"declared image input"
    input_path = tmp_path / "input.png"
    input_path.write_bytes(input_content)
    binding_path = tmp_path / "artifact-binding.json"
    binding_path.write_text(
        json.dumps(
            {
                "create": {
                    "interface": "image-job",
                    "parameters": {"prompt": "fox", "seed": 0},
                    "inputs": [
                        {
                            "slot": "input",
                            "name": "input.png",
                            "media_type": "image/png",
                        }
                    ],
                    "output_limits": OUTPUT_LIMITS,
                    "timeout_seconds": 3600,
                },
                "input_paths": {"input.png": str(input_path)},
            }
        ),
        encoding="utf-8",
    )
    output_directory = tmp_path / "empty-result-output"
    output_directory.mkdir()

    with _https_api_peer(tmp_path, api, headers) as (url, certificate, _peer):
        environment = _process_environment(tmp_path, url, certificate, headers)
        created = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "create",
            "--run",
            run_id,
            "--file",
            str(binding_path),
            "--request-key",
            "00000000-0000-4000-8000-000000000106",
        )
        assert created.returncode == 0, created.stderr
        job_id = json.loads(created.stdout).get("id")
        assert isinstance(job_id, str)

        submitted = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "submit",
            job_id,
            "--request-key",
            "00000000-0000-4000-8000-000000000107",
        )
        assert submitted.returncode == 0, submitted.stderr
        assert json.loads(submitted.stdout)["state"] == "queued"

        unavailable = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "download",
            job_id,
            "--output",
            str(output_directory),
        )
        assert unavailable.returncode == 2
        assert list(output_directory.iterdir()) == []
        human_queued = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "detail",
            job_id,
            json_output=False,
        )
        assert human_queued.returncode == 0, human_queued.stderr
        assert f"Artifact job: {job_id}" in human_queued.stdout
        assert "Result files: unavailable" in human_queued.stdout
        assert f"vonkctl recipe job detail {job_id} --follow" in human_queued.stdout

        with sessions() as session:
            artifact_job = session.get(ArtifactJob, job_id)
            assert artifact_job is not None
            assert artifact_job.operation_id is not None
            operation = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == artifact_job.operation_id
                )
            )
            assert operation is not None
            operation_id = operation.id

        empty_outputs: tuple[RecipeJobFile, ...] = ()
        empty_result = {
            "schema_version": 1,
            "job_id": job_id,
            "run_id": run_id,
            "exit_code": 0,
            "output_manifest": {
                **recipe_job_manifest_document(empty_outputs),
                "manifest_sha256": recipe_job_manifest_sha256(empty_outputs),
            },
            "evidence": {"elapsed_milliseconds": 1234, "peak_memory_bytes": None},
        }
        assert RecipeJobRunResult.parse(empty_result).outputs == ()
        with sessions.begin() as session:
            stored_operation = session.get(AgentOperation, operation_id)
            assert stored_operation is not None
            service.consume_agent_result(
                session,
                stored_operation,
                object(),
                SimpleNamespace(state="succeeded", result=empty_result),
            )

        view = service.get(job_id)
        assert view.state == expected_state
        if expected_state == "succeeded":
            result = service.result_metadata(job_id)
            assert result.output_files == ()
            assert result.output_manifest_sha256 == recipe_job_manifest_sha256(
                empty_outputs
            )
        else:
            assert view.status_reason is not None
            assert "output slot image file count is invalid" in view.status_reason
            malformed_success = replace(
                view,
                state="succeeded",
                output_manifest_sha256=recipe_job_manifest_sha256(empty_outputs),
                result_evidence={
                    "elapsed_milliseconds": 1234,
                    "peak_memory_bytes": None,
                },
                status_reason=None,
            )
            with pytest.raises(
                ValidationError, match="output slot image file count is invalid"
            ):
                ArtifactJobResponse.model_validate(
                    malformed_success, from_attributes=True
                )

        empty_download = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "download",
            job_id,
            "--output",
            str(output_directory),
        )
        human_result = _run_cli(
            installed_vonkctl,
            environment,
            tmp_path,
            "detail",
            job_id,
            json_output=False,
        )
        assert human_result.returncode == 0, human_result.stderr
        assert f"Artifact job: {job_id}" in human_result.stdout
        if expected_state == "succeeded":
            human_empty_download = _run_cli(
                installed_vonkctl,
                environment,
                tmp_path,
                "download",
                job_id,
                "--output",
                str(output_directory),
                json_output=False,
            )
            assert human_empty_download.returncode == 0, human_empty_download.stderr
            for output in (human_result.stdout, human_empty_download.stdout):
                assert "job succeeded with an empty result" in output
                assert "unavailable" not in output
        else:
            assert "State: failed" in human_result.stdout
            assert "output slot image file count is invalid" in human_result.stdout

    if expected_state == "succeeded":
        assert empty_download.returncode == 0, empty_download.stderr
        transfer = json.loads(empty_download.stdout)
        assert transfer["state"] == "succeeded"
        assert transfer["output_manifest_sha256"] == recipe_job_manifest_sha256(
            empty_outputs
        )
        assert transfer["files"] == []
        assert transfer["total_bytes"] == 0
        assert list(output_directory.iterdir()) == []
    else:
        assert empty_download.returncode == 2
        assert list(output_directory.iterdir()) == []
