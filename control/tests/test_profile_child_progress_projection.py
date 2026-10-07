"""Current-attempt measurements survive the Recipe → Run/Switch → profile seam."""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from vonk_agent_protocol import OperationProgress, canonical_message
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AgentPresence,
    CatalogDocumentRevision,
    Job,
)
from vonk_control.operation_progress import aggregate_progress, member_progress
from vonk_control.recipe_operations import RecipeOperationService, _stored_phases
from vonk_control.stored_json import write_guard_mode

from .preflight_fixtures import record_passing_preflight
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_profile_installed_execution import (
    _apply,
    _drive_to_job,
    _installed_profile,
    _profile_service,
)
from .test_recipe_operations import NOW, installed_recipe, setup_services


def test_wide_measurements_roundtrip_without_losing_integer_precision() -> None:
    completed = 2**80 + 7
    measured = OperationProgress(
        phase="copying",
        completed_bytes=completed,
        total_bytes=completed + 11,
        total_bytes_known=True,
    )
    projected = aggregate_progress(
        [
            member_progress(
                measured, member_id="measured-node", state="running", phase="copying"
            )
        ]
    )
    restored = OperationProgress.model_validate_json(
        canonical_message(projected), strict=True
    )
    assert restored.completed_bytes == completed
    assert restored.total_bytes == completed + 11
    assert restored.members[0].completed_bytes == completed


def test_live_agent_progress_reaches_recipe_switch_and_profile(
    tmp_path: Path, postgres_engine
) -> None:
    signer = Ed25519PrivateKey.generate()
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    with sessions.begin() as session:
        for node_id in nodes:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.capabilities = [*node.capabilities, "recipe.install"]
    jobs = AgentJobService(sessions, clock=lifecycle._clock)
    lifecycle._agent_jobs = jobs
    base_now = lifecycle._clock()
    service, planner = _profile_service(sessions, lifecycle)
    profile = _installed_profile(service, sessions, nodes)
    application = _apply(service, profile)
    install_id = _drive_to_job(service, planner, sessions, "recipe.install")

    claim = claim_agent(
        jobs,
        nodes[0],
        "serial-0",
        30,
        runtime_identity={
            **PACKAGED_RUNTIME_IDENTITY,
            "architecture": "linux-arm64",
            "observation_receipt_public_key": signer.public_key()
            .public_bytes_raw()
            .hex(),
        },
    )
    assert claim is not None
    jobs.heartbeat(
        claim,
        OperationProgress.model_validate_json(
            canonical_message(
                {
                    "phase": "copying",
                    "completed_bytes": 32,
                    "total_bytes": 64,
                    "total_bytes_known": True,
                    "members": [
                        {
                            "member_id": "copy-chunk-0",
                            "phase": "copying",
                            "completed_bytes": 32,
                            "total_bytes": 64,
                            "state": "running",
                        }
                    ],
                }
            ),
            strict=True,
        ),
        60,
    )
    jobs._clock = lambda: base_now + timedelta(seconds=2)
    jobs.heartbeat(
        claim,
        OperationProgress.model_validate_json(
            canonical_message(
                {
                    "phase": "copying",
                    "completed_bytes": 48,
                    "total_bytes": 64,
                    "total_bytes_known": True,
                    "members": [
                        {
                            "member_id": "copy-chunk-0",
                            "phase": "copying",
                            "completed_bytes": 48,
                            "total_bytes": 64,
                            "state": "running",
                        }
                    ],
                }
            ),
            strict=True,
        ),
        60,
    )

    observed_at = (base_now + timedelta(seconds=2)).isoformat()
    lifecycle._clock = lambda: base_now + timedelta(seconds=2)
    fresh = lifecycle.get(install_id)
    assert fresh.progress is not None
    assert fresh.progress.members[0].bytes_per_second is not None
    assert fresh.progress.members[0].eta_seconds is not None
    with sessions() as session:
        sample = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        assert sample is not None and sample.progress is not None
        persisted_progress = copy.deepcopy(sample.progress)

    lifecycle._clock = lambda: base_now + timedelta(seconds=60)
    # A new service reconnects only from the same durable rows.
    lifecycle = RecipeOperationService(
        sessions,
        agent_jobs=jobs,
        clock=lambda: base_now + timedelta(seconds=60),
    )
    recipe = lifecycle.get(install_id)
    assert recipe.progress is not None
    assert recipe.progress.completed_bytes == 48
    assert [member.member_id for member in recipe.progress.members] == list(nodes)
    assert recipe.progress.total_bytes is None
    assert not recipe.progress.total_bytes_known
    assert recipe.progress.eta_seconds is None
    assert recipe.progress.members[0].observed_at == observed_at
    assert recipe.progress.members[0].activity == "waiting"
    assert recipe.progress.members[0].bytes_per_second is None
    assert recipe.progress.members[0].smoothed_bytes_per_second is None
    assert recipe.progress.members[0].eta_seconds is None
    assert recipe.progress.members[0].total_bytes == 64
    assert recipe.progress.members[1].state == "queued"
    assert recipe.progress.members[1].completed_bytes == 0
    assert recipe.progress.members[1].total_bytes is None
    assert recipe.progress.members[1].observed_at is None
    assert recipe.progress.members[1].last_progress_at is None
    assert recipe.progress.members[1].activity == "waiting"
    with sessions() as session:
        sample = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        assert sample is not None and sample.progress == persisted_progress

    # The switch worker has its own clock; restore the lifecycle clock to that
    # test epoch before exercising the downstream read path.
    lifecycle._clock = lambda: base_now

    with sessions() as session:
        switch_job = session.scalar(
            select(Job).where(Job.kind == "recipe.run-switch.v2")
        )
        assert switch_job is not None
        switch_id = switch_job.id
    with sessions.begin() as session:
        switch_job = session.get(Job, switch_id)
        assert switch_job is not None and isinstance(switch_job.result, dict)
        switch_job.result = {
            **switch_job.result,
            "observation_due_at": lifecycle._clock().isoformat(),
        }
    assert planner.tick()
    switch = planner.get(switch_job.id)
    assert switch.progress is not None and switch.progress.operation is not None
    assert switch.progress.operation.completed_bytes == 48
    assert [member.member_id for member in switch.progress.operation.members] == list(
        nodes
    )
    assert switch.progress.operation.members[0].observed_at == observed_at
    assert switch.progress.operation.total_bytes is None
    assert switch.progress.operation.eta_seconds is None
    assert switch.progress.members[1].state == "pending"

    service.tick()
    profile_view = service.application(application.id)
    profile_progress = profile_view.progress.child_progress
    assert profile_progress is not None and profile_progress.operation is not None
    assert profile_progress.operation.completed_bytes == 48
    assert [member.member_id for member in profile_progress.operation.members] == list(
        nodes
    )
    assert profile_progress.operation.members[0].observed_at == observed_at
    assert profile_progress.operation.total_bytes is None
    assert profile_progress.operation.eta_seconds is None
    effects = [
        effect for effect in profile_view.progress.effects if effect.kind == "install"
    ]
    assert len(effects) == 1 and effects[0].progress is not None
    assert effects[0].progress.operation == profile_progress.operation


