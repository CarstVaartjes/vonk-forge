from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol import (
    AgentProgress,
    AgentResult,
    RecipeOperationRequest,
    canonical_message,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import canonical_payload
from vonk_control import agent_operation_states as aos
from vonk_control.agent_jobs import (
    AgentJobService,
    StaleAgentAttempt,
    _claim_predicate,
    authorize_operator_resume_in_session,
)
from vonk_control.distribution import (
    DistributionError,
    DistributionService,
    MemoryObjectSource,
)
from vonk_control.distribution_assignment import NodeDistributionAssignment
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.lifecycle import Outcome, Reported
from vonk_control.lifecycle.agent_operation import (
    AgentOperationAdapter,
    retry_scheduled,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
    NodeArtifact,
    RecipeBuild,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.recipe_start_payloads import (
    RecipeStartPlacement,
    _bind_compiled_execution_plan,
)
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_adapters import resolve_runtime_adapter

from .agent_fences import fenced_attempt, fenced_operation, park_for_operator
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    claim_agent,
)

_BUILD_ADAPTER = (
    resolve_runtime_adapter("vllm", {"node_count": 1}).to_wire().model_dump(mode="json")
)

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a" * 64
STOP_PAYLOAD = recipe_stop_payload(NODE_A, plan_digest=COMMIT)
STOP_RESULT: dict[str, object] = {}


def canonical_start_payload(*, start_deadline: datetime) -> dict[str, object]:
    """One wire-valid distributed rank-launch document.

    Built through the production binder, because the payload model cross-checks
    every placement field against the compiled plan: a hand-written document
    would prove nothing about the shape a queued start actually carries.  What
    this fixture pins is the field the renewal allowance consumes; the real
    producer that writes it onto a queued start operation keeps its own witness
    in ``test_recipe_operations``.
    """

    plan = copy.deepcopy(
        json.loads(
            (
                Path(__file__).parents[2]
                / "agent_protocol"
                / "tests"
                / "fixtures"
                / "compiled-execution-plan-v2.json"
            ).read_text(encoding="utf-8")
        )
    )
    plan["topology"].update(name="dual", node_count=2)
    compiled = _bind_compiled_execution_plan(
        plan,
        placement=RecipeStartPlacement(
            node_id=NODE_A,
            rank=0,
            role="entrypoint",
            port=8000,
            reserved_memory_bytes=4096,
            memory_floor_bytes=2048,
            memory_kind="unified",
            fabric_address="192.168.100.10",
        ),
        endpoint_address="192.168.100.10",
        master_address="192.168.100.10",
        master_port=29500,
        world_size=2,
    )
    payload: dict[str, object] = {
        "run_id": "00000000-0000-4000-8000-0000000000aa",
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "recipe_revision_id": "00000000-0000-4000-8000-0000000000bb",
        "mapping_id": "00000000-0000-4000-8000-0000000000cc",
        "run_generation": 1,
        "plan_digest": "b" * 64,
        "compiled_execution_plan": compiled,
        "phase": "rank-launch",
        "start_deadline": start_deadline.isoformat(),
    }
    RecipeOperationRequest.parse(ProtocolAgentOperation.RECIPE_START, payload)
    return payload


def canonical_start_result(payload: Mapping[str, object]) -> dict[str, object]:
    """A rank launch succeeds with an empty result."""

    del payload
    return {}


def canonical_install_payload() -> dict[str, object]:
    compiled_plan = json.loads(
        (
            Path(__file__).parents[2]
            / "agent_protocol"
            / "tests"
            / "fixtures"
            / "compiled-execution-plan-v2.json"
        ).read_text(encoding="utf-8")
    )
    payload = {
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "plan_digest": "b" * 64,
        "expected_bytes": compiled_plan["identity"]["model_artifact_bytes"],
        "compiled_execution_plan": compiled_plan,
    }
    RecipeOperationRequest.parse(ProtocolAgentOperation.RECIPE_INSTALL, payload)
    return payload


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class MonotonicClock:
    """A monotonic clock the test advances explicitly.

    A long poll measures its budget against a monotonic clock, so a CPU-starved
    scheduler must not be able to expire that budget between two database
    reads. Freezing the clock here is what makes the observation deterministic;
    the test advances it only to retire a poll that never observed the re-read.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# A hang guard, not a latency assertion: the frozen clock keeps a working poll
# alive until it observes the enqueue, and this bound only surfaces a broken
# re-read that would otherwise never observe anything.
_SCHEDULER_GUARD_SECONDS = 30.0


@pytest.fixture
def service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-jobs.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    clock = Clock()
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    workload_intent_ordinal=1,
                    architecture="linux-arm64",
                    semantic_version="1.0.0",
                    build_digest="sha256:" + "f" * 64,
                    binary_digest="f" * 64,
                )
            )
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=clock.now - timedelta(seconds=1),
                    not_after=clock.now + timedelta(hours=1),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    return AgentJobService(sessions, clock=clock), sessions, clock


def parent(sessions, clock) -> Job:
    payload = {"workload_intent_ordinal": 1}
    job = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_A, NODE_B],
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(job)
    return job


def job_state(sessions, job_id: str) -> Job:
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def test_agent_can_claim_only_its_node_operation(service) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )

    assert claim_agent(jobs, NODE_B, "serial-b") is None
    claim = claim_agent(jobs, NODE_A, "serial-a")

    assert claim is not None
    assert fenced_operation(sessions, claim).id == operation.id


def test_workload_enqueue_refuses_parent_without_an_admitted_intent(service) -> None:
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, job.id).payload = {}
    with pytest.raises(ValueError, match="requires a bound intent"):
        jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)


def test_newer_workload_intent_fences_old_enqueues_renewals_and_results(
    service,
) -> None:
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, job.id).payload = {"workload_intent_ordinal": 1}
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 2
    with pytest.raises(ValueError, match="superseded"):
        jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    with sessions.begin() as session:
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 1
    jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    with sessions.begin() as session:
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 2
    with pytest.raises(StaleAgentAttempt):
        jobs.heartbeat(claim, None, 30)
    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(claim, STOP_RESULT)


def test_new_intent_cancels_issued_order_and_receives_exact_stop_ack(service) -> None:
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    running = jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    queued = jobs.enqueue(
        job.id,
        NODE_B,
        "recipe.stop",
        COMMIT,
        recipe_stop_payload(NODE_B, plan_digest=COMMIT),
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    with sessions.begin() as session:
        for node_id in (NODE_A, NODE_B):
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.workload_intent_ordinal = 2
        jobs.request_superseded_workload_cancellation_in_session(
            session, (NODE_A, NODE_B), 2, clock.now
        )
    with sessions() as session:
        queued_row = session.get(AgentOperation, queued.id)
        running_row = session.get(AgentOperation, running.id)
        parent_row = session.get(Job, job.id)
        assert queued_row is not None and queued_row.state == "cancelled"
        assert running_row is not None and running_row.state == "running"
        assert parent_row is not None and parent_row.result is not None
        assert parent_row.result["cancel_requested"] is True
        pending = AgentJobService.assess_superseded_agent_effects_in_session(
            session, (NODE_A, NODE_B), 2, clock.now
        )
        assert tuple(item.operation_id for item in pending) == (running.id,)
        assert pending[0].failure_kind.value == "uncertain-effect"
    directive = jobs.heartbeat(claim, None, 30)
    assert directive.cancel_requested is True
    assert directive.deadline <= clock.now + timedelta(seconds=660)
    replacement = parent(sessions, clock)
    with sessions.begin() as session:
        replacement_row = session.get(Job, replacement.id)
        assert replacement_row is not None
        replacement_row.payload = {"workload_intent_ordinal": 2}
    fresh = jobs.enqueue(replacement.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    fresh_claim = claim_agent(jobs, NODE_A, "serial-a")
    assert (
        fresh_claim is not None
        and fenced_operation(sessions, fresh_claim).id == fresh.id
    )
    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(claim, STOP_RESULT)
    cancelled = AgentResult.model_validate(
        {
            "fence": claim.fence,
            "state": "cancelled",
            "result": {
                "error_code": "operation_cancelled",
                "reason": "exact stop completed",
            },
        }
    )
    clock.advance(seconds=31)
    with pytest.raises(StaleAgentAttempt):
        jobs.record_result(cancelled)
    assert jobs.record_late_result(cancelled)
    with sessions() as session:
        running_row = session.get(AgentOperation, running.id)
        parent_row = session.get(Job, job.id)
        assert running_row is not None and running_row.state == "cancelled"
        assert parent_row is not None and parent_row.state == "cancelled"
        assert not AgentJobService.assess_superseded_agent_effects_in_session(
            session, (NODE_A, NODE_B), 2, clock.now
        )
    assert jobs.known_superseded_cancellation(
        AgentProgress.model_validate(
            {
                "fence": claim.fence,
                "progress": None,
            }
        )
    )


def test_new_intent_finishes_superseded_parent_with_mixed_terminal_children(
    service,
) -> None:
    """A succeeded rank plus a cancelled rank must not leave its parent running."""

    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    succeeded = jobs.enqueue(old_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    cancelled = jobs.enqueue(
        old_parent.id,
        NODE_B,
        "recipe.stop",
        COMMIT,
        recipe_stop_payload(NODE_B, plan_digest=COMMIT),
    )
    with sessions.begin() as session:
        parent_row = session.get(Job, old_parent.id)
        succeeded_row = session.get(AgentOperation, succeeded.id)
        cancelled_row = session.get(AgentOperation, cancelled.id)
        node_a = session.get(AgentNode, NODE_A)
        node_b = session.get(AgentNode, NODE_B)
        assert (
            parent_row is not None
            and succeeded_row is not None
            and cancelled_row is not None
            and node_a is not None
            and node_b is not None
        )
        parent_row.state = "running"
        parent_row.result = {
            "cancel_requested": True,
            "cancel_requested_at": clock.now.isoformat(),
        }
        succeeded_row.state = "succeeded"
        cancelled_row.state = "cancelled"
        node_a.workload_intent_ordinal = 2
        node_b.workload_intent_ordinal = 2
        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, (NODE_A, NODE_B), 2, clock.now
        )

    with sessions() as session:
        parent_row = session.get(Job, old_parent.id)
        assert parent_row is not None
        assert parent_row.state == "cancelled"
        assert parent_row.status_reason == "superseded by newer workload intent"


def test_new_intent_retires_stopped_legacy_parent_with_unissued_child(service) -> None:
    """Pre-ordinal parked claims must not block a newer workload forever."""

    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    start_payload = canonical_start_payload(
        start_deadline=clock.now + timedelta(minutes=30)
    )
    succeeded = jobs.enqueue(
        old_parent.id, NODE_A, "recipe.start", COMMIT, start_payload
    )
    parked = jobs.enqueue(old_parent.id, NODE_B, "recipe.start", COMMIT, start_payload)
    run_id = str(uuid.uuid4())
    with sessions.begin() as session:
        parent_row = session.get(Job, old_parent.id)
        succeeded_row = session.get(AgentOperation, succeeded.id)
        parked_row = session.get(AgentOperation, parked.id)
        node_a = session.get(AgentNode, NODE_A)
        node_b = session.get(AgentNode, NODE_B)
        assert (
            parent_row is not None
            and succeeded_row is not None
            and parked_row is not None
            and node_a is not None
            and node_b is not None
        )
        session.add(
            RecipeRun(
                id=run_id,
                installation_id="legacy-installation",
                mapping_id="legacy-mapping",
                mapping_generation=1,
                alias="legacy",
                plan_digest="d" * 64,
                plan={},
                state="stopped",
                route_state="withdrawn",
                actor="operator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        parent_row.payload = {"owner_kind": "run", "owner_id": run_id}
        parent_row.state = "waiting-for-operator"
        succeeded_row.state = "succeeded"
        parked_row.state = "waiting-for-operator"
        parked_row.current_attempt = 1
        parked_row.status_reason = "claim refused: operator-retry-not-authorized"
        succeeded_row.workload_intent_ordinal = None
        parked_row.workload_intent_ordinal = None
        node_a.workload_intent_ordinal = 2
        node_b.workload_intent_ordinal = 2
        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, (NODE_A, NODE_B), 2, clock.now
        )

    with sessions() as session:
        parent_row = session.get(Job, old_parent.id)
        parked_row = session.get(AgentOperation, parked.id)
        assert parent_row is not None and parent_row.state == "cancelled"
        assert parked_row is not None and parked_row.state == "cancelled"
        assert parent_row.status_reason == "superseded by newer workload intent"


def test_late_old_cancellation_never_retires_a_newer_attempt(service) -> None:
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    operation = jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None
    second_fence = str(uuid.uuid4())
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        row = session.get(AgentOperation, operation.id)
        first_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == first.fence
            )
        )
        assert node is not None and row is not None and first_attempt is not None
        node.workload_intent_ordinal = 2
        jobs.request_superseded_workload_cancellation_in_session(
            session, (NODE_A,), 2, clock.now
        )
        first_attempt.state = "expired"
        row.current_attempt = 2
        session.add(
            AgentOperationAttempt(
                operation_id=operation.id,
                attempt=2,
                fence=second_fence,
                lease_deadline=clock.now + timedelta(minutes=1),
                agent_certificate_serial="serial-a",
                state="running",
            )
        )
    late = AgentResult.model_validate(
        {
            "fence": first.fence,
            "state": "cancelled",
            "result": {
                "error_code": "operation_cancelled",
                "reason": "old helper stopped",
            },
        }
    )
    assert jobs.record_late_result(late)
    with sessions() as session:
        row = session.get(AgentOperation, operation.id)
        newer = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == second_fence
            )
        )
        assert row is not None and row.current_attempt == 2 and row.state == "running"
        assert newer is not None and newer.state == "running" and newer.result is None


@pytest.mark.parametrize("older_work", (None, "exact-retry", "running"))
def test_agent_upgrade_completes_only_after_exact_new_runtime_reconnects(
    service,
    older_work,
) -> None:
    exercise_upgrade_reconnect(service, older_work)


def exercise_upgrade_reconnect(service, older_work) -> None:
    jobs, sessions, clock = service
    from .package_upgrade_fixtures import activation_receipt, source_transport

    target = {
        "architecture": "linux-arm64",
        "package_bytes": 1234,
        "package_sha256": "d" * 64,
        "package_signature": "e" * 128,
        "package_url": (
            "https://install.vonkforge.ai/dev/releases/example/"
            "spark/current/linux-arm64/vonk-forge-agent.deb"
        ),
        "package_version": "0.1.0~dev.330+g0123456789ab",
        "schema_version": 1,
        "target_binary_digest": "a" * 64,
        "target_build_digest": "sha256:" + "b" * 64,
    }
    old_identity = {
        "architecture": "linux-arm64",
        "binary_digest": "f" * 64,
        "build_digest": "sha256:" + "f" * 64,
        "semantic_version": "0.1.0",
    }
    older = first = None
    if older_work is not None:
        older = jobs.enqueue(
            parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
        )
        first = claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=old_identity,
        )
        assert first is not None and fenced_operation(sessions, first).id == older.id
        clock.advance(seconds=1)

    job = parent(sessions, clock)
    target.update(source_transport())
    operation = jobs.enqueue(job.id, NODE_A, "agent.upgrade.v1", COMMIT, target)
    if older_work == "running":
        # Independent recovery must still wait for an actually running mutation.
        assert (
            claim_agent(
                jobs,
                NODE_A,
                "serial-a",
                runtime_identity=old_identity,
            )
            is None
        )
    if first is not None:
        jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": first.fence,
                        "state": "waiting-for-operator",
                        "result": {
                            "error_code": "agent_restart_interrupted",
                            "failure_kind": "uncertain-effect",
                            "uncertain": True,
                            "reason": "agent process restarted",
                        },
                    }
                )
            )
        )
        with sessions() as session:
            stored = session.get(AgentOperation, fenced_operation(sessions, first).id)
            assert stored is not None and stored.next_action_at is not None
            clock.now = stored.next_action_at.replace(tzinfo=UTC)
        # Retry and upgrade ordering must survive a Controller restart.
        jobs = AgentJobService(sessions, clock=clock)
        # The older exact retry was queued first, so it resumes first.
        resumed = claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=old_identity,
        )
        assert older is not None
        assert resumed is not None and fenced_operation(sessions, resumed).id == (
            older.id
        )
        assert (
            fenced_attempt(sessions, resumed).attempt
            == fenced_attempt(sessions, first).attempt + 1
        )
        assert resumed.payload == first.payload
        jobs.succeed(resumed, STOP_RESULT)
        assert job_state(sessions, older.parent_job_id).state == "succeeded"

    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        runtime_identity=old_identity,
    )
    assert claim is not None
    assert fenced_operation(sessions, claim).id == operation.id
    assert job_state(sessions, job.id).state == "queued"

    new_identity = {
        **old_identity,
        "binary_digest": target["target_binary_digest"],
        "build_digest": target["target_build_digest"],
        "package_activation": activation_receipt(
            claim.payload.model_dump(mode="json"),
            NODE_A,
            now=int(clock.now.timestamp()),
        ),
    }
    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=new_identity,
        )
        is None
    )

    assert job_state(sessions, job.id).state == "succeeded"
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        assert stored is not None and stored.state == "succeeded"
        assert attempt is not None and attempt.state == "succeeded"
        assert attempt.result == {
            "architecture": "linux-arm64",
            "binary_digest": "a" * 64,
            "build_digest": "sha256:" + "b" * 64,
            "package_sha256": "d" * 64,
            "package_version": "0.1.0~dev.330+g0123456789ab",
            "status": "upgraded",
            "activation_receipt": new_identity["package_activation"],
        }


def test_artifact_distribution_is_serialized_as_a_mutation(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    payload = {"plan_digest": COMMIT}
    first = jobs.enqueue(parent_job.id, NODE_A, operation, COMMIT, payload)
    clock.advance(seconds=1)
    second = jobs.enqueue(parent_job.id, NODE_A, operation, COMMIT, payload)

    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )

    assert claim is not None
    assert fenced_operation(sessions, claim).id == first.id
    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
        )
        is None
    )
    with sessions() as session:
        stored = session.get(AgentOperation, second.id)
        assert stored is not None and stored.state == "queued"


def test_rust_claim_updates_current_contact(service) -> None:
    jobs, sessions, _clock = service
    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=PACKAGED_RUNTIME_IDENTITY,
            preflight_fingerprint="e" * 64,
        )
        is None
    )
    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.preflight_fingerprint == "e" * 64
        assert node.protocol_version == 4


def test_service_claim_requires_packaged_runtime_identity(
    service,
) -> None:
    jobs, _sessions, _clock = service

    with pytest.raises(TypeError, match="runtime_identity"):
        jobs.claim(
            NODE_A,
            "serial-a",
        )

    with pytest.raises(ValueError, match="runtime identity"):
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=None,
        )


def test_package_operation_is_not_a_control_plane_queue_operation(service) -> None:
    jobs, sessions, clock = service

    with pytest.raises(ValueError, match="not supported"):
        jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            "package.prepare",
            COMMIT,
            {
                "schema_version": 1,
                "deployment_id": "removed-package",
                "release_digest": "a" * 64,
                "deployment_digest": "b" * 64,
            },
        )


def test_recipe_build_is_rejected_when_builder_runtime_changed_before_claim(
    service,
) -> None:
    jobs, sessions, clock = service
    build_id = "00000000-0000-4000-8000-000000000009"
    revision_id = "00000000-0000-4000-8000-000000000001"
    payload = {
        "adapter": _BUILD_ADAPTER,
        "build_id": build_id,
        "recipe_revision_id": revision_id,
        "recipe_content_sha256": "a" * 64,
        "source_bundle_sha256": "b" * 64,
        "source_bundle_bytes": 4096,
        "base_images": [],
        "base_image_storage_bytes": 0,
        "capabilities": [],
        "build_input_sha256": "c" * 64,
        "dockerfile": "Dockerfile",
        "network": {"hosts": []},
        "options": {
            "additional_contexts": [],
            "annotations": [],
            "environment": [],
            "format": "oci",
            "identity_label": True,
            "ignorefile": None,
            "jobs": 1,
            "labels": [],
            "layer_compression": "disabled",
            "layer_labels": [],
            "layers": True,
            "no_hostname": False,
            "no_hosts": False,
            "omit_history": False,
            "os_features": [],
            "os_version": None,
            "shm_bytes": 67108864,
            "skip_unused_stages": True,
            "squash": "none",
            "timestamp": None,
            "unset_environment": [],
            "unset_labels": [],
        },
        "limits": {
            "cpu_cores": 8,
            "memory_bytes": 1024,
            "temporary_bytes": 4096,
            "processes": 64,
            "timeout_seconds": 600,
            "output_bytes": 2048,
        },
    }
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64,
                state="building",
                policy_report={
                    "passed": True,
                    "builder_binary_digest": "f" * 64,
                    "artifact_format": "docker-archive-v1",
                },
                plan=payload,
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            ResourceReservation(
                node_id=NODE_A,
                kind="disk",
                resource_key="c" * 64,
                amount_bytes=4096,
                owner_kind="recipe-build",
                owner_id=build_id,
                state="active",
                plan_digest="c" * 64,
                created_at=clock.now,
            )
        )
        stored_parent = session.get(Job, parent_job.id)
        assert stored_parent is not None
        stored_parent.kind = "recipe.build.v1"
        stored_parent.payload = {
            "schema_version": 1,
            "owner_kind": "recipe-build",
            "owner_id": build_id,
            "plan_digest": "c" * 64,
        }
    recipe_operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=jobs,
        clock=clock,
    )
    jobs.set_result_consumer(recipe_operations.consume_agent_result)
    operation = jobs.enqueue(
        parent_job.id,
        NODE_A,
        "recipe.build.v1",
        COMMIT,
        payload,
    )
    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        runtime_identity={
            "architecture": "linux-arm64",
            "build_digest": "sha256:" + "e" * 64,
            "binary_digest": "e" * 64,
            "semantic_version": "1.2.3",
        },
    )

    assert claim is None
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent_row = session.get(Job, parent_job.id)
        node = session.get(AgentNode, NODE_A)
        build = session.get(RecipeBuild, build_id)
        reservation = session.scalar(
            select(ResourceReservation).where(ResourceReservation.owner_id == build_id)
        )
        assert stored is not None and stored.state == "failed"
        assert parent_row is not None and parent_row.state == "failed"
        assert node is not None and node.binary_digest == "e" * 64
        assert build is not None and build.state == "failed"
        assert reservation is not None and reservation.state == "released"


def test_recipe_build_requires_runtime_identity_on_the_current_claim(service) -> None:
    jobs, sessions, clock = service
    build_id = "00000000-0000-4000-8000-000000000019"
    revision_id = "00000000-0000-4000-8000-000000000011"
    payload = {
        "adapter": _BUILD_ADAPTER,
        "build_id": build_id,
        "recipe_revision_id": revision_id,
        "recipe_content_sha256": "a" * 64,
        "source_bundle_sha256": "b" * 64,
        "source_bundle_bytes": 4096,
        "base_images": [],
        "base_image_storage_bytes": 0,
        "capabilities": [],
        "build_input_sha256": "c" * 64,
        "dockerfile": "Dockerfile",
        "network": {"hosts": []},
        "options": {
            "additional_contexts": [],
            "annotations": [],
            "environment": [],
            "format": "oci",
            "identity_label": True,
            "ignorefile": None,
            "jobs": 1,
            "labels": [],
            "layer_compression": "disabled",
            "layer_labels": [],
            "layers": True,
            "no_hostname": False,
            "no_hosts": False,
            "omit_history": False,
            "os_features": [],
            "os_version": None,
            "shm_bytes": 67108864,
            "skip_unused_stages": True,
            "squash": "none",
            "timestamp": None,
            "unset_environment": [],
            "unset_labels": [],
        },
        "limits": {
            "cpu_cores": 8,
            "memory_bytes": 1024,
            "temporary_bytes": 4096,
            "processes": 64,
            "timeout_seconds": 600,
            "output_bytes": 2048,
        },
    }
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64,
                state="building",
                policy_report={
                    "passed": True,
                    "builder_binary_digest": "f" * 64,
                    "artifact_format": "docker-archive-v1",
                },
                plan=payload,
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(
        parent_job.id,
        NODE_A,
        "recipe.build.v1",
        COMMIT,
        payload,
    )

    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )

    assert claim is None
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == "failed"


def test_concurrent_agents_cannot_claim_the_same_operation(service) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(
            pool.map(lambda _: claim_agent(jobs, NODE_A, "serial-a"), range(4))
        )

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert fenced_operation(sessions, claimed[0]).id == operation.id


def test_long_poll_wakes_on_enqueue_and_times_out_without_per_client_state(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(claim_agent, jobs, NODE_A, "serial-a", 1.0)
        time.sleep(0.05)
        operation = jobs.enqueue(
            parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
        )
        claim = waiting.result(timeout=1)
    elapsed = time.monotonic() - started

    assert claim is not None and fenced_operation(sessions, claim).id == operation.id
    assert elapsed < 0.8

    timeout_started = time.monotonic()
    assert claim_agent(jobs, NODE_B, "serial-b", 0.08) is None
    timeout_elapsed = time.monotonic() - timeout_started
    # The lower bound proves the requested long-poll timeout is honored. Keep
    # the upper bound generous enough for a CPU-starved parallel CI worker to
    # be scheduled after the condition deadline has already elapsed.
    assert 0.06 <= timeout_elapsed < 1.5


def test_long_poll_rechecks_database_for_another_process_enqueue(service) -> None:
    _, sessions, clock = service
    monotonic = MonotonicClock()
    jobs = AgentJobService(sessions, clock=clock, monotonic=monotonic)
    other_process = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    first_poll = Event()
    original_claim_once = jobs._claim_once

    def observed_claim_once(*args, **kwargs):
        result = original_claim_once(*args, **kwargs)
        first_poll.set()
        return result

    jobs._claim_once = observed_claim_once  # type: ignore[method-assign]
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        waiting = pool.submit(claim_agent, jobs, NODE_A, "serial-a", 2.0)
        assert first_poll.wait(timeout=_SCHEDULER_GUARD_SECONDS)
        operation = other_process.enqueue(
            parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
        )
        claim = waiting.result(timeout=_SCHEDULER_GUARD_SECONDS)
    finally:
        # Retire a poll that never observed the enqueue so the executor cannot
        # wait on it forever: advance past the budget and wake a wait that is
        # still blocked.
        monotonic.now += 60.0
        jobs.notify_available()
        pool.shutdown(wait=True)

    assert claim is not None and fenced_operation(sessions, claim).id == operation.id


def test_expired_attempt_cannot_publish_success(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None

    clock.advance(seconds=31)
    second = claim_agent(jobs, NODE_A, "serial-a")
    assert second is None

    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(first, STOP_RESULT)
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state in aos.PARKED


def test_lease_expiry_records_reason_and_last_contact_facts(service) -> None:
    """An expiry parks the operation with the facts that describe it.

    The transition records no attempt result, so without a reason and the last
    accepted contact an operator sees an interrupted operation carrying no
    evidence at all and cannot tell a lost connection from an effect that may
    already have happened.
    """

    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None
    with sessions() as session:
        running = session.get(AgentOperation, operation.id)
        assert running is not None and running.status_reason is None

    clock.advance(seconds=31)
    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        assert attempt is not None
        deadline = attempt.lease_deadline
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        last_seen = node.last_seen_at

    assert stored is not None and stored.state in aos.PARKED
    reason = stored.status_reason
    assert reason is not None
    assert "attempt 1 lease expired" in reason
    assert "the effect is unobserved" in reason
    assert f"lease deadline {deadline.isoformat()}" in reason
    assert (
        "last accepted contact never observed"
        if last_seen is None
        else f"last accepted contact {last_seen.isoformat()}"
    ) in reason


def test_replayed_result_stays_fenced_and_records_no_second_outcome(service) -> None:
    """A replayed result is still fenced; the agent keeps the evidence instead.

    The Controller deliberately refuses a duplicate outcome rather than
    acknowledging it, so the agent must not treat the refusal as acceptance.
    This pins that contract while the agent keeps what it observed.
    """

    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None
    jobs.succeed(first, STOP_RESULT)

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
    assert stored is not None and stored.state == "succeeded"
    assert attempt is not None and attempt.state == "succeeded"

    clock.advance(seconds=31)
    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(first, STOP_RESULT)

    with sessions() as session:
        unchanged = session.get(AgentOperation, operation.id)
    assert unchanged is not None and unchanged.state == "succeeded"


def test_revoked_expired_or_node_mismatched_certificate_cannot_claim(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    with sessions.begin() as session:
        session.get(AgentCertificate, "serial-a").revoked_at = clock.now  # type: ignore[union-attr]

    assert claim_agent(jobs, NODE_A, "serial-a") is None
    assert claim_agent(jobs, NODE_A, "serial-b") is None

    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.revoked_at = None
        certificate.not_after = clock.now

    assert claim_agent(jobs, NODE_A, "serial-a") is None


def test_enqueue_rejects_noncanonical_protocol_payload(service) -> None:
    jobs, sessions, clock = service

    with pytest.raises(ValueError, match="unsafe|protocol|validation"):
        jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            "recipe.stop",
            COMMIT,
            {"command": "uname"},
        )
    with pytest.raises(ValueError, match="large|protocol|validation"):
        jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            "recipe.stop",
            COMMIT,
            {"value": "x" * 70_000},
        )


@pytest.mark.parametrize(
    "terminal_state", ("succeeded", "failed", "waiting-for-operator", "expired")
)
def test_sqlite_enqueue_rejects_terminal_parent(service, terminal_state: str) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).state = terminal_state  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="terminal"):
        jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)


def test_sqlite_enqueue_enforces_parent_commit_and_target(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)

    with pytest.raises(ValueError, match="authority revision"):
        jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", "b" * 64, STOP_PAYLOAD)
    with sessions.begin() as session:
        stored_parent = session.get(Job, parent_job.id)
        assert stored_parent is not None
        stored_parent.targets = [NODE_A]
    with pytest.raises(ValueError, match="target"):
        jobs.enqueue(
            parent_job.id,
            NODE_B,
            "recipe.stop",
            COMMIT,
            recipe_stop_payload(NODE_B, plan_digest=COMMIT),
        )


def test_sqlite_enqueue_rejects_retired_node_before_parent_mutation(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.state = "retired"
        node.revoked_at = clock.now

    with pytest.raises(ValueError, match="active"):
        jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)

    assert job_state(sessions, parent_job.id).state == "queued"


def test_heartbeat_persists_canonical_progress_and_renews_lease(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None

    progress = jobs.heartbeat(claim, {"phase": "checking"}, 60)

    assert progress.deadline > claim.deadline
    assert progress.cancel_requested is False
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None
        assert attempt.progress["phase"] == "checking"
        assert attempt.progress["activity"] == "active"
        assert attempt.progress["observed_at"] == clock.now.isoformat()
        assert attempt.progress["last_progress_at"] == clock.now.isoformat()


def test_heartbeat_never_shortens_a_longer_existing_lease(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=10)

    progress = jobs.heartbeat(claim, {"phase": "checking"}, 30)

    assert progress.deadline >= claim.deadline


def test_a_lapsed_renewal_is_reacquired_inside_the_start_budget(service) -> None:
    # Wrong implementation: ``_active`` refused every renewal once the accepted
    # lease deadline had passed, so a Controller that was briefly unreachable --
    # one lost round trip, a restart, a slow database -- turned a healthy
    # multi-minute start into a parked operation whose effect was unobserved.
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        "recipe.start",
        COMMIT,
        canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30)),
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None

    # The Controller was unreachable for twice the accepted lease.
    clock.advance(seconds=60)
    renewed = jobs.heartbeat(claim, {"phase": "rank-launch"}, 30)

    assert renewed.deadline > claim.deadline
    assert renewed.cancel_requested is False
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None
        assert attempt.state == "running"
        # SQLite hands back a naive timestamp; the fact under test is that the
        # stored deadline moved past the lease the agent had accepted.
        stored = attempt.lease_deadline
        assert (
            stored.replace(tzinfo=UTC if stored.tzinfo is None else stored.tzinfo)
            > claim.deadline
        )
        current = session.get(AgentOperation, operation.id)
        assert current is not None
        assert current.state == "running"


def test_a_lapsed_renewal_is_refused_once_the_start_budget_is_spent(service) -> None:
    # The allowance is bounded by the operation's own immutable budget, not by
    # the lease being recovered: wrong implementation lets any fence re-acquire
    # an attempt whose start deadline has already elapsed.
    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        "recipe.start",
        COMMIT,
        canonical_start_payload(start_deadline=clock.now + timedelta(seconds=40)),
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None

    clock.advance(seconds=60)
    with pytest.raises(StaleAgentAttempt):
        jobs.heartbeat(claim, {"phase": "rank-launch"}, 30)


def test_a_lapsed_renewal_without_a_start_budget_is_refused(service) -> None:
    # Deliberate limitation, recorded rather than papered over: an operation that
    # binds no start deadline has no second clock to bound an allowance, and
    # inventing one would widen the fence with no fact behind it.  Its lease
    # stays the only clock.
    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None

    clock.advance(seconds=60)
    with pytest.raises(StaleAgentAttempt):
        jobs.heartbeat(claim, None, 30)


def test_a_silent_start_inside_its_launch_budget_is_not_parked_and_completes(
    service,
) -> None:
    # Wrong implementation: the attempt's accepted 30-second lease was the only
    # clock, so a rank launch that legitimately spends longer than that -- the
    # plan's own readiness budget is what the recipe declares -- was expired and
    # parked, and the eventual success arrived after the fence had closed.  The
    # operation's immutable start budget is the deadline that protects the
    # launch, so the exact attempt stays current, the successful outcome is
    # applied, and its owner is released.
    jobs, sessions, clock = service
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30))
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.start", COMMIT, payload
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None

    # The agent is blocked inside the launch and sends no heartbeat for four
    # times the lease the agent accepted, while the declared budget is open.
    clock.advance(seconds=120)

    # A poll for work by the same node must neither expire nor park it.
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    with sessions() as session:
        running = session.get(AgentOperation, operation.id)
    assert running is not None and running.state == "running"

    # The launch finally succeeds and the still-current attempt publishes it.
    jobs.succeed(claim, canonical_start_result(payload))

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
    assert stored is not None and stored.state == "succeeded"
    assert attempt is not None and attempt.state == "succeeded"
    assert job_state(sessions, operation.parent_job_id).state == "succeeded"


def test_a_start_that_stops_reporting_past_its_budget_fails_with_the_reason(
    service,
) -> None:
    # The launch allowance is bounded by the operation's own budget: a node that
    # never reports again once that budget is spent cannot be waited for, and a
    # retry could only be refused by the agent before it executes anything.  The
    # start fails with a named reason instead of waiting for an operator.
    jobs, sessions, clock = service
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(seconds=40))
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.start", COMMIT, payload
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None

    clock.advance(seconds=60)
    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
    assert stored is not None and stored.state == "failed"
    reason = stored.status_reason
    assert reason is not None
    assert "start deadline" in reason and "not retried" in reason


def test_a_superseded_attempts_late_result_cannot_overwrite_a_newer_attempt(
    service,
) -> None:
    # The launch budget accepts a slow-but-live result; it never re-blesses an
    # attempt another executor has replaced.  The positive control first pins
    # the acceptance the defect lacked, then the same late receipt is refused
    # once a second attempt owns the operation's fence.
    jobs, sessions, clock = service
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30))
    accepted = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.start", COMMIT, payload
    )
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None
    clock.advance(seconds=60)
    jobs.succeed(first, canonical_start_result(payload))
    with sessions() as session:
        accepted_row = session.get(AgentOperation, accepted.id)
    assert accepted_row is not None and accepted_row.state == "succeeded"

    # The same shape of late receipt cannot cross a newer attempt's ownership.
    superseded = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_B,
        "recipe.start",
        COMMIT,
        canonical_start_payload(start_deadline=clock.now + timedelta(seconds=40)),
    )
    abandoned = claim_agent(
        jobs,
        NODE_B,
        "serial-b",
    )
    assert abandoned is not None
    clock.advance(seconds=60)
    # A spent budget now fails the order; park it directly the way a restart
    # reconciliation of a still-open budget does, to keep this fence scenario.
    with sessions.begin() as session:
        parked = session.get(AgentOperation, superseded.id)
        parked.payload = canonical_start_payload(
            start_deadline=clock.now + timedelta(minutes=30)
        )
        attempt_row = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == superseded.id
            )
        )
        attempt_row.state = "expired"
        parked.state = "waiting-for-operator"
        AgentOperationAdapter(session).settle(
            parked, attempt_row, None, Reported(Outcome.UNKNOWN), clock.now
        )
    with sessions.begin() as session:
        authorize_operator_resume_in_session(
            session, superseded.parent_job_id, clock.now
        )
    # The park already scheduled its bounded safe retry, so let it come due
    # before the next claim; the retry clock and the operator authorisation are
    # the same decision and must not be moved earlier by a resume.
    clock.advance(seconds=3)
    current = claim_agent(
        jobs,
        NODE_B,
        "serial-b",
    )
    assert current is not None and fenced_attempt(sessions, current).attempt == 2
    late = AgentResult.model_validate_json(
        canonical_message(
            {
                "fence": abandoned.fence,
                "state": "succeeded",
                "result": canonical_start_result(payload),
            }
        )
    )
    jobs.record_late_result(late)

    with sessions() as session:
        stored = session.get(AgentOperation, superseded.id)
        current_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == current.fence
            )
        )
    assert stored is not None
    assert stored.state == "running" and stored.current_attempt == 2
    assert current_attempt is not None
    assert current_attempt.state == "running" and current_attempt.result is None


def test_a_finished_distribution_records_the_pulled_image_on_the_spark(
    service,
) -> None:
    """The success that ends a pull is what makes the next plan skip it."""

    jobs, sessions, clock = service
    source = MemoryObjectSource()
    model_digest = source.put(b"weights")
    address = "7" * 64
    image_bytes = 4096
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=str(uuid.uuid4()),
                recipe_revision_id=str(uuid.uuid4()),
                builder_node_id=NODE_A,
                source_bundle_sha256="c" * 64,
                build_input_sha256="e" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=f"sha256:{address}",
                oci_layout_sha256=address,
                image_bytes=image_bytes,
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
    assignment = NodeDistributionAssignment.parse(
        {
            "assignment_id": str(uuid.uuid4()),
            "plan_digest": COMMIT,
            "generation": 1,
            "node_id": NODE_A,
            "expires_at": (clock.now + timedelta(hours=1)).isoformat(),
            "model_artifact_set_sha256": "b" * 64,
            "objects": [
                {"name": "weights", "sha256": model_digest, "bytes": 7, "kind": "model"}
            ],
            "oci_image_digest": f"sha256:{address}",
            "oci_image_config_digest": "sha256:" + "9" * 64,
            "oci_archive_sha256": address,
        }
    )
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, address)
    DistributionService(source, clock=clock, sessions=sessions).register(assignment)
    jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value,
        COMMIT,
        {"plan_digest": COMMIT},
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None

    jobs.succeed(claim, {"downloaded_bytes": 7})

    with sessions() as session:
        stored = session.scalars(
            select(NodeArtifact).where(NodeArtifact.node_id == NODE_A)
        ).all()
    # Exactly the record admission and Run/Switch read as "image present".
    assert [
        (item.kind, item.digest, item.size_bytes, item.state) for item in stored
    ] == [("image", address, image_bytes, "verified")]


@pytest.mark.parametrize(
    "restriction", (None, "expired", "revoked", "cancelled", "stale")
)
def test_distribution_heartbeat_renews_only_live_authorized_transfer(
    service, restriction
) -> None:
    jobs, sessions, clock = service
    source = MemoryObjectSource()
    model_digest = source.put(b"weights")
    image_digest = source.put(b"image")
    assignment = NodeDistributionAssignment.parse(
        {
            "assignment_id": str(uuid.uuid4()),
            "plan_digest": COMMIT,
            "generation": 1,
            "node_id": NODE_A,
            "expires_at": (clock.now + timedelta(hours=1)).isoformat(),
            "model_artifact_set_sha256": "b" * 64,
            "objects": [
                {
                    "name": "weights",
                    "sha256": model_digest,
                    "bytes": 7,
                    "kind": "model",
                },
            ],
            "oci_image_digest": "sha256:" + image_digest,
            "oci_image_config_digest": "sha256:" + "9" * 64,
            "oci_archive_sha256": image_digest,
        }
    )
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, image_digest)
    distribution = DistributionService(source, clock=clock, sessions=sessions)
    distribution.register(assignment)
    other = NodeDistributionAssignment.parse(
        assignment.to_mapping()
        | {
            "assignment_id": str(uuid.uuid4()),
            "node_id": NODE_B,
        }
    )
    distribution.register(other)
    other_plan = NodeDistributionAssignment.parse(
        assignment.to_mapping()
        | {
            "assignment_id": str(uuid.uuid4()),
            "plan_digest": "c" * 64,
        }
    )
    distribution.register(other_plan)
    job = parent(sessions, clock)
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    jobs.enqueue(
        job.id,
        NODE_A,
        kind,
        COMMIT,
        {"plan_digest": COMMIT},
    )
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        certificate.not_after = clock.now + timedelta(hours=3)
    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert claim is not None
    # Steady heartbeats keep the transfer's own lease alive for the whole copy.
    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        attempt.lease_deadline = clock.now + timedelta(hours=2)
    clock.advance(seconds=3600 if restriction == "expired" else 3590)
    if restriction == "revoked":
        distribution.revoke(plan_digest=COMMIT, node_id=NODE_A)
    elif restriction == "cancelled":
        with sessions.begin() as session:
            session.get(Job, job.id).result = {"cancel_requested": True}
    elif restriction == "stale":
        with sessions.begin() as session:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.fence == claim.fence
                )
            )
            attempt.lease_deadline = clock.now
    if restriction == "stale":
        with pytest.raises(StaleAgentAttempt):
            jobs.heartbeat(claim, {"phase": "copying", "completed_bytes": 3}, 60)
    else:
        jobs.heartbeat(claim, {"phase": "copying", "completed_bytes": 3}, 60)
    clock.advance(seconds=20)
    # The serving process must observe the durable renewal after the initial
    # grant elapsed. A different node, revoked grant or expired fence cannot.
    restarted = DistributionService(source, clock=clock, sessions=sessions)
    if restriction is None:
        renewed, spec, opened = restarted.open_object(
            node_id=NODE_A, plan_digest=COMMIT, digest=model_digest
        )
        with opened.stream:
            assert opened.stream.read() == b"weights"
        assert spec.sha256 == model_digest
        assert (
            renewed.to_mapping() | {"expires_at": assignment.to_mapping()["expires_at"]}
            == assignment.to_mapping()
        )
        assert renewed.expires_at > clock.now
    else:
        with pytest.raises(DistributionError):
            restarted.authorize(node_id=NODE_A, plan_digest=COMMIT)
    with pytest.raises(DistributionError):
        restarted.authorize(node_id=NODE_B, plan_digest=COMMIT)
    with pytest.raises(DistributionError):
        restarted.authorize(node_id=NODE_A, plan_digest=other_plan.plan_digest)
    if restriction is None:
        clock.advance(seconds=3600)
        with pytest.raises(DistributionError):
            restarted.authorize(node_id=NODE_A, plan_digest=COMMIT)


def test_claim_persists_authenticated_running_release_identity(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    runtime_identity = AgentRuntimeIdentity(
        architecture="linux-arm64",
        binary_digest="c" * 64,
        build_digest="sha256:" + "c" * 64,
        semantic_version="1.2.3",
    )

    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=runtime_identity,
        )
        is not None
    )

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert {
            "architecture": node.architecture,
            "binary_digest": node.binary_digest,
            "build_digest": node.build_digest,
            "semantic_version": node.semantic_version,
        } == runtime_identity.model_dump(exclude={"package_activation"})
        assert node.contact_observation_digest is not None
        assert re.fullmatch(r"[0-9a-f]{64}", node.contact_observation_digest)


@pytest.mark.parametrize("architecture", ("linux-riscv64", True, 7))
def test_claim_rejects_malformed_runtime_architecture_without_persisting_it(
    service, architecture: object
) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    runtime_identity = {
        "architecture": architecture,
        "binary_digest": "c" * 64,
        "build_digest": "sha256:" + "c" * 64,
        "semantic_version": "1.2.3",
    }

    with pytest.raises(ValueError, match="runtime identity"):
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=runtime_identity,
        )

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.architecture == "linux-arm64"
        assert node.semantic_version == "1.0.0"
        assert node.build_digest == "sha256:" + "f" * 64
        assert node.binary_digest == "f" * 64


@pytest.mark.parametrize("agent_action", ("heartbeat", "result"))
def test_retired_identity_cannot_mutate_active_attempt_or_record_contact(
    service, agent_action: str
) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        certificate = session.get(AgentCertificate, "serial-a")
        assert node is not None and certificate is not None
        node.state = "retired"
        node.revoked_at = clock.now
        node.last_seen_at = None
        certificate.state = "revoked"
        certificate.revoked_at = clock.now

    with pytest.raises(StaleAgentAttempt):
        if agent_action == "heartbeat":
            jobs.heartbeat(claim, {"phase": "checking"}, 60)
        else:
            jobs.succeed(claim, {"healthy": True})

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        stored_operation = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt
                == fenced_attempt(sessions, claim).attempt,
            )
        )
        assert node is not None and node.last_seen_at is None
        assert node.protocol_version == 4
        assert stored_operation is not None and stored_operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.progress is None and attempt.result is None


def test_public_fence_string_interface_renews_and_completes(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None

    progress = jobs.heartbeat(claim.fence, {"phase": "checking"}, 60)
    jobs.succeed(progress.fence, STOP_RESULT)

    with pytest.raises(StaleAgentAttempt):
        jobs.fail(str(uuid.uuid4()), "unknown fence")


def test_attempt_expiring_exactly_at_claim_time_requires_operator_retry(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None

    clock.advance(seconds=30)
    second = claim_agent(jobs, NODE_A, "serial-a")

    assert second is None
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state in aos.PARKED


def test_parent_job_becomes_succeeded_only_after_every_operation_succeeds(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    jobs.enqueue(
        parent_job.id,
        NODE_B,
        "recipe.stop",
        COMMIT,
        recipe_stop_payload(NODE_B, plan_digest=COMMIT),
    )

    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None
    jobs.succeed(first, STOP_RESULT)
    assert job_state(sessions, parent_job.id).state == "queued"

    second = claim_agent(jobs, NODE_B, "serial-b")
    assert second is not None
    jobs.succeed(second, STOP_RESULT)

    assert job_state(sessions, parent_job.id).state == "succeeded"


def test_parent_job_fails_when_all_operations_are_terminal_and_one_failed(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    jobs.enqueue(
        parent_job.id,
        NODE_B,
        "recipe.stop",
        COMMIT,
        recipe_stop_payload(NODE_B, plan_digest=COMMIT),
    )

    failed = claim_agent(jobs, NODE_A, "serial-a")
    assert failed is not None
    jobs.fail(failed, "token=sensitive " + "x" * 2_000)
    assert job_state(sessions, parent_job.id).state == "queued"

    succeeded = claim_agent(jobs, NODE_B, "serial-b")
    assert succeeded is not None
    jobs.succeed(succeeded, STOP_RESULT)

    aggregate = job_state(sessions, parent_job.id)
    assert aggregate.state == "failed"
    assert aggregate.status_reason is not None
    assert "sensitive" not in aggregate.status_reason
    assert len(aggregate.status_reason) <= 1024


def test_parent_job_waits_when_all_operations_terminal_without_failures(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    jobs.enqueue(
        parent_job.id,
        NODE_B,
        "recipe.stop",
        COMMIT,
        recipe_stop_payload(NODE_B, plan_digest=COMMIT),
    )

    waiting = claim_agent(jobs, NODE_A, "serial-a")
    assert waiting is not None
    park_for_operator(sessions, jobs, waiting, "confirm displayed fingerprint")

    succeeded = claim_agent(jobs, NODE_B, "serial-b")
    assert succeeded is not None
    jobs.succeed(succeeded, STOP_RESULT)

    assert job_state(sessions, parent_job.id).state == "waiting-for-operator"


@pytest.mark.parametrize(
    "failure_kind", ["temporary-dependency", "resource-prerequisite"]
)
def test_transient_start_failure_retries_automatically_without_operator(
    service, failure_kind
) -> None:
    """Any transient failure of a restart-safe order is re-issued with backoff.

    Catches the narrower classifier that retried a start only for one error
    code and parked every other transient start failure for an operator.
    """

    from vonk_agent_protocol import AgentResult

    jobs, sessions, clock = service
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30))
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.start", COMMIT, payload
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    body = {key: claim.model_dump(mode="json")[key] for key in ("fence",)}
    jobs.record_result(
        AgentResult.model_validate_json(
            json.dumps(
                {
                    **body,
                    "state": "failed",
                    "result": {
                        "status": "failed",
                        "error_code": "recipe_start_failed",
                        "reason": "container runtime is busy",
                        "failure_kind": failure_kind,
                    },
                }
            )
        )
    )
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state in aos.PARKED
        assert stored.next_action_at is not None
        assert stored.next_action_at is not None
        due = stored.next_action_at.replace(tzinfo=UTC)
    assert job_state(sessions, operation.parent_job_id).state == "queued"
    clock.now = due + timedelta(seconds=1)
    retry = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert retry is not None and fenced_attempt(sessions, retry).attempt == 2


def test_a_foreign_container_refusal_waits_visibly_and_the_start_resumes_when_it_is_gone(
    service,
) -> None:
    """The typed refusal is a prerequisite wait, not an invalid contract.

    Catches the refusal read as ``invalid-contract``: the start order ended
    failed and the load with it, although the container may be removed a moment
    later.  The order stays re-issuable, names the container in its reason, and
    its parent stays open so the same intent starts once the name is free.
    """

    from vonk_agent_protocol import AgentResult

    jobs, sessions, clock = service
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30))
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.start", COMMIT, payload
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult.model_validate_json(
            json.dumps(
                {
                    "fence": claim.model_dump(mode="json")["fence"],
                    "state": "failed",
                    "result": {
                        "kind": "failed",
                        "code": "retained_container_foreign",
                        "reason": "container vonk-x occupies the name this start needs",
                        "failure_kind": "resource-prerequisite",
                        "retry_after_seconds": 30,
                        "evidence": {
                            "stage": "retained-container",
                            "diagnostic": "container=vonk-x",
                        },
                    },
                }
            )
        )
    )
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state in aos.PARKED
        assert "vonk-x" in (stored.status_reason or "")
        assert stored.next_action_at is not None
        due = stored.next_action_at.replace(tzinfo=UTC)
    assert due >= clock.now + timedelta(seconds=30)
    assert job_state(sessions, operation.parent_job_id).state == "queued"
    clock.now = due + timedelta(seconds=1)
    retry = claim_agent(jobs, NODE_A, "serial-a")
    assert retry is not None and fenced_attempt(sessions, retry).attempt == 2


def test_spent_start_budget_fails_a_parked_start_instead_of_waiting(service) -> None:
    """A start past its immutable deadline is failed, never parked or retried.

    Reproduces a distributed collective-readiness start that lapsed its lease
    and was re-issued until its parent was cancelled: the order sat in
    ``waiting-for-operator`` behind a retry the cancelled parent could never
    grant.  The next poll of the node closes it with a named reason.
    """

    jobs, sessions, clock = service
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30))
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.start", COMMIT, payload
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=31 * 60)
    # Lease lapsed and the budget is spent; park it the way the old path did.
    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        stored.state = "waiting-for-operator"
        stored.next_action_at = clock.now
        parent_job = session.get(Job, stored.parent_job_id)
        parent_job.result = {"cancel_requested": True}

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == "failed"
        assert stored.next_action_at is None
        assert "start deadline" in (stored.status_reason or "")
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        assert attempt is not None and attempt.state == "failed"
        assert attempt.result["error_code"] == "recipe_start_failed"
    assert job_state(sessions, operation.parent_job_id).state == "failed"


def test_start_failure_after_the_deadline_is_not_retried(service) -> None:
    from vonk_agent_protocol import AgentResult

    jobs, sessions, clock = service
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(seconds=40))
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.start", COMMIT, payload
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=35)
    jobs.heartbeat(claim, None, 30)
    clock.advance(seconds=10)
    jobs.record_result(
        AgentResult.model_validate_json(
            json.dumps(
                {
                    "fence": claim.fence,
                    "state": "failed",
                    "result": {
                        "status": "failed",
                        "error_code": "recipe_start_failed",
                        "reason": "distributed start deadline elapsed before execution",
                        "failure_kind": "temporary-dependency",
                    },
                }
            )
        )
    )
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == "failed"
        assert stored.next_action_at is None


@pytest.mark.parametrize(
    "body",
    [
        # What the Rust agent sends today: no failure kind, no uncertain flag.
        {"reason": "workload stop remains unconfirmed"},
        {
            "reason": "workload stop remains unconfirmed",
            "failure_kind": "uncertain-effect",
            "uncertain": True,
        },
        # Whatever failure kind it names, a waiting result is an uncertain effect.
        {
            "reason": "workload stop remains unconfirmed",
            "failure_kind": "invalid-contract",
        },
        {
            "reason": "workload stop remains unconfirmed",
            "failure_kind": "invalid-authority",
        },
        # What a typed agent sends: the same fact, as an unknown outcome.
        {
            "kind": "unknown",
            "wait_reason": "stop-unconfirmed",
            "reason": "workload stop remains unconfirmed",
        },
    ],
)
def test_an_agent_reported_waiting_body_is_retried_not_parked(service, body) -> None:
    """Finding 1: a stop that could not be confirmed is idempotent; no person waits."""

    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult.model_validate(
            {"fence": claim.fence, "state": "waiting-for-operator", "result": body}
        )
    )
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state in aos.PARKED
        assert stored.next_action_at is not None  # the retry is scheduled
        assert stored.status_reason is not None
        assert "retry scheduled at" in stored.status_reason
        assert "workload stop remains unconfirmed" in stored.status_reason
    assert job_state(sessions, operation.parent_job_id).state == "queued"
    clock.advance(seconds=120)
    retried = claim_agent(jobs, NODE_A, "serial-a")
    assert retried is not None
    assert fenced_operation(sessions, retried).id == operation.id
    assert fenced_attempt(sessions, retried).attempt == 2


@pytest.mark.parametrize(
    ("state", "body"),
    [
        (
            "waiting-for-operator",
            {"reason": "workload stop remains unconfirmed"},
        ),
        (
            "waiting-for-operator",
            {
                "kind": "unknown",
                "wait_reason": "stop-unconfirmed",
                "reason": "workload stop remains unconfirmed",
            },
        ),
    ],
)
def test_a_typed_and_a_legacy_unknown_store_the_same_order(
    service, state, body
) -> None:
    """Compatibility: both spellings park the order identically and keep its
    stored state word, so a Controller can serve typed and legacy agents at once."""

    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult.model_validate(
            {"fence": claim.fence, "state": state, "result": body}
        )
    )
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        assert stored.state in aos.PARKED
        attempt = fenced_attempt(sessions, claim)
        assert aos.attempt_is_observing(attempt)
        # The attempt stores the legacy-shaped body whichever way it arrived.
        assert attempt.result is not None
        assert attempt.result["reason"] == "workload stop remains unconfirmed"
        assert "kind" not in attempt.result


def test_a_typed_done_and_cancel_close_their_orders(service) -> None:
    jobs, sessions, clock = service
    first = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult.model_validate(
            {
                "fence": claim.fence,
                "state": "succeeded",
                "result": {"kind": "done", "result": {}},
            }
        )
    )
    with sessions() as session:
        stored = session.get(AgentOperation, first.id)
        assert stored is not None and stored.state == "succeeded"
        assert fenced_attempt(sessions, claim).result == {}


def test_a_waiting_report_of_a_cancelled_parent_ends_instead_of_waiting(
    service,
) -> None:
    """Rule 4: a cancel completes; the retry a cancelled parent could never claim
    is not scheduled, and the order does not wait for a person."""

    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    with sessions.begin() as session:
        stored_parent = session.get(Job, parent_job.id)
        assert stored_parent is not None
        stored_parent.result = {
            "cancel_requested": True,
            "cancel_requested_at": clock.now.isoformat(),
        }
    jobs.record_result(
        AgentResult.model_validate(
            {
                "fence": claim.fence,
                "state": "waiting-for-operator",
                "result": {"reason": "workload stop remains unconfirmed"},
            }
        )
    )
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        assert stored.state == "cancelled"
        assert stored.next_action_at is None


def test_progress_snapshots_and_phase_changes_have_bounded_write_frequency(
    service,
) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.heartbeat(claim, {"phase": "copying", "completed_bytes": 10}, 60)
    first_time = clock.now
    for count in range(11, 100):
        jobs.heartbeat(claim, {"phase": "copying", "completed_bytes": count}, 60)
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt.progress["completed_bytes"] == 10
        assert attempt.progress["observed_at"] == first_time.isoformat()
    clock.advance(seconds=0.1)
    jobs.heartbeat(claim, {"phase": "verifying", "completed_bytes": 100}, 60)
    with sessions() as session:
        rows = list(
            session.scalars(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.fence == claim.fence
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].progress["phase"] == "verifying"
        assert rows[0].progress["completed_bytes"] == 100


def test_distribution_retry_preserves_durable_progress_and_accepts_object_replay(
    service,
) -> None:
    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        kind,
        COMMIT,
        {"plan_digest": COMMIT},
    )
    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert claim is not None
    jobs.heartbeat(
        claim, {"phase": "copying", "completed_bytes": 100, "completed_items": 1}, 60
    )
    # Resume the same authorized, durable transfer after its prior attempt ended.
    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        stored.state = "waiting-for-operator"
        stored.next_action_at = clock.now
        prior = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        prior.state = "failed"
    clock.advance(seconds=61)
    resumed = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert resumed is not None and fenced_attempt(sessions, resumed).attempt == 2
    jobs.heartbeat(
        resumed, {"phase": "verifying", "completed_bytes": 50, "completed_items": 0}, 60
    )
    with sessions() as session:
        current = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == resumed.fence
            )
        )
        assert current.progress["completed_bytes"] == 100
        assert current.progress["completed_items"] == 1
        assert current.progress["phase"] == "verifying"


def test_late_result_is_retained_under_expired_fence_without_completing_operation(
    service,
) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=31)
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    from vonk_agent_protocol import AgentResult

    late = AgentResult.model_validate(
        {
            **{key: claim.model_dump(mode="json")[key] for key in ("fence",)},
            "state": "succeeded",
            "result": STOP_RESULT,
        }
    )
    assert jobs.record_late_result(late) is True
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert stored.state in aos.PARKED
        assert aos.attempt_lapsed(attempt)
        assert attempt.result == STOP_RESULT
    assert jobs.record_late_result(late) is True


def test_late_result_under_expired_fence_does_not_park_exact_resume_forever(
    service,
) -> None:
    """A result that arrives after the lease is evidence, not an operator gate.

    Live regression: a two-phase start's readiness failure reached the
    Controller an hour after its lease; the retained late result disqualified
    the parked order from exact re-issue, so the job, its run and every
    observation of that run waited for an operator indefinitely.
    """

    from vonk_agent_protocol import AgentResult

    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=31)
    late = AgentResult.model_validate(
        {
            **{key: claim.model_dump(mode="json")[key] for key in ("fence",)},
            "state": "failed",
            "result": {
                "status": "failed",
                "error_code": "recipe_stop_failed",
                "reason": "runtime process exited",
            },
        }
    )
    assert jobs.record_late_result(late) is True
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state in aos.PARKED
    resumed = None
    for _ in range(4):
        resumed = claim_agent(jobs, NODE_A, "serial-a")
        if resumed is not None:
            break
        with sessions() as session:
            stored = session.get(AgentOperation, operation.id)
            assert stored is not None
            assert stored.next_action_at is not None
            assert stored.next_action_at is not None
            clock.now = max(clock.now, stored.next_action_at.replace(tzinfo=UTC))
    assert resumed is not None
    assert (
        fenced_operation(sessions, resumed).id == operation.id
        and fenced_attempt(sessions, resumed).attempt == 2
    )
    with sessions() as session:
        previous = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert previous is not None and aos.attempt_lapsed(previous)
        assert previous.result is not None
    jobs.succeed(resumed, STOP_RESULT)
    assert job_state(sessions, operation.parent_job_id).state == "succeeded"


_RESTART_INTERRUPTED = {
    "error_code": "agent_restart_interrupted",
    "failure_kind": "uncertain-effect",
    "reason": "agent restarted with an operation in progress",
    "uncertain": True,
}


def _restart_interrupted_result(claim, operation_kind: str):
    from vonk_agent_protocol import AgentResult

    return AgentResult.model_validate_json(
        json.dumps(
            {
                "fence": claim.fence,
                "state": "waiting-for-operator",
                "result": {**_RESTART_INTERRUPTED, "operation": operation_kind},
            }
        )
    )


def _claim_until_due(jobs, sessions, clock, operation_id: str, *, rounds: int = 4):
    """Poll as the agent does, advancing to each scheduled retry time."""
    for _ in range(rounds):
        claim = claim_agent(jobs, NODE_A, "serial-a")
        if claim is not None:
            return claim
        with sessions() as session:
            stored = session.get(AgentOperation, operation_id)
            if stored.next_action_at is None:
                return None
            clock.now = max(clock.now, stored.next_action_at.replace(tzinfo=UTC))
    return None


def test_distribution_interrupted_twice_by_agent_restart_resumes_without_operator(
    service,
) -> None:
    """Live regression: a second restart parked the transfer behind an operator.

    The first interruption scheduled a retry. The claim that issued attempt 2
    left that retry authorisation on the row, so when attempt 2's agent
    restarted past its lease, the late restart receipt parked the order with an
    authorisation for the wrong attempt: neither the claim predicate nor the
    recovery scan could ever pick it up.
    """

    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, kind, COMMIT, {"plan_digest": COMMIT}
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.heartbeat(
        claim, {"phase": "copying", "completed_bytes": 100, "completed_items": 1}, 30
    )
    clock.advance(seconds=45)
    assert jobs.record_late_result(_restart_interrupted_result(claim, kind))
    claim = _claim_until_due(jobs, sessions, clock, operation.id)
    assert claim is not None and fenced_attempt(sessions, claim).attempt == 2
    resumed_progress = fenced_attempt(sessions, claim).progress
    assert resumed_progress is not None and resumed_progress["completed_bytes"] == 100

    clock.advance(seconds=45)
    assert jobs.record_late_result(_restart_interrupted_result(claim, kind))
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored.next_action_at is not None
        assert "retry scheduled at" in stored.status_reason
    assert job_state(sessions, operation.parent_job_id).state == "queued"

    third = _claim_until_due(jobs, sessions, clock, operation.id)
    assert third is not None and fenced_attempt(sessions, third).attempt == 3
    assert third.payload == claim.payload
    jobs.succeed(third, {"downloaded_bytes": 100})
    assert job_state(sessions, operation.parent_job_id).state == "succeeded"


@pytest.mark.parametrize("progresses", [False, True])
def test_repeated_agent_restarts_without_progress_slow_down_and_say_why(
    service, progresses: bool
) -> None:
    """A crash loop keeps its request but is throttled and named, never silent.

    A transfer that copies new bytes between restarts is healthy recovery and
    keeps the ordinary short backoff.
    """

    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, kind, COMMIT, {"plan_digest": COMMIT}
    )
    delays: list[float] = []
    reasons: list[str] = []
    for attempt_number in range(1, 8):
        claim = _claim_until_due(jobs, sessions, clock, operation.id, rounds=12)
        assert claim is not None
        assert fenced_attempt(sessions, claim).attempt == attempt_number
        if progresses or attempt_number == 1:
            jobs.heartbeat(
                claim,
                {
                    "phase": "copying",
                    "completed_bytes": 100 * attempt_number,
                    "completed_items": 1,
                },
                30,
            )
        clock.advance(seconds=45)
        assert jobs.record_late_result(_restart_interrupted_result(claim, kind))
        with sessions() as session:
            stored = session.get(AgentOperation, operation.id)
            delays.append(
                (stored.next_action_at.replace(tzinfo=UTC) - clock.now).total_seconds()
            )
            reasons.append(stored.status_reason)

    assert all("retry scheduled at" in reason for reason in reasons)
    if progresses:
        assert max(delays) <= 90
        assert not any("without copying new bytes" in reason for reason in reasons)
    else:
        assert "without copying new bytes" not in reasons[1]
        assert "agent restarted 3 times in a row" in reasons[3]
        assert delays[3] >= 30 and delays[4] >= 60 and delays[5] >= 120
        assert max(delays) <= 600 * 5 // 4


def _registered_distribution_grant(sessions, clock):
    source = MemoryObjectSource()
    model_digest = source.put(b"weights")
    image_digest = source.put(b"image")
    assignment = NodeDistributionAssignment.parse(
        {
            "assignment_id": str(uuid.uuid4()),
            "plan_digest": COMMIT,
            "generation": 1,
            "node_id": NODE_A,
            "expires_at": (clock.now + timedelta(hours=1)).isoformat(),
            "model_artifact_set_sha256": "b" * 64,
            "objects": [
                {
                    "name": "weights",
                    "sha256": model_digest,
                    "bytes": 7,
                    "kind": "model",
                }
            ],
            "oci_image_digest": "sha256:" + image_digest,
            "oci_image_config_digest": "sha256:" + "9" * 64,
            "oci_archive_sha256": image_digest,
        }
    )
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, image_digest)
    distribution = DistributionService(source, clock=clock, sessions=sessions)
    distribution.register(assignment)
    return distribution


@pytest.mark.parametrize("revoked", [False, True])
def test_a_retried_distribution_is_issued_a_fresh_grant(service, revoked: bool) -> None:
    """Live regression: the retry reused the first attempt's expired grant.

    The grant lapses an hour after registration; a transfer parked or backing
    off for longer was refused ``distribution.expired`` and failed terminally
    although nothing but time had changed. A revoked grant stays revoked.
    """

    from vonk_control.distribution import DistributionError

    jobs, sessions, clock = service
    distribution = _registered_distribution_grant(sessions, clock)
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, kind, COMMIT, {"plan_digest": COMMIT}
    )
    with sessions.begin() as session:
        session.get(AgentCertificate, "serial-a").not_after = clock.now + timedelta(
            hours=12
        )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=45)
    assert jobs.record_late_result(_restart_interrupted_result(claim, kind))
    # Hours later, with the grant lapsed and its refusal already recorded.
    clock.advance(seconds=3 * 3600)
    with pytest.raises(DistributionError, match="expired"):
        distribution.authorize(node_id=NODE_A, plan_digest=COMMIT)
    if revoked:
        distribution.revoke(plan_digest=COMMIT, node_id=NODE_A)

    retry = _claim_until_due(jobs, sessions, clock, operation.id)
    assert retry is not None and fenced_attempt(sessions, retry).attempt == 2
    fresh = DistributionService(distribution.source, clock=clock, sessions=sessions)
    if revoked:
        with pytest.raises(DistributionError, match="no longer active"):
            fresh.authorize(node_id=NODE_A, plan_digest=COMMIT)
    else:
        grant = fresh.authorize(node_id=NODE_A, plan_digest=COMMIT)
        assert grant.expires_at > clock.now + timedelta(minutes=30)


@pytest.mark.parametrize("sibling_fails_first", [False, True])
def test_a_failed_job_does_not_leave_a_sibling_parked_behind_an_unclaimable_retry(
    service, sibling_fails_first: bool
) -> None:
    """Live regression: one node's terminal failure stranded the other's retry.

    The job failed, so the sibling's scheduled retry could never be claimed
    (``parent-not-claimable``) and no operator action applied to it.
    """

    from vonk_agent_protocol import AgentResult

    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    job = parent(sessions, clock)
    first = jobs.enqueue(job.id, NODE_A, kind, COMMIT, {"plan_digest": COMMIT})
    second = jobs.enqueue(job.id, NODE_B, kind, COMMIT, {"plan_digest": COMMIT})
    with sessions.begin() as session:
        for serial in ("serial-a", "serial-b"):
            session.get(AgentCertificate, serial).not_after = clock.now + timedelta(
                hours=4
            )
    claim_a = claim_agent(jobs, NODE_A, "serial-a")
    claim_b = claim_agent(jobs, NODE_B, "serial-b")
    assert claim_a is not None and claim_b is not None

    def refuse() -> None:
        jobs.record_result(
            AgentResult.model_validate_json(
                json.dumps(
                    {
                        "fence": claim_b.fence,
                        "state": "failed",
                        "result": {
                            "status": "failed",
                            "error_code": "artifact_distribution_failed",
                            "reason": "Controller distribution could not be verified",
                            "failure_kind": "invalid-authority",
                        },
                    }
                )
            )
        )

    # The second node keeps its lease; the first node's agent restarted and lost it.
    clock.advance(seconds=25)
    jobs.heartbeat(claim_b, None, 30)
    clock.advance(seconds=20)
    if sibling_fails_first:
        # The late restart receipt of the first node arrives after the job ended.
        refuse()
        assert jobs.record_late_result(_restart_interrupted_result(claim_a, kind))
    else:
        assert jobs.record_late_result(_restart_interrupted_result(claim_a, kind))
        refuse()

    assert job_state(sessions, job.id).state == "failed"
    with sessions() as session:
        parked = session.get(AgentOperation, first.id)
        assert parked.state == "cancelled"
        assert parked.next_action_at is None and parked.next_action_at is None
        assert "abandoned" in parked.status_reason
        assert session.get(AgentOperation, second.id).state == "failed"


def test_distribution_parked_with_a_stale_retry_authorisation_resumes_itself(
    service,
) -> None:
    """A Controller upgrade heals operations already parked in the stuck shape.

    No migration: the row is written exactly as the earlier release left it, an
    authorisation for attempt 1 on an expired attempt 2 with no retry time.
    """

    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, kind, COMMIT, {"plan_digest": COMMIT}
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=45)
    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        stored.current_attempt = 2
        attempt.attempt = 2
        attempt.state = "expired"
        attempt.result = {**_RESTART_INTERRUPTED, "operation": kind}
        stored.state = "waiting-for-operator"
        stored.next_action_at = None
        stored.status_reason = "attempt 2 lease expired; the effect is unobserved"
        session.get(Job, stored.parent_job_id).state = "waiting-for-operator"

    resumed = _claim_until_due(jobs, sessions, clock, operation.id)
    assert resumed is not None and fenced_attempt(sessions, resumed).attempt == 3
    assert fenced_operation(sessions, resumed).id == operation.id


def test_non_idempotent_operation_interrupted_by_restart_still_waits_for_operator(
    service,
) -> None:
    """A user job's side effects cannot be proved undone, so it is never replayed."""

    jobs, sessions, clock = service
    vector = json.loads(
        (
            Path(__file__).parents[2]
            / "agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
        ).read_text()
    )
    kind = vector["operation"]
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, kind, COMMIT, vector["payload"]
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=45)
    jobs.record_late_result(_restart_interrupted_result(claim, kind))
    for _ in range(3):
        assert claim_agent(jobs, NODE_A, "serial-a") is None
        clock.advance(seconds=3600)
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        # parked for a person (being observed, never scheduled for a retry)
        assert stored.state in aos.PARKED
        assert retry_scheduled(stored) is None
        assert stored.current_attempt == 1


