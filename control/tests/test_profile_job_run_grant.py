"""A job submitted to a profile-activated job run gets its exact helper grants.

The profile load activates the job recipe (``recipe.job.activate.v1``); an
artifact job is created, submitted and claimed by the Spark's agent, and the
agent asks the Controller to sign the host-runtime Start (and its own Stop)
of exactly that job's container.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    ContainerRuntimeAction,
    RecipeJobFile,
    SignedHostHelperGrant,
    canonical_message,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_agent_protocol.host_helper import (
    ExecuteContainerRuntimeRequestOperation,
    HostRuntimeRequest,
)
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_control.agent_jobs import AgentJobService
from vonk_control.artifact_blob_store import ArtifactBlobStore
from vonk_control.artifact_jobs import ArtifactJobService
from vonk_control.host_helper_authority import (
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.models import AgentNode, RecipeRun
from vonk_control.recipe_stop_payloads import stop_payload_from_job_run

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import SOLO, World, build_world
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_artifact_jobs import _configure_artifact_recipe, submitted_artifact_job
from .test_profile_job_member import _load


def _sha256(document: object) -> str:
    return hashlib.sha256(canonical_message(document)).hexdigest()


def _claimed_job(world: World, orders: AgentJobService, node_id: str):
    """The Spark's agent claims its next order, as its poll does."""
    with world.sessions() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        fingerprint = node.preflight_fingerprint
    claim = claim_agent(
        orders,
        node_id,
        "serial-0",
        runtime_identity={**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"},
        preflight_fingerprint=fingerprint,
    )
    assert claim is not None and claim.operation.value == "recipe.job.run.v1"
    plan = RecipeJobRunRequest.model_validate_json(canonical_message(claim.payload))
    return claim, plan


def _report_output(
    jobs: ArtifactJobService, orders: AgentJobService, node_id: str, claim, plan
) -> None:
    """The agent uploads the job's one output and reports it under its fence."""
    output = b"png-output"
    digest = hashlib.sha256(output).hexdigest()
    jobs.put_output(
        plan.job_id,
        node_id=node_id,
        name="output.png",
        media_type="image/png",
        expected_sha256=digest,
        content=output,
    )
    files = (
        RecipeJobFile(
            name="output.png",
            media_type="image/png",
            size_bytes=len(output),
            sha256=digest,
        ),
    )
    orders.record_result(
        AgentResult.model_validate(
            {
                "fence": claim.fence,
                "state": "succeeded",
                "result": {
                    "job_id": plan.job_id,
                    "run_id": plan.run_id,
                    "exit_code": 0,
                    "output_manifest": {
                        **recipe_job_manifest_document(files),
                        "manifest_sha256": recipe_job_manifest_sha256(files),
                    },
                    "evidence": {
                        "elapsed_milliseconds": 10,
                        "peak_memory_bytes": None,
                    },
                },
            }
        )
    )


def _grant_start(
    authority: HostRuntimeAuthorityService,
    node_id: str,
    fence: str,
    plan: RecipeJobRunRequest,
) -> SignedHostHelperGrant:
    """The agent asks for the Start of exactly this job's container."""
    request = HostRuntimeRequest(
        action="start",
        fence=fence,
        arguments=["run"],
        job_plan=plan,
        run_generation=plan.run_generation,
    )
    return authority.issue_grant(
        node_id=node_id,
        certificate_serial="serial-0",
        fence=fence,
        action=ContainerRuntimeAction.START,
        request_sha256=_sha256(request),
        start_plan_sha256=_sha256(plan),
        run_generation=plan.run_generation,
        runtime_run_id=plan.run_id,
        runtime_target_id=plan.job_id,
        runtime_installation_id=plan.installation_id,
    )


def test_a_claimed_job_on_a_profile_activated_run_is_granted_its_exact_start_and_stop(
    tmp_path: Path, postgres_engine
) -> None:
    world = build_world(
        tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
    )
    try:
        sim = Simulation(world, seed=0)
        sim.round()
        spark = world.nodes[:1]
        member, _number = world.create_profile(
            "Job member", [world.assignment(SOLO, spark)]
        )
        load = _load(world, member)
        assert sim.settle(load) == "succeeded"
        with world.sessions() as session:
            (run,) = session.scalars(
                select(RecipeRun).where(RecipeRun.state == "running")
            )
            run_id = run.id
        jobs = ArtifactJobService(
            world.sessions,
            recipe_operations=world.controller.lifecycle,
            blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
            clock=world.clock,
        )
        orders = AgentJobService(world.sessions, clock=world.clock)

        def consume(session, operation, attempt, message) -> None:
            # The Controller's production result wiring.
            jobs.consume_agent_result(session, operation, attempt, message)
            world.controller.lifecycle.consume_agent_result(
                session, operation, attempt, message
            )

        orders.set_result_consumer(consume)
        issuer = HostHelperGrantIssuer(
            ed25519.Ed25519PrivateKey.from_private_bytes(bytes([23]) * 32),
            clock=world.clock,
        )
        authority = HostRuntimeAuthorityService(
            world.sessions, issuer, clock=world.clock
        )

        submitted = submitted_artifact_job(jobs, run_id, request_suffix=720)
        claim, plan = _claimed_job(world, orders, spark[0])
        assert plan.job_id == submitted.id

        start = _grant_start(authority, spark[0], claim.fence, plan).claims.operation
        assert isinstance(start, ExecuteContainerRuntimeRequestOperation)
        assert (start.fence, start.start_plan_sha256) == (claim.fence, _sha256(plan))
        assert start.runtime_target_id == submitted.id

        stop_plan = stop_payload_from_job_run(plan, cancel_pending_start=False)
        stop_request = HostRuntimeRequest(
            action="stop",
            fence=claim.fence,
            arguments=[],
            stop_plan=stop_plan,
            run_generation=stop_plan.run_generation,
        )
        stop = authority.issue_grant(
            node_id=spark[0],
            certificate_serial="serial-0",
            fence=claim.fence,
            action=ContainerRuntimeAction.STOP,
            request_sha256=_sha256(stop_request),
            stop_plan_sha256=_sha256(stop_plan),
            run_generation=stop_plan.run_generation,
            runtime_run_id=stop_plan.run_id,
            runtime_target_id=stop_plan.target_runtime_id,
            runtime_installation_id=stop_plan.installation_id,
        ).claims.operation
        assert isinstance(stop, ExecuteContainerRuntimeRequestOperation)
        assert stop.runtime_target_id == submitted.id

        # The granted container ran; the agent's fenced result is accepted.
        _report_output(jobs, orders, spark[0], claim, plan)
        assert jobs.get(submitted.id).state == "succeeded"

        # The run keeps serving: the next job is granted its own Start.
        later = submitted_artifact_job(jobs, run_id, request_suffix=730)
        later_claim, later_plan = _claimed_job(world, orders, spark[0])
        assert later_plan.job_id == later.id
        later_start = _grant_start(
            authority, spark[0], later_claim.fence, later_plan
        ).claims.operation
        assert isinstance(later_start, ExecuteContainerRuntimeRequestOperation)
        assert later_start.runtime_target_id == later.id
    finally:
        world.close()
