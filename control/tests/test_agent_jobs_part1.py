from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from threading import Event

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol import (
    AgentProgress,
    AgentResult,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    canonical_message,
)
from vonk_control import agent_operation_states as aos
from vonk_control.agent_jobs import (
    AgentJobService,
)
from vonk_control.agent_operation_facts import START_STALL_WINDOW, job_start_deadline
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.inventory.items import (
    inventory_items,
    runtime_value,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RecipeBuild,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime.adapters import resolve_runtime_adapter
from vonk_control.workload_intent import (
    allocate_workload_intent,
    take_workload_intent,
)

from . import agent_jobs_support as _support
from .agent_fences import fenced_attempt, fenced_operation
from .agent_jobs_support import (
    COMMIT,
    NODE_A,
    NODE_B,
    STOP_PAYLOAD,
    STOP_RESULT,
    canonical_start_payload,
    canonical_start_result,
    distribution_payload,
    job_state,
    parent,
    superseded_order_payload,
)
from .node_contacts import contact_row
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    claim_agent,
)

service = _support.service


_BUILD_ADAPTER = (
    resolve_runtime_adapter("vllm", {"node_count": 1}).to_wire().model_dump(mode="json")
)


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
    with pytest.raises(Exception):  # noqa: B017 -- no dispatch and repaired admission are the witnesses
        jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    fresh = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.RECIPE_STOP.value,
        COMMIT,
        STOP_PAYLOAD,
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None and fenced_operation(sessions, claim).id == fresh.id
    jobs.succeed(claim, STOP_RESULT)
    assert job_state(sessions, fresh.parent_job_id).state == LifecycleState.SUCCEEDED