def test_transient_distribution_failure_recovers_after_repeated_faults_and_restart(
    service,
) -> None:
    from vonk_agent_protocol import AgentResult

    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        kind,
        COMMIT,
        {"plan_digest": COMMIT},
    )
    for attempt_number in range(1, 8):
        claim = claim_agent(
            jobs,
            NODE_A,
            "serial-a",
        )
        assert (
            claim is not None
            and fenced_attempt(sessions, claim).attempt == attempt_number
        ), attempt_number
        body = {key: claim.model_dump(mode="json")[key] for key in ("fence",)}
        jobs.record_result(
            AgentResult.model_validate_json(
                json.dumps(
                    {
                        **body,
                        "state": "failed",
                        "result": {
                            "status": "failed",
                            "error_code": "artifact_distribution_failed",
                            "reason": "controller transport failed",
                            "failure_kind": "temporary-dependency",
                            **(
                                {"retry_after_seconds": 120}
                                if attempt_number == 1
                                else {}
                            ),
                        },
                    }
                )
            )
        )
        with sessions() as session:
            stored = session.get(AgentOperation, operation.id)
            due = stored.next_action_at
            assert stored.current_attempt == attempt_number
            assert stored.state in aos.PARKED
        jobs = AgentJobService(sessions, clock=clock)
        assert (
            claim_agent(
                jobs,
                NODE_A,
                "serial-a",
            )
            is None
        )
        assert due is not None
        if attempt_number == 1:
            assert due.replace(tzinfo=UTC) >= clock.now + timedelta(seconds=120)
        else:
            assert (
                clock.now < due.replace(tzinfo=UTC) <= clock.now + timedelta(seconds=60)
            )
        clock.now = due.replace(tzinfo=UTC) + timedelta(seconds=1)

    recovered = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert (
        recovered is not None
        and fenced_operation(sessions, recovered).id == operation.id
    )
    jobs.succeed(
        recovered,
        {"downloaded_bytes": 0},
    )
    with sessions() as session:
        assert session.get(AgentOperation, operation.id).state == "succeeded"


