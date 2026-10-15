"""A job submitted to a profile-activated job run gets its exact helper grants.

The profile load activates the job recipe (``recipe.job.activate.v1``); an
artifact job is created, submitted and claimed by the Spark's agent, and the
agent asks the Controller to sign the host-runtime Start (and its own Stop)
of exactly that job's container.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    ContainerRuntimeAction,
    InvalidRequestReason,
    RecipeJobFile,
    SignedHostHelperGrant,
    canonical_message,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_agent_protocol.host_helper import ExecuteContainerRuntimeRequestOperation
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_control.agent_api.authority import issue_host_runtime_grant
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_jobs.retry import superseded_cancellation_deadline
from vonk_control.agent_operation_facts import SUPERSEDED_CANCELLATION_SECONDS
from vonk_control.artifact_blob_store import ArtifactBlobStore
from vonk_control.artifact_jobs import ArtifactJobService
from vonk_control.host_helper_authority import (
    HostHelperAuthorityError,
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
    HostRuntimeGrantUnsatisfiable,
)
from vonk_control.models import AgentNode, Job, RecipeRun
from vonk_control.stored_json import read_row_column

from .agent_grant_requests import job_run_grant_request
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


def _grant(
    authority: HostRuntimeAuthorityService,
    node_id: str,
    fence: str,
    plan: RecipeJobRunRequest,
    action: ContainerRuntimeAction,
) -> SignedHostHelperGrant:
    """The agent asks, through the route, for its Start or its one Stop."""
    return issue_host_runtime_grant(
        authority,
        job_run_grant_request(plan, fence=fence, action=action),
        node_id=node_id,
        certificate_serial="serial-0",
    )


def _grant_start(
    authority: HostRuntimeAuthorityService,
    node_id: str,
    fence: str,
    plan: RecipeJobRunRequest,
) -> SignedHostHelperGrant:
    return _grant(authority, node_id, fence, plan, ContainerRuntimeAction.START)


def _grant_stop(
    authority: HostRuntimeAuthorityService,
    node_id: str,
    fence: str,
    plan: RecipeJobRunRequest,
) -> SignedHostHelperGrant:
    """The agent's one Stop: for cleanup after a failed run and cancellation."""
    return _grant(authority, node_id, fence, plan, ContainerRuntimeAction.STOP)


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

        # Cleanup after a failed run: no cancellation, the agent's own Stop.
        stop = _grant_stop(authority, spark[0], claim.fence, plan).claims.operation
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

        # Cancelled mid-run: the same agent Stop is granted under cancellation.
        jobs.cancel(
            later.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000740",
            reason="operator cancelled",
        )
        with world.sessions() as session:
            order = session.get(Job, later.operation_id)
            assert order is not None
            assert superseded_cancellation_deadline(order.result) is not None
        later_stop = _grant_stop(
            authority, spark[0], later_claim.fence, later_plan
        ).claims.operation
        assert isinstance(later_stop, ExecuteContainerRuntimeRequestOperation)
        assert later_stop.runtime_target_id == later.id

        # A slow agent, still heartbeating, past the cancellation window: its
        # cleanup Stop of its own job container is still granted; a Start is not.
        for _ in range(SUPERSEDED_CANCELLATION_SECONDS // 20 + 1):
            world.advance(20)
            orders.heartbeat(later_claim, None, 60)
            world.tick()
        late_stop = _grant_stop(
            authority, spark[0], later_claim.fence, later_plan
        ).claims.operation
        assert isinstance(late_stop, ExecuteContainerRuntimeRequestOperation)
        assert late_stop.runtime_target_id == later.id
        with pytest.raises(HostHelperAuthorityError):
            _grant_start(authority, spark[0], later_claim.fence, later_plan)
    finally:
        world.close()


def test_a_job_whose_parent_lacks_its_manifest_is_refused_definitely_at_once(
    tmp_path: Path, postgres_engine
) -> None:
    # An order queued before its producer recorded the manifest (the upgrade
    # case): the stored parent is self-consistent but names no child, so the
    # job's Start can never be granted. The refusal is typed and definite on
    # the first ask, never a wait.
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
        assert sim.settle(_load(world, member)) == "succeeded"
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
            jobs.consume_agent_result(session, operation, attempt, message)
            world.controller.lifecycle.consume_agent_result(
                session, operation, attempt, message
            )

        orders.set_result_consumer(consume)
        authority = HostRuntimeAuthorityService(
            world.sessions,
            HostHelperGrantIssuer(
                ed25519.Ed25519PrivateKey.from_private_bytes(bytes([23]) * 32),
                clock=world.clock,
            ),
            clock=world.clock,
        )
        submitted = submitted_artifact_job(jobs, run_id, request_suffix=740)
        with world.sessions.begin() as session:
            parent = session.get(Job, submitted.operation_id)
            assert parent is not None
            document = {
                key: value for key, value in parent.payload.items() if key != "phases"
            }
            parent.payload = document
            parent.payload_digest = _sha256(read_row_column(parent, "payload"))
        claim, plan = _claimed_job(world, orders, spark[0])

        with pytest.raises(HostRuntimeGrantUnsatisfiable) as refused:
            _grant_start(authority, spark[0], claim.fence, plan)
        assert refused.value.reason is InvalidRequestReason.INCOMPLETE

        # The agent ends the job with that refusal as its evidence (nothing
        # started), and the job is failed at once, not observed again.
        diagnostic = "http_status=409 error_code=incomplete"
        orders.record_result(
            AgentResult.model_validate(
                {
                    "fence": claim.fence,
                    "state": "failed",
                    "result": {
                        "kind": "failed",
                        "code": "recipe_job_run_failed",
                        "reason": "job start was refused by the Controller",
                        "failure_kind": "invalid-contract",
                        "evidence": {
                            "stage": "runtime-adapter",
                            "diagnostic": diagnostic,
                        },
                    },
                }
            )
        )
        failed = jobs.get(submitted.id)
        assert failed.state == "failed"
        assert diagnostic in (failed.status_reason or ""), failed.status_reason
        with world.sessions() as session:
            order = session.get(Job, submitted.operation_id)
            assert order is not None and order.state == "failed"
        assert (
            claim_agent(
                orders,
                spark[0],
                "serial-0",
                runtime_identity={
                    **PACKAGED_RUNTIME_IDENTITY,
                    "architecture": "linux-arm64",
                },
                preflight_fingerprint=None,
            )
            is None
        )
    finally:
        world.close()
