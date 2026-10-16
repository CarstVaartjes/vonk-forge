"""The artifact job canary: one-shot jobs through the real Controller, agent and helper.

The serving canary proves a workload that listens. This proves the other kind:
a job Recipe whose process writes a file and exits. It is the synthetic
canary's own build, model and base image with one more file, a stub job at the
job harness's executable path, so it needs no GPU, model download or new
external image.

Two jobs on one run must succeed with their result downloadable and leave no
container behind; one job that exits nonzero must end failed with the process's
own stderr in its stored evidence, never stuck or with its scope unproven.
Every artifact job failed on hardware through boundary bugs that each side's
own tests could not see, such as a job order queued without the manifest its
grant needs (#1569) and a detached launch whose container id was dropped
(#1576).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping

from scripts.development_slice_client import Client, require_object

#: The stub job is installed where the job harness executes its launcher.
JOB_EXECUTABLE = "/opt/vonk/bin/diffusers-job"
JOB_SCRIPT_NAME = "acceptance_job.py"
RESULT_NAME = "result.txt"
RESULT_BYTES = b"vonk acceptance artifact job ok\n"
FAILURE_EXIT_CODE = 3
FAILURE_MARKER = "vonk-acceptance-job: requested failure"
#: Long enough for a job claim, a container start and the result upload.
JOB_SECONDS = 300
#: The helper removes a job container when its Start sees the exit.
CONTAINER_GONE_SECONDS = 60

JOB_SCRIPT = f'''#!/usr/bin/env python3
"""Acceptance artifact job: one text result, or the requested failing exit."""

import argparse
import pathlib
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--exit-code", type=int, required=True)
parser.add_argument("--output-dir", type=pathlib.Path, required=True)
arguments = parser.parse_args()
if arguments.exit_code:
    print({FAILURE_MARKER!r}, file=sys.stderr, flush=True)
    sys.exit(arguments.exit_code)
(arguments.output_dir / {RESULT_NAME!r}).write_bytes({RESULT_BYTES!r})
'''.encode()

_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class ArtifactJobFailure(RuntimeError):
    """An artifact job that did not end as the acceptance requires."""


def job_recipe(recipe: dict[str, object]) -> None:
    """Turn the canary Recipe into an artifact job Recipe of the same build."""

    identity: dict[str, object] = recipe["identity"]  # type: ignore[assignment]
    metadata: dict[str, object] = recipe["metadata"]  # type: ignore[assignment]
    runtime: dict[str, object] = recipe["runtime"]  # type: ignore[assignment]
    build = recipe["execution"]["build"]  # type: ignore[index]
    identity["slug"] = f"{identity['slug']}-artifact-job"
    metadata["title"] = f"{metadata['title']} (artifact job)"
    runtime["engine"] = "diffusers"
    runtime["entrypoint"] = [JOB_EXECUTABLE]
    # A Recipe knob is a parameter each job may set: the stub's exit code.
    runtime["arguments"] = [
        {"name": "exit-code", "value": None, "setting": "exit_code"}
    ]
    recipe["settings"] = {
        "kind": "job",
        "concurrency": None,
        "knobs": {"exit_code": {"value": 0, "change_effect": "none"}},
    }
    recipe["interfaces"] = [
        {
            "adapter": "artifact-job",
            "output": {
                "max_total_bytes": 4096,
                "slots": [
                    {
                        "id": "result",
                        "label": "Result",
                        "description": "The stub job's text result.",
                        "media_types": ["text/plain"],
                        "extensions": [".txt"],
                        "min_files": 1,
                        "max_files": 1,
                        "max_file_bytes": 4096,
                        "max_total_bytes": 4096,
                    }
                ],
            },
        }
    ]
    recipe["validation"] = {
        "serving": {
            "interface": "artifact-job",
            "checks": [
                {
                    "name": "job-output",
                    "kind": "artifact-job.output",
                    "request": {
                        "transport": "job",
                        # A job without inputs stages nothing; the fixture
                        # only has to be a file the package carries.
                        "fixture": f"{build['context']['path']}/{JOB_SCRIPT_NAME}",
                        "input_slots": {},
                        "output_slot": "result",
                    },
                    "assertions": ["inference.completed", "artifact.output"],
                }
            ],
        }
    }


def job_build_files(
    recipe: Mapping[str, object],
) -> dict[str, Callable[[bytes], bytes]]:
    """The build context gains the stub job, installed by the Dockerfile."""

    build = recipe["execution"]["build"]  # type: ignore[index]
    context = build["context"]["path"]  # type: ignore[index]

    def install_job(dockerfile: bytes) -> bytes:
        user = b"\nUSER "
        if dockerfile.count(user) != 1:
            raise ArtifactJobFailure("the canary Dockerfile no longer ends as a user")
        copy = f"\nCOPY --chmod=0555 {JOB_SCRIPT_NAME} {JOB_EXECUTABLE}".encode()
        return dockerfile.replace(user, copy + user)

    return {
        build["dockerfile"]: install_job,  # type: ignore[index]
        f"{context}/{JOB_SCRIPT_NAME}": lambda _absent: JOB_SCRIPT,
    }


def run_artifact_jobs(
    control: Client,
    *,
    run_id: str,
    request_key: Callable[[str], str],
    container_names: Callable[[], set[str]],
) -> None:
    """Two jobs succeed on one run, one fails with evidence; none leaves a container."""

    for stage in ("job-first", "job-second"):
        _succeeded_job(control, run_id, request_key, container_names, stage)
    _failed_job(control, run_id, request_key, container_names)


def _succeeded_job(
    control: Client,
    run_id: str,
    request_key: Callable[[str], str],
    container_names: Callable[[], set[str]],
    stage: str,
) -> None:
    job = _run_job(control, run_id, request_key, stage, exit_code=0)
    job_id = str(job["id"])
    outputs = job.get("output_files")
    if job.get("state") != "succeeded" or not isinstance(outputs, list):
        raise ArtifactJobFailure(f"artifact job {stage} did not succeed: {_view(job)}")
    expected = {
        "name": RESULT_NAME,
        "media_type": "text/plain",
        "size_bytes": len(RESULT_BYTES),
        "sha256": hashlib.sha256(RESULT_BYTES).hexdigest(),
    }
    if outputs != [expected]:
        raise ArtifactJobFailure(f"artifact job {stage} result is not the stub's")
    _, body = control.request(
        "GET",
        f"/api/artifact-jobs/{job_id}/results/{RESULT_NAME}/{expected['sha256']}",
        allowed=(200,),
        decode=False,
    )
    if body != RESULT_BYTES:
        raise ArtifactJobFailure(f"artifact job {stage} result download differs")
    _await_container_gone(job_id, container_names)


def _failed_job(
    control: Client,
    run_id: str,
    request_key: Callable[[str], str],
    container_names: Callable[[], set[str]],
) -> None:
    job = _run_job(control, run_id, request_key, "job-failing", FAILURE_EXIT_CODE)
    job_id = str(job["id"])
    evidence = job.get("result_evidence") or {}
    if (
        job.get("state") != "failed"
        or job.get("output_files") != []
        or not isinstance(evidence, dict)
        # A process that ran and exited is a known end, never a scope in doubt.
        or evidence.get("failure_kind") is not None
        or evidence.get("active_scope_may_remain") is True
    ):
        raise ArtifactJobFailure(
            f"failing artifact job did not end failed: {_view(job)}"
        )
    # The job's order is one Controller job whose Spark child ran the process.
    # A deterministic exit is never retried, so its evidence is attempt 1's.
    _, bundle = control.request(
        "GET",
        f"/api/operations/{job.get('operation_id')}/evidence",
        query={"attempt": 1},
    )
    diagnostics = require_object(
        require_object(bundle, "failing artifact job evidence").get("diagnostics"),
        "failing artifact job diagnostics",
    )
    stderr = require_object(diagnostics.get("stderr"), "failing artifact job stderr")
    if FAILURE_MARKER not in str(stderr.get("text")):
        raise ArtifactJobFailure(
            "the failing job's stderr did not reach its stored evidence"
        )
    _await_container_gone(job_id, container_names)


def _run_job(
    control: Client,
    run_id: str,
    request_key: Callable[[str], str],
    stage: str,
    exit_code: int,
) -> dict[str, object]:
    """Create, finalize and submit one job as an operator does; follow it to its end."""

    _, created = control.request(
        "POST",
        f"/api/recipe/runs/{run_id}/artifact-jobs",
        {
            "interface": "artifact-job",
            "parameters": {"exit_code": exit_code},
            "inputs": [],
            "output_limits": {
                "max_files": 1,
                "max_file_bytes": 4096,
                "max_total_bytes": 4096,
                "allowed_media_types": ["text/plain"],
            },
            "timeout_seconds": JOB_SECONDS,
        },
        headers={"X-Request-ID": request_key(f"{stage}-create")},
        allowed=(200, 201),
    )
    job_id = require_object(created, f"artifact job {stage}").get("id")
    control.request("POST", f"/api/artifact-jobs/{job_id}/finalize", allowed=(200,))
    _, submitted = control.request(
        "POST",
        f"/api/artifact-jobs/{job_id}/submit",
        headers={"X-Request-ID": request_key(f"{stage}-submit")},
        allowed=(200, 202),
    )
    job = require_object(submitted, f"artifact job {stage}")
    deadline = time.monotonic() + JOB_SECONDS
    while job.get("state") not in _TERMINAL:
        if time.monotonic() >= deadline:
            raise ArtifactJobFailure(f"artifact job {stage} did not end: {_view(job)}")
        time.sleep(1)
        _, payload = control.request("GET", f"/api/artifact-jobs/{job_id}")
        job = require_object(payload, f"artifact job {stage}")
    return job


def _await_container_gone(job_id: str, container_names: Callable[[], set[str]]) -> None:
    """A job's container is named by its job id; it must not outlive the job."""

    name = f"vonk-{job_id}"
    deadline = time.monotonic() + CONTAINER_GONE_SECONDS
    while name in container_names():
        if time.monotonic() >= deadline:
            raise ArtifactJobFailure(f"artifact job container {name} remains")
        time.sleep(1)


def _view(job: Mapping[str, object]) -> str:
    return str(
        {
            key: job.get(key)
            for key in (
                "id",
                "state",
                "status_reason",
                "result_evidence",
                "output_files",
                "supported_actions",
            )
        }
    )[:1500]