def test_successful_distribution_receipt_closes_coalesced_final_counters(
    service,
) -> None:
    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        kind,
        COMMIT,
        {"plan_digest": COMMIT},
    )
    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert claim is not None
    jobs.heartbeat(
        claim,
        {
            "phase": "copying",
            "completed_bytes": 100,
            "total_bytes": 200,
            "total_bytes_known": True,
            "completed_items": 1,
            "total_items": 2,
        },
        60,
    )
    jobs.succeed(
        claim,
        {"downloaded_bytes": 200},
    )
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt.progress["completed_bytes"] == 200
        assert attempt.progress["completed_items"] == 2
        assert attempt.progress["phase"] == "completed"


@pytest.mark.parametrize("explicit_null", [False, True])
def test_queue_stores_and_claims_the_canonical_payload_hash(
    service, explicit_null: bool
) -> None:
    from vonk_agent_protocol import canonical_message, canonical_payload

    jobs, sessions, clock = service
    vector = json.loads(
        (
            Path(__file__).parents[2]
            / "agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
        ).read_text()
    )
    payload = vector["payload"]
    job_input = payload["compiled_execution_plan"]["job"]["input"]
    if explicit_null:
        job_input["slots"] = None
    else:
        job_input.pop("slots", None)
    expected = canonical_payload(vector["operation"], payload)
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, vector["operation"], COMMIT, payload
    )
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored.payload == json.loads(expected)
        assert stored.payload_digest == hashlib.sha256(expected).hexdigest()
        assert "slots" not in stored.payload["compiled_execution_plan"]["job"]["input"]
        assert stored.payload["compiled_execution_plan"]["endpoint"] is None
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    assert canonical_message(claim.payload) == expected
    executable = os.environ.get("VONK_CANONICAL_WIRE_PROBE")
    if executable:
        parsed = subprocess.run(
            [executable, "AgentClaim"],
            input=canonical_message(claim),
            capture_output=True,
            check=True,
        )
        assert parsed.stdout == canonical_message(claim)