def test_disjoint_child_samples_remain_distinct_after_restart(
    tmp_path: Path, postgres_engine
) -> None:
    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    second = "spk_" + f"{2:032x}"
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=second,
                state="active",
                architecture="linux-arm64",
                protocol_version=2,
                last_seen_at=NOW,
            )
        )
        session.flush()
        session.add(
            AgentCertificate(
                serial="serial-b",
                node_id=second,
                fingerprint="fingerprint-b",
                not_before=NOW,
                not_after=NOW + timedelta(days=1),
            )
        )
        session.add(
            AgentPresence(
                node_id=second,
                certificate_serial="serial-b",
                certificate_fingerprint="fingerprint-b",
                management_address="192.168.1.212",
                observed_at=NOW,
            )
        )
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        selector = f"{revision.publisher}/{revision.slug}"
    InventoryRepository(sessions, clock=lambda: NOW).record(
        InventorySnapshotInput(
            second,
            NOW,
            10_000,
            8_000,
            10_000,
            8_000,
            10_000,
            8_000,
            1,
            False,
            ("runtime.vonk.v1", "recipe.image.pull.v1", "recipe.operations.v1"),
            memory_pool="shared",
        )
    )
    record_passing_preflight(sessions, NOW)
    all_nodes = (*nodes, second)
    with sessions.begin() as session:
        for node_id in all_nodes:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.capabilities = [*node.capabilities, "recipe.install"]
    now = [NOW]
    lifecycle._clock = lambda: now[0]
    jobs = AgentJobService(sessions, clock=lambda: now[0])
    lifecycle._agent_jobs = jobs
    service, planner = _profile_service(sessions, lifecycle)
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Independent measured lanes",
                "assignments": [
                    {
                        "recipe_selector": selector,
                        "spark_ids": [node],
                        "desired_state": "installed",
                    }
                    for node in all_nodes
                ],
            }
        ),
        actor="admin",
    )
    application = _apply(service, profile)
    install_ids: tuple[str, ...] = ()
    for _ in range(24):
        planner.tick()
        service.tick()
        with sessions() as session:
            install_ids = tuple(
                session.scalars(select(Job.id).where(Job.kind == "recipe.install"))
            )
        if len(install_ids) == 2:
            break
    assert len(install_ids) == 2
    measured = {all_nodes[0]: (32, 64), second: (16, 128)}
    claims = []
    for node, serial in zip(all_nodes, ("serial-0", "serial-b"), strict=True):
        key = Ed25519PrivateKey.generate()
        claim = claim_agent(
            jobs,
            node,
            serial,
            30,
            runtime_identity={
                **PACKAGED_RUNTIME_IDENTITY,
                "architecture": "linux-arm64",
                "observation_receipt_public_key": key.public_key()
                .public_bytes_raw()
                .hex(),
            },
        )
        assert claim is not None
        claims.append(claim)
        completed, total = measured[node]
        jobs.heartbeat(
            claim,
            OperationProgress(
                phase="copying",
                completed_bytes=completed,
                total_bytes=total,
                total_bytes_known=True,
            ),
            1200,
        )
    # Fresh owners observe the same accepted children and durable samples.
    recovered_lifecycle = RecipeOperationService(
        sessions, agent_jobs=jobs, clock=lambda: now[0]
    )
    recovered_service, recovered_planner = _profile_service(
        sessions, recovered_lifecycle
    )
    for _ in range(4):
        now[0] += timedelta(seconds=10)
        recovered_planner.tick()
        recovered_service.tick()
    with sessions() as session:
        original_samples = {}
        for claim in claims:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == claim.operation_id
                )
            )
            assert attempt is not None
            original_samples[claim.operation_id] = copy.deepcopy(attempt.progress)
    effects = [
        effect
        for effect in recovered_service.application(application.id).progress.effects
        if effect.kind == "install"
    ]
    assert len(effects) == 2
    for effect in effects:
        assert len(effect.node_ids) == 1
        assert effect.progress is not None and effect.progress.operation is not None
        progress = effect.progress.operation
        node = effect.node_ids[0]
        assert (progress.completed_bytes, progress.total_bytes) == measured[node]
        assert [member.member_id for member in progress.members] == [node]
        assert effect.request_key and effect.operation_id
    with sessions() as session:
        for claim in claims:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == claim.operation_id
                )
            )
            assert (
                attempt is not None
                and attempt.progress == original_samples[claim.operation_id]
            )


