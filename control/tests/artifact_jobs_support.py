from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import TypedDict

from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    RecipeJobFile,
    RecipeJobInputRequest,
    RecipeJobOutputLimits,
    RecipeJobRunRequest,
    RecipeJobRunResult,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control import artifact_job_states as ajs
from vonk_control.agent_jobs import (
    AgentJobService,
    controller_result_consumer,
)
from vonk_control.artifact_blob_store import (
    ArtifactBlobStore,
)
from vonk_control.artifact_jobs import (
    ArtifactJobService,
)
from vonk_control.compiled_artifact_contract import (
    ParameterScalar,
)
from vonk_control.models import (
    AgentOperation,
)

from .runtime_identity_support import claim_agent
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
)


def _cancelling(view) -> bool:
    """A cancel was requested and the job has not ended: it completes by itself."""

    return view.cancel_requested_at is not None and view.state not in ajs.ENDED


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


def running_two_rank_artifact_service(tmp_path):
    sessions, recipe_operations, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, recipe_transform=_configure_artifact_recipe
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
    service = ArtifactJobService(
        sessions,
        recipe_operations=recipe_operations,
        blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
        clock=lambda: NOW,
    )
    return sessions, recipe_operations, service, activated.owner_id, nodes


def _two_rank_claims(tmp_path, suffix: int):
    """A submitted job on a two-Spark run, with both Sparks' claimed orders."""

    sessions, recipe_operations, service, run_id, nodes = (
        running_two_rank_artifact_service(tmp_path)
    )
    clock = MutableClock(NOW)
    agent_jobs = AgentJobService(sessions, clock=clock)
    agent_jobs.set_result_consumer(
        controller_result_consumer(
            artifact_jobs=service.consume_agent_result,
            recipe_operations=recipe_operations.consume_agent_result,
        )
    )
    recipe_operations._agent_jobs = agent_jobs
    submitted = submitted_artifact_job(service, run_id, request_suffix=suffix)
    claims = {
        node_id: claim_agent(agent_jobs, node_id, f"serial-{index}")
        for index, node_id in enumerate(nodes)
    }
    assert all(claim is not None for claim in claims.values())
    with sessions() as session:
        ranks = {
            order.node_id: RecipeJobRunRequest.model_validate(
                order.payload
            ).compiled_execution_plan.runtime.placement.rank
            for order in session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == submitted.operation_id
                )
            )
        }
    owner_node = next(node for node, rank in ranks.items() if rank == 0)
    companion_node = next(node for node in nodes if node != owner_node)
    owner_claim = claims[owner_node]
    companion_claim = claims[companion_node]
    assert owner_claim is not None and companion_claim is not None
    return (
        sessions,
        recipe_operations,
        service,
        agent_jobs,
        run_id,
        submitted,
        (owner_node, owner_claim),
        (companion_node, companion_claim),
        nodes,
    )