@pytest.mark.parametrize("include_null", [False, True])
def test_lease_only_wire_heartbeat_retains_measured_progress(
    service, include_null: bool
) -> None:
    from vonk_agent_protocol import AgentProgress, canonical_message

    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.heartbeat(
        claim,
        {
            "phase": "transfer",
            "completed_bytes": 10,
            "total_bytes": 100,
            "total_bytes_known": True,
            "members": [
                {"member_id": NODE_A, "phase": "transfer", "completed_bytes": 10}
            ],
        },
        30,
    )
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        previous = attempt.progress
        previous_deadline = attempt.lease_deadline
    document = {
        key: value
        for key, value in json.loads(canonical_message(claim)).items()
        if key in AgentProgress.model_fields
    }
    if include_null:
        document["progress"] = None
    incoming = AgentProgress.model_validate(document)
    clock.now += timedelta(seconds=5)
    directive = jobs.heartbeat(incoming, incoming.progress, 60)
    assert directive.deadline > claim.deadline
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt.progress == previous
        assert attempt.lease_deadline > previous_deadline
    # A measured snapshot explicitly clears members and declares its total unknown.
    jobs.heartbeat(
        claim,
        {
            "phase": "transfer",
            "completed_bytes": 10,
            "total_bytes_known": False,
            "members": [],
        },
        60,
    )
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt.progress["members"] == []
        assert attempt.progress["total_bytes_known"] is False
        assert "total_bytes" not in attempt.progress


