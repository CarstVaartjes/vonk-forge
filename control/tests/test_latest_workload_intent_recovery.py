"""A newer profile intent cancels issued work before starting its replacement."""

from __future__ import annotations

from dataclasses import fields
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from vonk_agent_protocol import AgentResult
from vonk_agent_protocol.runtime_preflight import RuntimePreflightRequest
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.inventory_repository import (
    MAX_INVENTORY_FUTURE_SKEW,
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    Job,
    NodeInventorySnapshot,
    RecipeRun,
)
from vonk_control.run_switch_operations import (
    RecipeLifecyclePhaseExecutor,
    RunSwitchOperationService,
)
from vonk_control.runtime_preflight import mandatory_capabilities, request_digest

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_fleet_profiles import _uuid
from .test_recipe_operations import installed_recipe, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


def _result(claim, *, state: str, evidence: dict[str, object]) -> AgentResult:
    return AgentResult.model_validate(
        {
            "schema_version": 1,
            "job_id": claim.job_id,
            "operation_id": claim.operation_id,
            "attempt": claim.attempt,
            "fence": claim.fence,
            "node_id": claim.node_id,
            "deadline": claim.deadline,
            "state": state,
            "result": evidence,
        }
    )


def test_new_profile_cancels_issued_start_then_stops_before_replacement(
    tmp_path: Path, postgres_engine
) -> None:
    engine = create_engine(postgres_engine.url.render_as_string(hide_password=False))
    try:
        sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
            tmp_path, engine=engine
        )
        node_id = nodes[0]
        fingerprint = "a" * 64
        agent_capabilities = (
            "agent.runtime.rust.v1",
            "runtime.vonk.v1",
            "recipe.install",
            "recipe.start",
            "recipe.stop",
            "runtime.preflight.v1",
            f"runtime.preflight.fingerprint.{fingerprint}",
        )
        with sessions.begin() as session:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.capabilities = sorted(set(node.capabilities) | set(agent_capabilities))
        agent_jobs = AgentJobService(sessions, clock=lifecycle._clock)
        agent_jobs.set_result_consumer(lifecycle.consume_agent_result)
        lifecycle._agent_jobs = agent_jobs
        installed = installed_recipe(
            lifecycle, mapping_id, build_id, nodes, request_id=_uuid(940)
        )
        old_plan = lifecycle.preview_run(installed.owner_id, "old-chat")
        old_start = lifecycle.start(
            old_plan,
            plan_digest=old_plan.plan_digest,
            actor="admin",
            request_id=_uuid(941),
        )
        identity = {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
        old_claim = claim_agent(
            agent_jobs,
            node_id,
            "serial-0",
            30,
            capabilities=agent_capabilities,
            runtime_identity=identity,
        )
        assert old_claim is not None and old_claim.job_id == old_start.id

        with sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
        assert revision is not None
        switch = RunSwitchOperationService(
            sessions,
            lifecycle=lifecycle,
            clock=lifecycle._clock,
            artifacts=CompleteArtifactInspector(),
            artifact_phase_executor=RecordingArtifactExecutor(),
            memory_floor_bytes=50,
        )
        profiles = build_production_fleet_profile_service(
            sessions, clock=lifecycle._clock, run_switch_operations=switch
        )
        profile = profiles.create(
            FleetProfileInput.model_validate(
                {
                    "name": "Replace issued start",
                    "assignments": [
                        {
                            "recipe_selector": f"vonk-forge/{revision.slug}",
                            "spark_ids": list(nodes),
                            "desired_state": "running",
                            "assignment_name": "new-chat",
                        }
                    ],
                }
            ),
            actor="admin",
        )
        preview = profiles.preview(profile.id)
        assert preview.allowed, preview.reasons
        application = profiles.apply(
            profile.id,
            plan_digest=preview.plan_digest,
            request_key=_uuid(942),
            actor="admin",
        )
        cancellation = agent_jobs.heartbeat(old_claim, None, 30)
        assert cancellation.cancel_requested
        agent_jobs.record_result(
            _result(
                old_claim,
                state="cancelled",
                evidence={
                    "error_code": "operation_cancelled",
                    "reason": "exact old start stopped",
                },
            )
        )
        with sessions() as session:
            old_run = session.get(RecipeRun, old_start.owner_id)
            old_job = session.get(Job, old_start.id)
            assert old_run is not None and old_job is not None
            assert old_run.state == "lost"
            assert old_run.route_state == "withdrawn"
            assert old_job.state == "cancelled"

        # The new intent must issue an exact stop for the uncertain old run.
        stop_job = None
        for _ in range(24):
            profiles.tick()
            switch.tick()
            with sessions() as session:
                stop_job = session.scalar(select(Job).where(Job.kind == "recipe.stop"))
            if stop_job is not None:
                break
        assert stop_job is not None, profiles.application(application.id).status_reason
        stop_claim = claim_agent(
            agent_jobs,
            node_id,
            "serial-0",
            30,
            capabilities=agent_capabilities,
            runtime_identity=identity,
        )
        assert stop_claim is not None and stop_claim.job_id == stop_job.id
        agent_jobs.record_result(
            _result(stop_claim, state="succeeded", evidence={"stopped": True})
        )
        with sessions() as session:
            old_run = session.get(RecipeRun, old_start.owner_id)
            assert old_run is not None and old_run.state == "stopped"
            stopped_at = old_run.stopped_at
            snapshot = session.scalar(
                select(NodeInventorySnapshot).where(
                    NodeInventorySnapshot.node_id == node_id
                )
            )
            assert snapshot is not None
            assert (
                session.scalar(
                    select(Job).where(
                        Job.kind == "recipe.start", Job.id != old_start.id
                    )
                )
                is None
            )
        assert stopped_at is not None

        # The stop releases ownership, but only new physical evidence can show
        # that its memory is actually free. Keep the replacement unqueued until
        # a sample clears the stop timestamp and admitted clock skew.
        fresh_at = stopped_at + MAX_INVENTORY_FUTURE_SKEW + timedelta(seconds=1)
        fresh_clock = lambda: fresh_at
        lifecycle._clock = fresh_clock
        agent_jobs._clock = fresh_clock
        switch._clock = fresh_clock
        profiles._clock = fresh_clock
        phase_executor = switch._phase_executor
        assert isinstance(phase_executor, RecipeLifecyclePhaseExecutor)
        phase_executor._clock = fresh_clock
        inventory = InventoryRepository(sessions, clock=fresh_clock)
        inventory_values = {
            item.name: getattr(snapshot, item.name)
            for item in fields(InventorySnapshotInput)
        }
        inventory_values["observed_at"] = fresh_at
        inventory.record(InventorySnapshotInput(**inventory_values))

        # A fresh start can now be claimed. No old job is replayed to the node.
        replacement = None
        for _ in range(32):
            profiles.tick()
            switch.tick()
            with sessions() as session:
                replacement = session.scalar(
                    select(Job).where(
                        Job.kind == "recipe.start", Job.id != old_start.id
                    )
                )
                preflight = session.scalar(
                    select(Job).where(
                        Job.kind == "runtime.preflight.v1",
                        Job.state.in_(("queued", "running")),
                    )
                )
            if replacement is not None:
                break
            if preflight is not None:
                probe = RuntimePreflightRequest.model_validate(preflight.payload)
                preflight_claim = claim_agent(
                    agent_jobs,
                    node_id,
                    "serial-0",
                    30,
                    capabilities=agent_capabilities,
                    runtime_identity=identity,
                )
                assert preflight_claim is not None
                assert preflight_claim.job_id == preflight.id
                agent_jobs.record_result(
                    _result(
                        preflight_claim,
                        state="succeeded",
                        evidence={
                            "schema_version": 1,
                            "fingerprint": fingerprint,
                            "request_sha256": request_digest(probe),
                            "observed_at": int(lifecycle._clock().timestamp()),
                            "duration_ms": 1,
                            "cached": False,
                            "findings": [
                                {
                                    "capability": capability,
                                    "status": "passed",
                                    "code": "available",
                                }
                                for capability in mandatory_capabilities(probe)
                            ],
                        },
                    )
                )
        assert replacement is not None, profiles.application(
            application.id
        ).status_reason
        new_claim = claim_agent(
            agent_jobs,
            node_id,
            "serial-0",
            30,
            capabilities=agent_capabilities,
            runtime_identity=identity,
        )
        assert new_claim is not None and new_claim.job_id == replacement.id
        assert new_claim.operation_id != old_claim.operation_id
    finally:
        engine.dispose()