def test_future_start_role_is_queued_without_a_measurement(tmp_path: Path) -> None:
    sessions, service, _queue, mapping, build, nodes = setup_services(
        tmp_path,
        nodes=2,
        start_order=("worker", "entrypoint"),
    )
    installed = installed_recipe(
        service, mapping, build, nodes, request_id=str(uuid4())
    )
    plan = service.preview_run(installed.owner_id, "sequential-progress")
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
    )
    with sessions() as session:
        job = session.get(Job, start.id)
        assert job is not None
        phases = _stored_phases(job)
        assert not isinstance(phases, Residue) and len(phases) >= 2
        future_node = phases[1][0][1]
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        # Current native launch accepts a full prelaunch gang. The later
        # reviewed phase remains unissued, regardless of its role order.
        assert {child.id for child in children} == {
            identity for identity, _node, _payload in phases[0]
        }
        assert not {child.id for child in children} & {
            identity for group in phases[1:] for identity, _node, _payload in group
        }
    observed = service.get(start.id)
    assert observed.progress is not None
    member = next(
        item for item in observed.progress.members if item.member_id == future_node
    )
    assert member.state == "queued" and member.activity == "waiting"
    assert member.observed_at is None and member.last_progress_at is None
    assert member.total_bytes is None and member.bytes_per_second is None
    assert member.eta_seconds is None and member.elapsed_seconds is None


@pytest.mark.parametrize(
    "fault", ["unlisted-operation", "wrong-node", "duplicate-target"]
)
def test_damaged_membership_is_unknown_and_does_not_mutate_execution(
    tmp_path: Path, fault: str
) -> None:
    sessions, service, _queue, mapping, build, nodes = setup_services(
        tmp_path,
        nodes=2,
        distributed_lifecycle=True,
    )
    installed = installed_recipe(
        service, mapping, build, nodes, request_id=str(uuid4())
    )
    plan = service.preview_run(installed.owner_id, "measured-membership")
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
    )
    with write_guard_mode(strict=False), sessions.begin() as session:
        job = session.get(Job, start.id)
        assert job is not None
        damaged = copy.deepcopy(job.payload)
        if fault == "duplicate-target":
            job.targets = [*nodes, nodes[0]]
        elif fault == "unlisted-operation":
            damaged["phases"][0][0]["operation_id"] = str(uuid4())
        else:
            first = damaged["phases"][0]
            first[0]["node_id"], first[1]["node_id"] = (
                first[1]["node_id"],
                first[0]["node_id"],
            )
        job.payload = damaged
        original_state = job.state
    observed = service.get(start.id)
    assert observed.progress is None
    assert observed.id == start.id and observed.state == original_state
    with sessions() as session:
        job = session.get(Job, start.id)
        assert (
            job is not None and job.payload == damaged and job.state == original_state
        )