def _supersede_waiting_mutation(
    sessions, clock, parent_job, operation, *, cancel_requested_at: str | None
) -> None:
    """Leave one issued order waiting for a cancellation it can never receive.

    A ``waiting-for-operator`` operation has no live attempt, so the agent can
    never deliver the cancellation receipt the mutating gate otherwise waits
    for.  The parent has already recorded ``cancel_requested``; the timestamp is
    what authorises its bounded cleanup window and is exactly what the buggy
    writers omitted.
    """

    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        old_parent = session.get(Job, parent_job.id)
        assert stored is not None and old_parent is not None
        stored.state = "waiting-for-operator"
        stored.current_attempt = 1
        stored.status_reason = "operation outcome uncertain"
        session.add(
            AgentOperationAttempt(
                operation_id=stored.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=clock.now + timedelta(minutes=1),
                agent_certificate_serial="serial-a",
                state="waiting-for-operator",
            )
        )
        old_parent.state = "running"
        old_parent.result = {
            "cancel_requested": True,
            "cancel_request_id": str(uuid.uuid4()),
            "cancel_actor": "controller",
            "reason": "superseded by newer workload intent",
            **(
                {}
                if cancel_requested_at is None
                else {"cancel_requested_at": cancel_requested_at}
            ),
        }


def _enqueue_successor_mutation(jobs, sessions, clock):
    with sessions.begin() as session:
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 2
    successor_parent = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, successor_parent.id).payload = {"workload_intent_ordinal": 2}
    return jobs.enqueue(
        successor_parent.id,
        NODE_A,
        ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value,
        COMMIT,
        {"plan_digest": COMMIT},
    )