def test_newer_workload_intent_fences_old_enqueues_renewals_and_results(
    service,
) -> None:
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, job.id).payload = {"workload_intent_ordinal": 1}
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 2
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        jobs.enqueue(
            job.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
        )
    with sessions.begin() as session:
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 1
    jobs.enqueue(
        job.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    with sessions.begin() as session:
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 2
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        jobs.heartbeat(claim, None, 30)
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        jobs.succeed(claim, {})


def test_an_order_numbered_from_a_stale_read_is_refused_and_a_fresh_intent_admitted(
    service,
) -> None:
    """The enqueue check: a plain read at N loses to an intent N+1 that commits.

    The numbering is a conditional UPDATE, so the stale writer changes nothing; the
    order it would have issued at N is refused at enqueue with the typed
    SUPERSEDED refusal (the claim predicate, not tested here, is the second
    fence); a fresh read and allocation is admitted and claimable.
    """

    jobs, sessions, clock = service
    stale_parent = parent(sessions, clock)
    with sessions() as session:
        seen = session.get(AgentNode, NODE_A).workload_intent_ordinal
    with sessions.begin() as session:
        winner = allocate_workload_intent(session, (NODE_A,))
    assert winner == seen + 1
    with sessions.begin() as session:
        _, taken = take_workload_intent(session, {NODE_A: seen})
    assert taken == ()
    with pytest.raises(InvalidRequestError) as refused:
        jobs.enqueue(
            stale_parent.id,
            NODE_A,
            "recipe.start",
            COMMIT,
            superseded_order_payload(clock),
        )
    assert refused.value.typed_reason is InvalidRequestReason.SUPERSEDED
    with sessions.begin() as session:
        fresh = allocate_workload_intent(session, (NODE_A,))
    assert fresh == winner + 1
    fresh_parent = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, fresh_parent.id).payload = {"workload_intent_ordinal": fresh}
    jobs.enqueue(
        fresh_parent.id,
        NODE_A,
        "recipe.start",
        COMMIT,
        superseded_order_payload(clock),
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None


def test_new_intent_cancels_issued_order_and_receives_exact_stop_ack(service) -> None:
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    running = jobs.enqueue(
        job.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
    queued = jobs.enqueue(
        job.id,
        NODE_B,
        "recipe.start",
        COMMIT,
        superseded_order_payload(clock),
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
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        jobs.succeed(claim, {})
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
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
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


def test_an_intent_that_fences_no_spark_cancels_nothing(service) -> None:
    """Liveness seed 57: a recovery that changes no Spark fences none; it is
    admitted, not refused on every retry."""

    jobs, sessions, clock = service
    job = parent(sessions, clock)
    queued = jobs.enqueue(
        job.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
    with sessions.begin() as session:
        jobs.request_superseded_workload_cancellation_in_session(
            session, (), 2, clock.now
        )
    with sessions() as session:
        queued_row = session.get(AgentOperation, queued.id)
        assert queued_row is not None and queued_row.state == "queued"


def test_new_intent_cancels_an_order_by_its_children_never_its_targets(
    service,
) -> None:
    """A damaged ``targets`` list neither hides an older order on the newer
    intent's Spark nor pulls one from another Spark into its cancellation."""

    jobs, sessions, clock = service
    inside = parent(sessions, clock)
    running = jobs.enqueue(
        inside.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    outside = parent(sessions, clock)
    queued = jobs.enqueue(
        outside.id, NODE_B, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
    with sessions.begin() as session:
        inside_row = session.get(Job, inside.id)
        outside_row = session.get(Job, outside.id)
        node = session.get(AgentNode, NODE_A)
        assert inside_row is not None and outside_row is not None
        assert node is not None
        inside_row.targets = ["another-spark"]
        outside_row.targets = [NODE_A]
        node.workload_intent_ordinal = 2
        jobs.request_superseded_workload_cancellation_in_session(
            session, (NODE_A,), 2, clock.now
        )

    with sessions() as session:
        inside_row = session.get(Job, inside.id)
        outside_row = session.get(Job, outside.id)
        running_row = session.get(AgentOperation, running.id)
        queued_row = session.get(AgentOperation, queued.id)
        assert inside_row is not None and isinstance(inside_row.result, dict)
        assert inside_row.result["cancel_requested"] is True
        assert running_row is not None and running_row.state == "running"
        # The order on the other Spark is outside the newer intent: untouched.
        assert outside_row is not None and outside_row.state == "queued"
        assert outside_row.result is None
        assert queued_row is not None and queued_row.state == "queued"


def test_new_intent_finishes_superseded_parent_with_mixed_terminal_children(
    service,
) -> None:
    """A succeeded rank plus a cancelled rank must not leave its parent running."""

    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    succeeded = jobs.enqueue(
        old_parent.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
    cancelled = jobs.enqueue(
        old_parent.id,
        NODE_B,
        "recipe.start",
        COMMIT,
        superseded_order_payload(clock),
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
        parent_row.state = "needs-operator"
        succeeded_row.state = "succeeded"
        parked_row.state = "needs-operator"
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
        first_attempt.state, first_attempt.observation_cause = (
            "observing",
            "lease-lapsed",
        )
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
    payload = distribution_payload()
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
        assert inventory_items(node).preflight_fingerprint == "e" * 64
        assert contact_row(node).protocol_version == 4


def test_service_claim_requires_packaged_runtime_identity(
    service,
) -> None:
    jobs, _sessions, _clock = service

    with pytest.raises(TypeError):
        jobs.claim(
            NODE_A,
            "serial-a",
        )

    with pytest.raises(ValueError):
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=None,
        )


def test_package_operation_is_not_a_control_plane_queue_operation(service) -> None:
    jobs, sessions, clock = service

    with pytest.raises(ValueError):
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


@pytest.mark.usefixtures("damaged_json_rows")
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
        assert (
            node is not None
            and runtime_value(node, lambda identity: identity.binary_digest) == "e" * 64
        )
        assert build is not None and build.state == "failed"
        assert reservation is not None and reservation.state == "released"


@pytest.mark.usefixtures("damaged_json_rows")
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
        waiting = pool.submit(claim_agent, jobs, NODE_A, "serial-a", 30.0)
        time.sleep(0.05)
        operation = jobs.enqueue(
            parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
        )
        claim = waiting.result(timeout=30)
    elapsed = time.monotonic() - started

    assert claim is not None and fenced_operation(sessions, claim).id == operation.id
    # Woken by the enqueue, far before its own 30 s long-poll timeout.
    assert elapsed < 10

    timeout_started = time.monotonic()
    assert claim_agent(jobs, NODE_B, "serial-b", 0.08) is None
    timeout_elapsed = time.monotonic() - timeout_started
    # The lower bound proves the requested long-poll timeout is honored; the
    # upper bound only tells a timeout that fired from one that never fires.
    assert 0.06 <= timeout_elapsed < 10


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

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
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
        assert attempt.lease_deadline is not None
        assert attempt.result is None
        node = session.get(AgentNode, NODE_A)
        assert node is not None

    assert stored is not None and stored.state in aos.PARKED
    reason = stored.status_reason
    assert reason is not None


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
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
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

    with pytest.raises(ValueError):
        jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            "recipe.stop",
            COMMIT,
            {"command": "uname"},
        )
    with pytest.raises(ValueError):
        jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            "recipe.stop",
            COMMIT,
            {"value": "x" * 70_000},
        )


@pytest.mark.parametrize(
    "terminal_state", ("succeeded", "failed", "needs-operator", "cancelled")
)
def test_sqlite_enqueue_rejects_terminal_parent(service, terminal_state: str) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).state = terminal_state  # type: ignore[union-attr]

    with pytest.raises(ValueError):
        jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)


def test_sqlite_enqueue_enforces_parent_commit_and_target(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", "b" * 64, STOP_PAYLOAD)
    with sessions.begin() as session:
        stored_parent = session.get(Job, parent_job.id)
        assert stored_parent is not None
        stored_parent.targets = [NODE_A]
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
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

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
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


def test_heartbeat_with_progress_that_regresses_still_renews_the_lease(
    service,
) -> None:
    """Progress is optional evidence on a lease heartbeat.

    Catches refusing the heartbeat (and so losing the lease) because the
    progress document went backwards: it is dropped, the lease is renewed.
    """

    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.heartbeat(
        claim,
        {
            "phase": "copying",
            "completed_bytes": 10,
            "checkpoint": {"key": "one-attempt", "sequence": 0},
        },
        60,
    )
    clock.advance(seconds=30)

    directive = jobs.heartbeat(
        claim,
        {
            "phase": "copying",
            "completed_bytes": 3,
            "checkpoint": {"key": "one-attempt", "sequence": 0},
        },
        60,
    )

    assert directive.deadline >= clock.now + timedelta(seconds=60)
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None
        assert attempt.progress["completed_bytes"] == 10


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
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        jobs.heartbeat(claim, {"phase": "rank-launch"}, 30)


def test_a_progressing_start_outlives_its_accepted_deadline_and_a_stalled_one_ends(
    service,
) -> None:
    # Wrong implementation: the accepted start deadline was a fixed total, so a
    # model load that kept reporting progress (its log growing) was refused at
    # that deadline -- a big model could never take longer than an hour.
    jobs, sessions, clock = service
    owner = parent(sessions, clock)
    accepted = clock.now + timedelta(minutes=30)
    jobs.enqueue(
        owner.id,
        NODE_A,
        "recipe.start",
        COMMIT,
        canonical_start_payload(start_deadline=accepted),
    )
    with sessions.begin() as session:
        # The agent's identity outlives the load, so only the start's own
        # deadline can refuse it.
        certificate = session.get(AgentCertificate, "serial-a")
        certificate.not_after = clock.now + timedelta(hours=12)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None

    # Three hours of load, three stall windows, progress reported every twenty
    # minutes while the Controller was unreachable each time (the 30 s lease
    # always lapsed in between).
    for items in range(1, 10):
        clock.advance(seconds=20 * 60)
        renewed = jobs.heartbeat(
            claim, {"phase": "verifying", "completed_items": items}, 30
        )
        assert renewed.cancel_requested is False
    with sessions() as session:
        job = session.get(Job, owner.id)
        assert job is not None
        # The job-level deadline (the start's failure and Run/Switch final
        # verification) moves with the same progress.
        assert job_start_deadline(job, accepted, clock.now) == (
            clock.now + START_STALL_WINDOW
        )

    # The load stops progressing: a full window later the start is spent.
    clock.advance(seconds=int(START_STALL_WINDOW.total_seconds()) + 1)
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        jobs.heartbeat(claim, None, 30)
    with sessions() as session:
        job = session.get(Job, owner.id)
        assert job is not None
        assert job_start_deadline(job, accepted, clock.now) < clock.now


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
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
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
