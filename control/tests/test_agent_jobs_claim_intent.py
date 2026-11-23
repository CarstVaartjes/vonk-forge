"""Agent jobs: claim intent."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import select

from vonk_agent_protocol.agent_operations import (
    AgentOperation as ProtocolAgentOperation,
)
from vonk_agent_protocol.contracts import AgentProgress, AgentResult
from vonk_agent_protocol.lifecycle_vocabulary import (
    InvalidRequestReason,
    LifecycleState,
)
from vonk_agent_protocol.outcome import InvalidRequestError
from vonk_control import agent_operation_states as aos
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.install_admission.service import InstallAdmissionService
from vonk_control.inventory.items import (
    inventory_items,
    runtime_value,
)
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.execution import RecipeRun, ResourceReservation
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.recipe_operations.service import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime.adapters import resolve_runtime_adapter
from vonk_control.workload_intent import (
    allocate_workload_intent,
    take_workload_intent,
)

from .agent_fences import fenced_operation
from .agent_jobs_support import (
    COMMIT,
    NODE_A,
    NODE_B,
    STOP_PAYLOAD,
    STOP_RESULT,
    canonical_start_payload,
    distribution_payload,
    exercise_upgrade_reconnect,
    job_state,
    parent,
    superseded_order_payload,
)
from .node_contacts import contact_row
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    claim_agent,
)

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
    assert claim is not None
    assert fenced_operation(sessions, claim).id == fresh.id
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
        assert queued_row is not None
        assert queued_row.state == "cancelled"
        assert running_row is not None
        assert running_row.state == "running"
        assert parent_row is not None
        assert parent_row.result is not None
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
    assert fresh_claim is not None
    assert fenced_operation(sessions, fresh_claim).id == fresh.id
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
        assert running_row is not None
        assert running_row.state == "cancelled"
        assert parent_row is not None
        assert parent_row.state == "cancelled"
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
        assert queued_row is not None
        assert queued_row.state == "queued"


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
        assert inside_row is not None
        assert outside_row is not None
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
        assert inside_row is not None
        assert isinstance(inside_row.result, dict)
        assert inside_row.result["cancel_requested"] is True
        assert running_row is not None
        assert running_row.state == "running"
        # The order on the other Spark is outside the newer intent: untouched.
        assert outside_row is not None
        assert outside_row.state == "queued"
        assert outside_row.result is None
        assert queued_row is not None
        assert queued_row.state == "queued"


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
        assert parent_row is not None
        assert succeeded_row is not None
        assert cancelled_row is not None
        assert node_a is not None
        assert node_b is not None
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
        assert parent_row is not None
        assert succeeded_row is not None
        assert parked_row is not None
        assert node_a is not None
        assert node_b is not None
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
        assert parent_row is not None
        assert parent_row.state == "cancelled"
        assert parked_row is not None
        assert parked_row.state == "cancelled"


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
        assert node is not None
        assert row is not None
        assert first_attempt is not None
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
        assert row is not None
        assert row.current_attempt == 2
        assert row.state == "running"
        assert newer is not None
        assert newer.state == "running"
        assert newer.result is None


@pytest.mark.parametrize("older_work", [None, "exact-retry", "running"])
def test_agent_upgrade_completes_only_after_exact_new_runtime_reconnects(
    service,
    older_work,
) -> None:
    exercise_upgrade_reconnect(service, older_work)


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
        assert stored is not None
        assert stored.state == "queued"


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
        assert node is not None
        assert runtime_value(node, lambda identity: identity.binary_digest) == "e" * 64
        assert build is not None and build.state == "failed"
        assert reservation is not None
        assert reservation.state == "released"


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

    assert claim is not None
    assert fenced_operation(sessions, claim).id == operation.id
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

    jobs._claim_once = observed_claim_once
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

    assert claim is not None
    assert fenced_operation(sessions, claim).id == operation.id


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
        assert stored is not None
        assert stored.state in aos.PARKED