@pytest.mark.parametrize(
    "cancel_requested_at",
    [None, "expired"],
    ids=["disarmed", "expired"],
)
def test_superseded_waiting_mutation_is_reconciled_and_stops_blocking(
    service, cancel_requested_at
) -> None:
    """A cancelled order that can never report back must not wedge the node.

    The old order is waiting for a cancellation receipt, but a
    ``waiting-for-operator`` operation has no live attempt to fence.  When its
    cancellation is disarmed (no parseable ``cancel_requested_at``) or past its
    authorised cleanup window, the wait has no bound, so the claim path drives
    it to its known terminal state instead of blocking later mutations forever.
    """

    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    old = jobs.enqueue(old_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    requested_at = (
        None
        if cancel_requested_at is None
        else (clock.now - timedelta(seconds=700)).isoformat()
    )
    _supersede_waiting_mutation(
        sessions, clock, old_parent, old, cancel_requested_at=requested_at
    )
    new = _enqueue_successor_mutation(jobs, sessions, clock)

    claim = claim_agent(jobs, NODE_A, "serial-a")

    assert claim is not None
    assert fenced_operation(sessions, claim).id == new.id
    with sessions() as session:
        reconciled = session.get(AgentOperation, old.id)
        assert reconciled is not None
        assert reconciled.state == "cancelled"
        assert reconciled.status_reason is not None
        assert "superseded by workload intent 2" in reconciled.status_reason
        assert "intent 1 cancelled" in reconciled.status_reason
        assert (
            "cancel_requested_at missing or unparseable"
            if cancel_requested_at is None
            else "cancellation cleanup deadline elapsed"
        ) in reconciled.status_reason
    assert job_state(sessions, old_parent.id).state == "cancelled"


def test_disarmed_cancellation_records_the_defect(service) -> None:
    """A flag without a usable timestamp disarms cleanup; say so loudly."""

    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    old = jobs.enqueue(old_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    _supersede_waiting_mutation(
        sessions, clock, old_parent, old, cancel_requested_at=None
    )
    _enqueue_successor_mutation(jobs, sessions, clock)

    assert claim_agent(jobs, NODE_A, "serial-a") is not None

    assert (
        job_state(sessions, old_parent.id).status_reason
        == "cancel_requested carried no cancel_requested_at; the superseded "
        "order was reconciled to cancelled"
    )


def test_live_cancellation_still_blocks_later_work(service) -> None:
    """A cancellation inside its cleanup window still fences later mutations."""

    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    old = jobs.enqueue(old_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    _supersede_waiting_mutation(
        sessions,
        clock,
        old_parent,
        old,
        cancel_requested_at=clock.now.isoformat(),
    )
    new = _enqueue_successor_mutation(jobs, sessions, clock)

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        blocked = session.get(AgentOperation, new.id)
        waiting = session.get(AgentOperation, old.id)
        assert blocked is not None and blocked.state == "queued"
        assert waiting is not None and waiting.state in aos.PARKED


def test_live_prior_mutation_still_blocks_later_work(service) -> None:
    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    old = jobs.enqueue(old_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None and fenced_operation(sessions, first).id == old.id
    clock.advance(seconds=1)
    new_parent = parent(sessions, clock)
    new = jobs.enqueue(new_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        blocked = session.get(AgentOperation, new.id)
        running = session.get(AgentOperation, old.id)
        assert blocked is not None and blocked.state == "queued"
        assert running is not None and running.state == "running"


@pytest.mark.parametrize("dead_attempt", ("missing", "stopped"))
def test_dead_running_mutation_is_reconciled_and_stops_blocking(
    service, dead_attempt
) -> None:
    """A running order with no executor must not wedge later work (#811).

    #810 reconciled a superseded waiting order, but the claim gate still treated
    every ``running`` predecessor as live without reading its attempt.  When an
    agent restarts and its attempt row is gone or already stopped, that order
    can never deliver the receipt a wait needs, yet it blocked every later
    mutation on the node forever.  The wrong implementation appends the
    predecessor to ``active_mutations`` unconditionally; this one parks it as
    an unobserved operator wait and admits the successor on the next claim.
    """

    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    old = jobs.enqueue(old_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None and fenced_operation(sessions, first).id == old.id

    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == old.id
            )
        )
        assert attempt is not None
        if dead_attempt == "missing":
            session.delete(attempt)
        else:
            attempt.state = "expired"
    clock.advance(seconds=1)
    new_parent = parent(sessions, clock)
    new = jobs.enqueue(new_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)

    # The first claim reconciles the dead order and refuses itself so the park
    # commits; the next claim must then admit the successor.
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    claimed = claim_agent(jobs, NODE_A, "serial-a")

    assert claimed is not None and fenced_operation(sessions, claimed).id == new.id
    with sessions() as session:
        parked = session.get(AgentOperation, old.id)
        successor = session.get(AgentOperation, new.id)
        assert parked is not None and parked.state in aos.PARKED
        assert parked.status_reason is not None
        assert "the effect is unobserved" in parked.status_reason
        assert (
            "no recorded attempt" in parked.status_reason
            if dead_attempt == "missing"
            else "stopped in state expired" in parked.status_reason
        )
        assert successor is not None and successor.state == "running"


def test_reconciled_dead_running_mutation_retains_its_pending_result(service) -> None:
    """Reconciliation must never discard evidence the Controller has not applied."""

    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    old = jobs.enqueue(old_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None
    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == old.id
            )
        )
        assert attempt is not None
        attempt.result = {"reason": "unacknowledged stop receipt"}
        attempt.state = "expired"
    clock.advance(seconds=1)
    new_parent = parent(sessions, clock)
    jobs.enqueue(new_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)

    assert claim_agent(jobs, NODE_A, "serial-a") is None
    assert claim_agent(jobs, NODE_A, "serial-a") is not None

    with sessions() as session:
        retained = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == old.id
            )
        )
        assert retained is not None
        assert retained.result == {"reason": "unacknowledged stop receipt"}


def test_claim_refusal_records_parent_state_reason(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).state = "succeeded"

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.status_reason is not None
        assert "parent-not-claimable" in stored.status_reason
        assert "succeeded" in stored.status_reason


def test_excluded_work_refusal_names_both_ordinals(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    with sessions.begin() as session:
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 2

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.status_reason is not None
        assert "workload-intent-superseded" in stored.status_reason
        assert "operation_intent=1" in stored.status_reason
        assert "node_intent=2" in stored.status_reason


def test_excluded_work_refusal_records_a_cancelled_parent(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).result = {"cancel_requested": True}

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.status_reason is not None
        assert "parent-cancel-requested" in stored.status_reason


def test_excluded_work_refusal_records_an_unready_retry_attempt(service) -> None:
    """The missing ``retry_ready_attempt`` condition is named on the operator surface.

    A ``waiting-for-operator`` operation whose attempt is not available for a
    retry satisfied every condition the old Python classifier checked, so the
    recorded reason was ``unclassified-unclaimable``.
    """

    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    _scenario_operator_retry_attempt_not_ready(sessions, clock, parent_job, operation)

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.status_reason is not None
        assert "operator-retry-attempt-not-ready" in stored.status_reason


def test_work_enqueued_after_the_claim_query_is_not_reported_as_refused(
    service,
) -> None:
    """A node that polled a moment before an enqueue committed was not refused.

    The claim query and the explanation of an empty claim are separate reads of
    one transaction, so an operation committed in between is fully claimable
    when the explanation looks. It was recorded as ``unclassified-unclaimable``
    on a healthy install or start; it must record nothing and be claimed on
    the next poll.
    """

    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)

    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert jobs._excluded_work_refusal(session, node, clock.now) is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and "refused" not in (stored.status_reason or "")
    assert claim_agent(jobs, NODE_A, "serial-a") is not None


@pytest.mark.parametrize("malformed", (1, "true"))
def test_claim_admits_and_names_a_malformed_cancel_flag(service, malformed) -> None:
    """A non-boolean cancel flag does not cancel, and is not silent either.

    The canonical lifecycle result declares
    ``cancel_requested: Literal[True]``, so a stored ``1`` or ``"true"`` is
    malformed.  The predicate reads it exactly instead of coercing it, so
    legitimate work is not blocked, and the admission path records the
    malformation so reading it as "not cancelled" never becomes a silent
    default.
    """

    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    with sessions.begin() as session:
        job = session.get(Job, parent_job.id)
        assert job is not None
        job.result = {"cancel_requested": malformed}

    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None and fenced_operation(sessions, claim).id == operation.id

    with sessions() as session:
        job = session.get(Job, parent_job.id)
        assert job is not None and job.status_reason is not None
        assert job.status_reason.startswith("claim note: ")
        assert "parent-cancel-flag-malformed" in job.status_reason


def test_no_work_claim_records_no_refusal(service) -> None:
    jobs, sessions, clock = service

    # An empty node has nothing to explain, and a completed operation is not
    # "work this node may not execute", so neither case writes a reason.
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.succeed(claim, STOP_RESULT)

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        assert session.scalars(select(AgentOperation)).all() != []
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.status_reason is None


def _claim_refusal_check_names() -> tuple[str, ...]:
    """Every named condition of the authoritative claim predicate, in order.

    The cases below are derived from the predicate itself rather than from a
    hand-written list, so adding a condition without a scenario is a test
    failure instead of a silently unclassified refusal.
    """

    predicate = _claim_predicate(datetime(2026, 8, 3, tzinfo=UTC))
    names = [condition.check for condition in predicate.diagnostics]
    names.extend(condition.check for condition in predicate.common)
    for branch in predicate.branches:
        names.extend(condition.check for condition in branch.conditions)
    return tuple(names)


def _set_operation(sessions, operation: AgentOperation, **fields: object) -> None:
    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        for name, value in fields.items():
            setattr(stored, name, value)


def _add_attempt(
    sessions, operation: AgentOperation, clock, *, state: str, lease_seconds: int
) -> None:
    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        session.add(
            AgentOperationAttempt(
                operation_id=operation.id,
                attempt=stored.current_attempt,
                fence=str(uuid.uuid4()),
                lease_deadline=clock.now + timedelta(seconds=lease_seconds),
                agent_certificate_serial="serial-a",
                state=state,
            )
        )


def _scenario_parent_job_missing(sessions, clock, parent_job, operation) -> None:
    _set_operation(sessions, operation, parent_job_id=str(uuid.uuid4()))


def _scenario_workload_intent_superseded(
    sessions, clock, parent_job, operation
) -> None:
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.workload_intent_ordinal = operation.workload_intent_ordinal + 1


def _scenario_parent_cancel_requested(sessions, clock, parent_job, operation) -> None:
    with sessions.begin() as session:
        job = session.get(Job, parent_job.id)
        assert job is not None
        job.result = {"cancel_requested": True}


def _scenario_parent_cancel_flag_malformed(
    sessions, clock, parent_job, operation
) -> None:
    with sessions.begin() as session:
        job = session.get(Job, parent_job.id)
        assert job is not None
        job.result = {"cancel_requested": 1}


def _scenario_queued_attempt_not_zero(sessions, clock, parent_job, operation) -> None:
    _set_operation(sessions, operation, current_attempt=1)


def _scenario_running_attempt_missing(sessions, clock, parent_job, operation) -> None:
    _set_operation(sessions, operation, state="running", current_attempt=1)


def _scenario_running_attempt_not_running(
    sessions, clock, parent_job, operation
) -> None:
    _set_operation(sessions, operation, state="running", current_attempt=1)
    _add_attempt(sessions, operation, clock, state="failed", lease_seconds=60)


def _scenario_running_lease_live(sessions, clock, parent_job, operation) -> None:
    _set_operation(sessions, operation, state="running", current_attempt=1)
    _add_attempt(sessions, operation, clock, state="running", lease_seconds=60)


def _scenario_operator_retry_not_authorized(
    sessions, clock, parent_job, operation
) -> None:
    _set_operation(sessions, operation, state="waiting-for-operator", current_attempt=1)


def _scenario_upgrade_safety_not_elapsed(
    sessions, clock, parent_job, operation
) -> None:
    _set_operation(
        sessions,
        operation,
        kind=ProtocolAgentOperation.AGENT_UPGRADE.value,
        state="waiting-for-operator",
        current_attempt=1,
        next_action_at=clock.now,
    )
    _add_attempt(sessions, operation, clock, state="running", lease_seconds=60)


def _scenario_operator_retry_not_due(sessions, clock, parent_job, operation) -> None:
    _set_operation(
        sessions,
        operation,
        state="waiting-for-operator",
        current_attempt=1,
        next_action_at=clock.now + timedelta(seconds=60),
    )


def _scenario_operator_retry_attempt_not_ready(
    sessions, clock, parent_job, operation
) -> None:
    _set_operation(
        sessions,
        operation,
        state="waiting-for-operator",
        current_attempt=1,
        next_action_at=clock.now,
    )


#: One scenario per condition the predicate can fail.  Each leaves exactly one
#: named condition false, so the classifier must name that condition.
_REFUSAL_SCENARIOS: dict[str, Callable[..., None]] = {
    "parent-cancel-flag-malformed": _scenario_parent_cancel_flag_malformed,
    "parent-job-missing": _scenario_parent_job_missing,
    "workload-intent-superseded": _scenario_workload_intent_superseded,
    "parent-cancel-requested": _scenario_parent_cancel_requested,
    "queued-attempt-not-zero": _scenario_queued_attempt_not_zero,
    "running-attempt-missing": _scenario_running_attempt_missing,
    "running-attempt-not-running": _scenario_running_attempt_not_running,
    "running-lease-live": _scenario_running_lease_live,
    "operator-retry-not-authorized": _scenario_operator_retry_not_authorized,
    "upgrade-safety-not-elapsed": _scenario_upgrade_safety_not_elapsed,
    "operator-retry-not-due": _scenario_operator_retry_not_due,
    "operator-retry-attempt-not-ready": _scenario_operator_retry_attempt_not_ready,
}


def test_claim_refusal_scenarios_cover_the_predicate() -> None:
    """Every predicate condition has a scenario, and no scenario is stale."""

    assert set(_REFUSAL_SCENARIOS) == set(_claim_refusal_check_names())


@pytest.mark.parametrize("check", _claim_refusal_check_names())
def test_excluded_work_refusal_names_every_predicate_condition(service, check) -> None:
    """An operation failing only one predicate condition is never opaque.

    The old classifier restated the predicate in Python, so it missed
    ``retry_ready_attempt`` and coerce-differently ``cancel_requested`` values
    and fell through to ``unclassified-unclaimable``.  Deriving from the one
    predicate makes that fallback unreachable for any modelled condition.
    """

    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    _REFUSAL_SCENARIOS[check](sessions, clock, parent_job, operation)

    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        excluded = jobs._excluded_work_refusal(session, node, clock.now)

    assert excluded is not None
    _, refusal, _ = excluded
    assert refusal == check
    assert refusal != "unclassified-unclaimable"


@pytest.mark.parametrize(
    "condition",
    [
        "temporary",
        "expired",
        "cancelled",
        "revoked",
        "superseded",
        "invalid-authority",
        "integrity-failure",
        "unknown",
    ],
)
def test_existing_exhausted_exact_intent_rearms_only_with_current_safe_evidence(
    service, condition
):
    """Reconcile valid persisted exhaustion without reviving obsolete authority."""
    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    job = parent(sessions, clock)
    operation = jobs.enqueue(
        job.id,
        NODE_A,
        kind,
        COMMIT,
        {"plan_digest": COMMIT},
    )
    for _ in range(5):
        claim = claim_agent(
            jobs,
            NODE_A,
            "serial-a",
        )
        assert claim is not None
        jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        **{
                            key: claim.model_dump(mode="json")[key]
                            for key in ("fence",)
                        },
                        "state": "failed",
                        "result": {
                            "status": "failed",
                            "error_code": "artifact_distribution_failed",
                            "reason": "NAS transport unavailable",
                            "failure_kind": "temporary-dependency",
                        },
                    }
                )
            )
        )
        with sessions() as session:
            row = session.get(AgentOperation, operation.id)
            assert row is not None and row.next_action_at is not None
            clock.now = row.next_action_at.replace(tzinfo=UTC) + timedelta(seconds=1)
    with sessions.begin() as session:
        row = session.get(AgentOperation, operation.id)
        assert row is not None
        original_payload = dict(row.payload)
        # The prior finite policy legitimately persisted this current-schema
        # state after its fifth interrupted attempt.
        row.next_action_at = None
        parent_row = session.get(Job, job.id)
        assert parent_row is not None
        parent_row.state = "waiting-for-operator"
        last = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == 5,
            )
        )
        assert last is not None
        if condition == "expired":
            last.state = "expired"
            last.result = None
            last.lease_deadline = clock.now - timedelta(seconds=1)
        elif condition == "cancelled":
            parent_row.result = {"cancel_requested": True}
        elif condition in {"revoked", "superseded"}:
            node = session.get(AgentNode, NODE_A)
            assert node is not None
            if condition == "revoked":
                node.revoked_at = clock.now
            else:
                node.workload_intent_ordinal += 1
        elif condition in {"invalid-authority", "integrity-failure", "unknown"}:
            last.result = {
                "status": "failed",
                "error_code": "artifact_distribution_failed",
                "reason": "Blocked",
                **({"failure_kind": condition} if condition != "unknown" else {}),
            }
    jobs = AgentJobService(sessions, clock=clock)
    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
        )
        is None
    )
    with sessions() as session:
        row = session.get(AgentOperation, operation.id)
        assert row is not None
        due = row.next_action_at
        assert row.current_attempt == 5 and row.payload == original_payload
        if condition not in {"temporary", "expired"}:
            assert due is None
            return
        assert due is not None
        assert clock.now < due.replace(tzinfo=UTC) <= clock.now + timedelta(seconds=60)
    clock.now = due.replace(tzinfo=UTC)
    jobs = AgentJobService(sessions, clock=clock)
    resumed = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert (
        resumed is not None
        and fenced_operation(sessions, resumed).id == operation.id
        and fenced_attempt(sessions, resumed).attempt == 6
    )


