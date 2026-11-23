"""Shared helpers for the artifact_jobs tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import TypedDict

from vonk_agent_protocol.contracts import AgentResult
from vonk_agent_protocol.lifecycle_vocabulary import AgentResultState
from vonk_agent_protocol.recipe_jobs import (
    RecipeJobFile,
    RecipeJobInputRequest,
    RecipeJobOutputLimits,
    RecipeJobRunResult,
)
from vonk_agent_protocol.recipe_jobs import (
    manifest_document as recipe_job_manifest_document,
)
from vonk_agent_protocol.recipe_jobs import (
    manifest_sha256 as recipe_job_manifest_sha256,
)
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.artifact_jobs import states as ajs
from vonk_control.artifact_jobs.blob_store import (
    ArtifactBlobStore,
)
from vonk_control.artifact_jobs.service import ArtifactJobService
from vonk_control.compiled_artifact_contract import (
    ParameterScalar,
)

from .recipe_operations_support import NOW, installed_recipe, setup_services


def _cancelling(view) -> bool:
    """A cancel was requested and the job has not ended: it completes by itself."""

    return view.cancel_requested_at is not None and view.state not in ajs.ENDED


def _agent_queue(sessions, recipe_operations, service) -> AgentJobService:
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    recipe_operations._agent_jobs = agent_jobs
    return agent_jobs


class _ArtifactCreateRequest(TypedDict):
    """The keyword arguments :meth:`ArtifactJobService.create` accepts."""

    run_id: str
    interface: str
    parameters: dict[str, ParameterScalar]
    inputs: list[dict[str, object]]
    output_limits: dict[str, object]
    timeout_seconds: int
    actor: str
    request_id: str


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _configure_artifact_recipe(document: dict[str, object]) -> None:
    document["settings"] = {
        "kind": "job",
        "concurrency": None,
        "knobs": {},
    }
    document["validation"] = {
        "serving": {
            "interface": "image-job",
            "checks": [
                {
                    "name": "image-job-output",
                    "kind": "image-job.output",
                    "assertions": ["inference.completed", "artifact.output"],
                    "request": {
                        "transport": "job",
                        "fixture": "fixtures/input.png",
                        "input_slots": {},
                        "output_slot": "image",
                    },
                }
            ],
        },
    }
    document["interfaces"] = [
        {
            "adapter": "image-job",
            "input": {
                "required": True,
                "media_types": ["image/png"],
                "max_bytes": 32 * 1024**2,
            },
            "output": {
                "max_total_bytes": 4096,
                "slots": [
                    {
                        "id": "image",
                        "label": "Image",
                        "description": "Generated image",
                        "media_types": ["image/png"],
                        "extensions": [".png"],
                        "min_files": 1,
                        "max_files": 1,
                        "max_file_bytes": 1024,
                        "max_total_bytes": 4096,
                    }
                ],
            },
        }
    ]
    runtime = _mapping(document["runtime"])
    arguments = _sequence(runtime["arguments"])
    arguments.extend(
        [
            {"name": "prompt", "value": None, "setting": "prompt"},
            {"name": "seed", "value": None, "setting": "seed"},
        ]
    )
    document["settings"]["knobs"] = {
        "prompt": {"value": "", "change_effect": "restart"},
        "seed": {"value": 0, "change_effect": "restart"},
    }


def running_artifact_service(tmp_path, *, recipe_transform=None, engine=None):
    def transform(document: dict[str, object]) -> None:
        _configure_artifact_recipe(document)
        if recipe_transform is not None:
            recipe_transform(document)

    sessions, recipe_operations, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, recipe_transform=transform, engine=engine
    )
    installed = installed_recipe(
        recipe_operations,
        mapping_id,
        build_id,
        nodes,
        request_id="00000000-0000-4000-8000-000000000101",
    )
    run_plan = recipe_operations.preview_run(installed.owner_id, "image-job")
    activated = recipe_operations.activate_job_run(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000102",
    )
    return (
        sessions,
        recipe_operations,
        queue,
        ArtifactJobService(
            sessions,
            recipe_operations=recipe_operations,
            blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
            clock=lambda: NOW,
        ),
        activated.owner_id,
        nodes[0],
    )


def artifact_create_request(run_id: str, request_id: str) -> _ArtifactCreateRequest:
    content = b"png"
    return {
        "run_id": run_id,
        "interface": "image-job",
        "parameters": {"prompt": "fox", "seed": 0},
        "inputs": [
            {
                "slot": "input",
                "name": "input.png",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
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
        "request_id": request_id,
    }


def create_artifact_job(service: ArtifactJobService, *args, **request):
    """Create a job from plain request documents, as the HTTP route does.

    The service takes typed inputs and limits; a test describes them as plain
    documents, so the contract models are built (and validated) here.
    """

    if "inputs" in request:
        request["inputs"] = [
            item
            if isinstance(item, RecipeJobInputRequest)
            else RecipeJobInputRequest.model_validate(item)
            for item in request["inputs"]
        ]
    if "output_limits" in request and not isinstance(
        request["output_limits"], RecipeJobOutputLimits
    ):
        request["output_limits"] = RecipeJobOutputLimits.model_validate(
            request["output_limits"]
        )
    return service.create(*args, **request)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def finalized_artifact_job(
    service: ArtifactJobService,
    run_id: str,
    *,
    request_suffix: int,
    output_limits: RecipeJobOutputLimits | None = None,
):
    request = artifact_create_request(
        run_id, f"00000000-0000-4000-8000-{request_suffix:012d}"
    )
    if output_limits is not None:
        request["output_limits"] = output_limits.model_dump(mode="json")
    job = create_artifact_job(service, **request)
    content = b"png"
    service.put_input(
        job.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    return service.finalize(job.id)


def submitted_artifact_job(
    service: ArtifactJobService,
    run_id: str,
    *,
    request_suffix: int,
    output_limits: RecipeJobOutputLimits | None = None,
):
    job = finalized_artifact_job(
        service, run_id, request_suffix=request_suffix, output_limits=output_limits
    )
    return service.submit(
        job.id,
        actor="operator",
        request_id=f"00000000-0000-4000-8000-{request_suffix + 1:012d}",
    )


def cancellation_result(
    claim,
    artifact_job,
    *,
    state: str,
    reason: str,
) -> AgentResult:
    empty: tuple[RecipeJobFile, ...] = ()
    return AgentResult(
        fence=claim.fence,
        state=AgentResultState(state),
        result=RecipeJobRunResult.model_validate(
            {
                "job_id": artifact_job.id,
                "run_id": artifact_job.run_id,
                "exit_code": 130,
                "output_manifest": {
                    **recipe_job_manifest_document(empty),
                    "manifest_sha256": recipe_job_manifest_sha256(empty),
                },
                "evidence": {
                    "elapsed_milliseconds": 10,
                    "peak_memory_bytes": None,
                },
                "reason": reason,
            }
        ),
    )