def test_a_start_budget_begins_when_the_start_is_first_dispatched(service) -> None:
    # Wrong implementation: the deadline was fixed when the start was queued, so
    # a start that waited behind a long mutating operation on its Spark (a model
    # copy of 4947 s against a 3600 s budget) reached the agent already expired.
    jobs, sessions, clock = service
    queued_at = clock.now
    budget = timedelta(seconds=3600)
    payload = canonical_start_payload(start_deadline=queued_at + budget)
    holder = parent(sessions, clock)
    operation = jobs.enqueue(holder.id, NODE_A, "recipe.start", COMMIT, payload)
    with sessions.begin() as session:
        stored_parent = session.get(Job, holder.id)
        assert stored_parent is not None
        document = {
            **stored_parent.payload,
            "start_deadline": payload["start_deadline"],
            "phases": [[{"node_id": NODE_A, "payload": payload}]],
        }
        stored_parent.payload = document
        stored_parent.payload_digest = hashlib.sha256(
            canonical_message(document)
        ).hexdigest()

    clock.advance(seconds=4947)
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.not_after = clock.now + timedelta(hours=3)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    anchored = (clock.now + budget).isoformat()
    assert claim.payload["start_deadline"] == anchored
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        assert stored.payload["start_deadline"] == anchored
        assert (
            stored.payload_digest
            == hashlib.sha256(
                canonical_payload(ProtocolAgentOperation.RECIPE_START, stored.payload)
            ).hexdigest()
        )
        stored_parent = session.get(Job, holder.id)
        assert stored_parent is not None
        assert stored_parent.payload["start_deadline"] == anchored
        assert stored_parent.payload["phases"][0][0]["payload"]["start_deadline"] == (
            anchored
        )
        assert (
            stored_parent.payload_digest
            == hashlib.sha256(canonical_message(stored_parent.payload)).hexdigest()
        )


def test_a_prompt_start_keeps_its_queued_deadline(service) -> None:
    jobs, sessions, clock = service
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(seconds=90))
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "recipe.start", COMMIT, payload)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    assert claim.payload["start_deadline"] == payload["start_deadline"]
