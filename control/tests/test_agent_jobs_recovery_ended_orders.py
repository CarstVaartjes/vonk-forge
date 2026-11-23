"""Agent jobs: recovery ended orders."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update

from vonk_agent_protocol.agent_operations import (
    AgentFailureKind,
    AgentFailureResult,
    ArtifactDistributionResult,
)
from vonk_agent_protocol.agent_operations import (
    AgentOperation as ProtocolAgentOperation,
)
from vonk_agent_protocol.contracts import AgentResult, canonical_payload
from vonk_agent_protocol.inventory import GpuHealth
from vonk_agent_protocol.lifecycle_vocabulary import (
    AgentResultState,
    FailureCode,
    LifecycleState,
    ObservationCause,
    OutcomeKind,
)
from vonk_agent_protocol.outcome import OutcomeFailed
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode
from vonk_agent_protocol.reason_codes.runs import SupersedeCode
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.agent_jobs.retirement import _release_retired_owner_in_session
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.agent_jobs.stored import column_message, column_value
from vonk_control.agent_jobs.waits import record_queue_waits
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.jobs.documents import RecipeBuildParent, RecipeStartParent
from vonk_control.lifecycle.agent_operation import (
    AgentOperationAdapter,
)
from vonk_control.lifecycle.evidence import Residue
from vonk_control.lifecycle.types import Outcome
from vonk_control.models.execution import RecipeRun
from vonk_control.models.fleet import AgentCertificate, AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.recipe_operations.lifecycle_contract import (
    RecipeOperationCancellationResult,
)
from vonk_control.run_switch_operations.child_wake import ChildEndWake
from vonk_control.runtime.preflight import (
    admission_blockers,
    latest_result,
    request_digest,
)

from .agent_fences import fenced_attempt, fenced_operation
from .agent_jobs_support import (
    COMMIT,
    NODE_A,
    NODE_B,
    STOP_PAYLOAD,
    STOP_RESULT,
    _claim_until_due,
    _enqueue_successor_mutation,
    _restart_interrupted_result,
    canonical_start_payload,
    distribution_payload,
    job_state,
    parent,
    superseded_order_payload,
)
from .node_contacts import contact_row
from .non_blocking import assert_ended_without_blocking, assert_no_orphaned_holds
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import (
    claim_agent,
)
from .test_runtime_preflight import request as preflight_request
from .test_runtime_preflight import result as passing_result


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
def test_terminal_request_is_not_revived_by_stale_recovery_evidence(service, condition):
    """A definite terminal result cannot be reopened by stale retry evidence."""

    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    job = parent(sessions, clock)
    operation = jobs.enqueue(
        job.id,
        NODE_A,
        kind,
        COMMIT,
        distribution_payload(),
    )
    # A security refusal ends immediately; stale evidence cannot reopen it.
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.FAILED,
            result=OutcomeFailed(
                kind=OutcomeKind.FAILED,
                code=FailureCode.ARTIFACT_DISTRIBUTION_FAILED,
                reason="distribution authority denied",
                failure_kind=AgentFailureKind.INVALID_AUTHORITY,
            ),
        )
    )
    with sessions.begin() as session:
        row = session.get(AgentOperation, operation.id)
        assert row is not None
        original_payload = dict(row.payload)
        assert row.state == LifecycleState.FAILED.value
        row.next_action_at = None
        parent_row = session.get(Job, job.id)
        assert parent_row is not None
        parent_row.state = LifecycleState.FAILED.value
        last = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == 1,
            )
        )
        assert last is not None
        failure = AgentFailureResult.model_validate_json(json.dumps(last.result))
        reason_code = failure.error_code
        assert reason_code == FailureCode.ARTIFACT_DISTRIBUTION_FAILED
        # Even retryable or uncertain attempt evidence cannot revive this end.
        failure = failure.model_copy(
            update={"failure_kind": AgentFailureKind.TEMPORARY_DEPENDENCY}
        )
        last.result = json.loads(failure.model_dump_json(exclude_none=True))
        if condition == "expired":
            last.state = LifecycleState.OBSERVING.value
            last.observation_cause = ObservationCause.LEASE_LAPSED.value
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
            failure = failure.model_copy(
                update={
                    "failure_kind": AgentFailureKind(condition)
                    if condition != "unknown"
                    else None
                }
            )
            last.result = json.loads(failure.model_dump_json(exclude_none=True))
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
        assert row.current_attempt == 1
        assert row.payload == original_payload
        assert due is None
        assert row.state == LifecycleState.FAILED.value
    if condition in {"revoked", "superseded"}:
        with sessions.begin() as session:
            node = session.get(AgentNode, NODE_A)
            assert node is not None
            node.revoked_at = None
            node.workload_intent_ordinal = operation.workload_intent_ordinal
    fresh = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, kind, COMMIT, distribution_payload()
    )
    admitted = claim_agent(jobs, NODE_A, "serial-a")
    assert admitted is not None
    assert fenced_operation(sessions, admitted).id == fresh.id
    assert fenced_attempt(sessions, admitted).attempt == 1
    jobs.succeed(admitted, ArtifactDistributionResult(downloaded_bytes=0))


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
        # A distributed start's parent is a recipe.start job, whose stored
        # document is the contract the anchoring reads.
        stored_parent.kind = "recipe.start"
        document = {
            "schema_version": 1,
            "owner_kind": "run",
            "owner_id": str(uuid.uuid4()),
            "plan_digest": COMMIT,
            "workload_intent_ordinal": 1,
            "start_deadline": payload["start_deadline"],
            "phases": [
                [
                    {
                        "operation_id": operation.id,
                        "node_id": NODE_A,
                        "payload": payload,
                    }
                ]
            ],
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
        # The parent stores its own deadline as a timestamp (its contract's
        # spelling), the phase payloads as the wire string they carry.
        assert datetime.fromisoformat(
            stored_parent.payload["start_deadline"]
        ) == datetime.fromisoformat(anchored)
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


def test_wrong_fence_cannot_finish_work_and_current_fence_can(service) -> None:
    """Catches forged authority ending a legitimate attempt or blocking its successor."""
    jobs, sessions, clock = service
    order = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.RECIPE_STOP.value,
        COMMIT,
        STOP_PAYLOAD,
    )
    fence = claim_agent(jobs, NODE_A, "serial-a")
    assert fence is not None
    forged = fence.model_copy(update={"fence": str(uuid.uuid4())})
    with pytest.raises(Exception):  # noqa: B017 -- unchanged effect and exact completion establish refusal
        jobs.succeed(forged, STOP_RESULT)
    assert fenced_operation(sessions, fence).state == LifecycleState.RUNNING
    jobs.succeed(fence, STOP_RESULT)
    assert job_state(sessions, order.parent_job_id).state == LifecycleState.SUCCEEDED
    successor = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.RECIPE_STOP.value,
        COMMIT,
        STOP_PAYLOAD,
    )
    fresh = claim_agent(jobs, NODE_A, "serial-a")
    assert fresh is not None
    assert fenced_operation(sessions, fresh).id == successor.id
    jobs.succeed(fresh, STOP_RESULT)
    assert (
        job_state(sessions, successor.parent_job_id).state == LifecycleState.SUCCEEDED
    )


def test_cancelled_build_waits_for_platform_cleanup_as_an_observation(service) -> None:
    """A rebuildable child cannot turn pending cleanup into a human gate."""

    jobs, sessions, clock = service
    job = parent(sessions, clock)
    with sessions.begin() as session:
        stored = session.get(Job, job.id)
        assert stored is not None
        stored.kind = ProtocolAgentOperation.RECIPE_BUILD.value
        stored.payload = RecipeBuildParent(
            schema_version=1,
            owner_kind="recipe-build",
            owner_id=str(uuid.uuid4()),
            plan_digest=COMMIT,
        ).model_dump(mode="json", exclude_none=True)
        stored.payload_digest = hashlib.sha256(
            canonical_message(stored.payload)
        ).hexdigest()
        stored.result = RecipeOperationCancellationResult(
            cancel_requested=True,
            cancel_requested_at=clock.now,
            cancel_request_id=str(uuid.uuid4()),
            cancel_actor="operator",
            reason="stop this build",
        ).model_dump(mode="json", exclude_none=True)
        jobs._aggregate_parent_state(session, job.id)
    assert job_state(sessions, job.id).state == "observing"


@pytest.mark.parametrize("action", ["heartbeat", "result"])
@pytest.mark.parametrize("requested_at", [None, "expired"])
def test_ended_superseded_authority_releases_the_queue(service, action, requested_at):
    """An exhausted cancellation must not raise repeatedly or retain a live claim."""

    jobs, sessions, clock = service
    owner = parent(sessions, clock)
    operation = jobs.enqueue(
        owner.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    with sessions.begin() as session:
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 2
        session.get(Job, owner.id).result = {
            "cancel_requested": True,
            **(
                {
                    "cancel_requested_at": (
                        clock.now - timedelta(seconds=700)
                    ).isoformat()
                }
                if requested_at
                else {}
            ),
        }

    def end(_):
        if action == "heartbeat":
            directive = jobs.heartbeat(claim, None, 30)
            assert directive.cancel_requested
            assert directive.deadline == clock.now
        else:
            jobs._finish(claim, "cancelled", result=None, reason="superseded")
        with sessions() as session:
            return session.get(AgentOperation, operation.id)

    def fresh(_):
        new = _enqueue_successor_mutation(jobs, sessions, clock)
        receipt = claim_agent(jobs, NODE_A, "serial-a")
        assert receipt is not None
        assert fenced_operation(sessions, receipt).id == new.id
        with sessions() as session:
            return session.get(AgentOperation, new.id)

    def assert_reason(row):
        assert row.status_reason is not None

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        operation,
        end=end,
        fresh=fresh,
        request_key=lambda row: row.id,
        assert_reason=assert_reason,
    )


@pytest.mark.parametrize("issued", [False, True])
def test_gone_target_ends_without_retaining_queue_ownership(service, issued):
    """A revoked target never waits for its lease or a poll that cannot arrive."""

    jobs, sessions, clock = service
    owner = parent(sessions, clock)
    operation = jobs.enqueue(owner.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    if issued:
        assert claim_agent(jobs, NODE_A, "serial-a") is not None
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        node.state = NodeIdentityState.RETIRED
        node.revoked_at = clock.now

    def end(_):
        assert jobs.reconcile_orders()
        with sessions() as session:
            return session.get(AgentOperation, operation.id)

    def fresh(_):
        new = jobs.enqueue(
            parent(sessions, clock).id,
            NODE_B,
            "recipe.stop",
            COMMIT,
            recipe_stop_payload(NODE_B, plan_digest=COMMIT),
        )
        receipt = claim_agent(jobs, NODE_B, "serial-b")
        assert receipt is not None
        assert fenced_operation(sessions, receipt).id == new.id
        with sessions() as session:
            return session.get(AgentOperation, new.id)

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        operation,
        end=end,
        fresh=fresh,
        request_key=lambda row: row.id,
    )


def test_retiring_bookkeeping_preserves_a_working_route(service):
    """An unobserved retired order is never evidence to withdraw a serving run."""

    _jobs, sessions, clock = service
    owner = parent(sessions, clock)
    run_id = str(uuid.uuid4())
    with sessions.begin() as session:
        run = RecipeRun(
            id=run_id,
            installation_id="installation",
            mapping_id="mapping",
            mapping_generation=1,
            alias="working",
            plan_digest=COMMIT,
            plan={},
            state="running",
            route_state="published",
            actor="operator",
            created_at=clock.now,
            updated_at=clock.now,
        )
        session.add(run)
        stored_owner = session.get(Job, owner.id)
        stored_owner.payload = {"owner_kind": "run", "owner_id": run_id}
        _release_retired_owner_in_session(session, stored_owner, clock.now)
        assert run.state == "running"
        assert run.route_state == "published"
        assert run.route_error is None


def test_non_blocking_guard_still_rejects_a_live_unknown_claim(service):
    """A reported-unknown observation is distinct from a relinquished lease."""

    jobs, sessions, clock = service
    owner = parent(sessions, clock)
    operation = jobs.enqueue(owner.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    with sessions.begin() as session:
        row = session.get(AgentOperation, operation.id)
        row.state = "cancelled"
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        attempt.state = "observing"
        attempt.observation_cause = "reported-unknown"
    with (
        sessions() as session,
        pytest.raises(AssertionError),
    ):
        assert_no_orphaned_holds(session)


def test_stored_parent_identity_normalizes_full_state_and_rejects_damage(service):
    """Catch hashing full Controller state instead of its canonical typed identity."""

    _jobs, sessions, clock = service
    job = parent(sessions, clock)
    with sessions.begin() as session:
        stored = session.get(Job, job.id)
        assert stored is not None
        stored.kind = "recipe.start"
        stored.payload = {
            "schema_version": 1,
            "owner_kind": "run",
            "owner_id": str(uuid.uuid4()),
            "plan_digest": "b" * 64,
            "start_deadline": "2026-08-03T01:00:00+00:00",
            "phases": None,
        }
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None
        accepted = canonical_message(stored.payload)
        typed = column_value(stored, "payload")
        assert isinstance(typed, RecipeStartParent)
        assert canonical_message(typed) != accepted
        assert column_message(stored, "payload") == canonical_message(typed)

    with sessions.begin() as session:
        session.connection().execute(
            update(Job).where(Job.id == job.id).values(payload={"damaged": True})
        )
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None
        assert isinstance(column_value(stored, "payload"), Residue)
        assert column_message(stored, "payload") == b""


@pytest.mark.parametrize("progresses", [True, False])
def test_distribution_recovery_budget_is_a_stall_window(
    service, progresses: bool
) -> None:
    """A slow transfer that keeps copying bytes is never ended by the budget.

    Interrupted attempts that add bytes move the recovery deadline; ones that
    add none leave it, and the order keeps retrying (visible, not failed).
    """

    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, kind, COMMIT, distribution_payload()
    )
    deadlines = []
    for attempt_number in range(1, 5):
        claim = _claim_until_due(jobs, sessions, clock, operation.id, rounds=12)
        assert claim is not None
        if progresses or attempt_number == 1:
            jobs.heartbeat(
                claim,
                {"phase": "copying", "completed_bytes": 100 * attempt_number},
                30,
            )
        clock.advance(seconds=20 * 60 if progresses else 45)
        assert jobs.record_late_result(_restart_interrupted_result(claim, kind))
        with sessions() as session:
            stored = session.get(AgentOperation, operation.id)
            assert stored.state != "failed"
            deadlines.append(stored.recovery_deadline)
    assert all(deadline is not None for deadline in deadlines)
    if progresses:
        assert deadlines == sorted(set(deadlines))
    else:
        assert len(set(deadlines)) == 1


@pytest.mark.parametrize("report", ["succeeded", "failed"])
def test_a_parent_that_ended_keeps_its_outcome_when_its_orders_are_reconciled(
    service, report
) -> None:
    # The parent was ended by its owner (superseded); a later aggregation of its
    # orders (every order change and reconcile pass runs it) ends the orders'
    # view, never the parent a second time.
    jobs, sessions, clock = service
    owner = parent(sessions, clock)
    jobs.enqueue(owner.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    if report == "succeeded":
        jobs.succeed(claim, STOP_RESULT)
    else:
        jobs.fail(claim, "engine exited")
    with sessions.begin() as session:
        ended = session.get(Job, owner.id)
        assert ended is not None
        ended.state = LifecycleState.CANCELLED.value
        ended.status_reason = SupersedeCode.SUPERSEDED_BY_INTENT.value
    clock.advance(seconds=5)
    with sessions.begin() as session:
        jobs._aggregate_parent(session, owner.id)
    stored = job_state(sessions, owner.id)
    assert stored.state == LifecycleState.CANCELLED
    assert stored.status_reason == SupersedeCode.SUPERSEDED_BY_INTENT


def test_an_order_that_ended_keeps_its_outcome_unless_its_report_is_replaced(
    service,
) -> None:
    # A later owner decision (a cancel, a deadline) about an order that already
    # ended is evidence; only the result consumer, in the queue's transaction,
    # replaces the end the queue just recorded from the same report.
    jobs, sessions, clock = service
    owner = parent(sessions, clock)
    order = jobs.enqueue(owner.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.succeed(claim, STOP_RESULT)
    clock.advance(seconds=5)
    with sessions.begin() as session:
        stored = session.get(AgentOperation, order.id)
        assert stored is not None
        reason = stored.status_reason
        AgentOperationAdapter(session).record_outcome(
            stored, None, None, Outcome.CANCELLED, clock.now
        )
        assert (stored.state, stored.status_reason) == ("succeeded", reason)
        AgentOperationAdapter(session).record_outcome(
            stored, None, None, Outcome.FAILED, clock.now, replaces_report=True
        )
        assert stored.state == "failed"


def test_gpu_fault_between_admission_and_claim_waits_then_recovers(service) -> None:
    # Catches dispatching a previously accepted launch after the GPU broke;
    # cleanup must remain claimable and a healthy observation must unblock it.

    jobs, sessions, clock = service
    repo = InventoryRepository(sessions)
    evidence = NodeInventoryReport(
        schema_version=1,
        node_id=NODE_A,
        observed_at=clock.now,
        disk_total_bytes=1000,
        disk_free_bytes=900,
        host_memory_total_bytes=1000,
        host_memory_free_bytes=900,
        gpu_memory_total_bytes=1000,
        gpu_memory_free_bytes=900,
        gpu_count=1,
        memory_pool="shared",
        capabilities=[],
        artifact_store_read_only=False,
        nvidia_driver_version="unknown",
        container_runtime_version="unknown",
        gpu_health=GpuHealth.NEEDS_RESET,
    )
    repo.record(evidence)
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.RECIPE_START,
        COMMIT,
        superseded_order_payload(clock),
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    with sessions() as session:
        waiting = session.get(AgentOperation, operation.id)
        assert waiting is not None
        assert waiting.current_attempt == 0
        assert ProjectionCode.NODE_GPU_REQUIRES_RESET in waiting.status_reason
        assert waiting.next_action_at is not None
        deadline = waiting.recovery_deadline

    with sessions.begin() as session:
        waiting = session.get(AgentOperation, operation.id)
        assert waiting is not None
        record_queue_waits(session, [waiting], clock.now)
        assert waiting.wait_evidence is not None
        assert waiting.wait_evidence.reason == ProjectionCode.NODE_GPU_REQUIRES_RESET
        assert waiting.wait_evidence.blockers[0].node_ids == [NODE_A]
    cleanup = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.RECIPE_STOP,
        COMMIT,
        STOP_PAYLOAD,
    )
    stop = claim_agent(jobs, NODE_A, "serial-a")
    assert stop is not None
    assert fenced_operation(sessions, stop).id == cleanup.id
    jobs.succeed(stop, STOP_RESULT)
    clock.advance(seconds=5)
    repo.record(
        evidence.model_copy(
            update={"observed_at": clock.now, "gpu_health": GpuHealth.HEALTHY}
        )
    )
    clock.advance(seconds=1)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    assert fenced_operation(sessions, claim).id == operation.id
    with sessions() as session:
        current = session.get(AgentOperation, operation.id)
        assert current is not None
        assert current.recovery_deadline == deadline


def test_ended_hold_clears_the_stamped_deadline_of_install_build_and_job_not_start() -> (
    None
):
    # Catches an install or build that keeps what remained of a dispatch hold.

    stamped = datetime(2026, 1, 1, tzinfo=UTC)
    for kind, cleared in (
        (ProtocolAgentOperation.RECIPE_INSTALL, True),
        (ProtocolAgentOperation.RECIPE_BUILD, True),
        (ProtocolAgentOperation.RECIPE_JOB_RUN, True),
        (ProtocolAgentOperation.RECIPE_START, False),
    ):
        operation = AgentOperation(
            kind=kind.value, current_attempt=0, recovery_deadline=stamped
        )
        AgentOperationAdapter.clear_hold_deadline(operation)
        assert (operation.recovery_deadline is None) is cleared
    later = AgentOperation(
        kind=ProtocolAgentOperation.RECIPE_INSTALL.value,
        current_attempt=1,
        recovery_deadline=stamped,
    )
    AgentOperationAdapter.clear_hold_deadline(later)
    assert later.recovery_deadline == stamped


def test_reusable_preflight_survives_operation_history_collection(service) -> None:
    """Catches treating a historical command receipt as current capability storage."""

    queue, sessions, clock = service
    request = preflight_request()
    digest = request_digest(request)
    job = parent(sessions, clock)
    operation = queue.enqueue(
        job.id,
        NODE_A,
        ProtocolAgentOperation.RUNTIME_PREFLIGHT,
        COMMIT,
        request.model_dump(mode="json"),
    )
    claim = claim_agent(queue, NODE_A, "serial-a", preflight_fingerprint="a" * 64)
    assert claim is not None
    result = passing_result(request, observed_at=int(clock.now.timestamp()))
    queue.succeed(claim.fence, result)
    with sessions.begin() as session:
        session.execute(
            delete(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        session.execute(delete(AgentOperation).where(AgentOperation.id == operation.id))
    with sessions() as session:
        retained = latest_result(session, NODE_A, requirements_sha256=digest)
        assert retained is not None
        assert retained.result == result
        assert not admission_blockers(request, retained, current_fingerprint="a" * 64)
        assert admission_blockers(request, retained, current_fingerprint="b" * 64)


def test_failed_child_end_wake_preserves_committed_receipt(service):
    """A failed owner wake cannot turn an already committed report into loss."""

    queue, sessions, clock = service
    request = preflight_request()
    job = parent(sessions, clock)
    queue.enqueue(
        job.id,
        NODE_A,
        ProtocolAgentOperation.RUNTIME_PREFLIGHT,
        COMMIT,
        request.model_dump(mode="json"),
    )

    def lost_wake() -> datetime:
        raise OSError("injected completion wake loss")

    ChildEndWake.install(sessions, lost_wake)
    claim = claim_agent(queue, NODE_A, "serial-a", preflight_fingerprint="a" * 64)
    assert claim is not None
    queue.succeed(
        claim.fence, passing_result(request, observed_at=int(clock.now.timestamp()))
    )
    with sessions() as session:
        receipt = latest_result(
            session, NODE_A, requirements_sha256=request_digest(request)
        )
        assert receipt is not None
        assert receipt.received_at == clock.now
        committed = session.get(Job, job.id)
        assert committed is not None
        assert committed.state == LifecycleState.SUCCEEDED


@pytest.mark.parametrize("contact_path", ["claim", "heartbeat"])
def test_controller_clock_rollback_replaces_pinned_contact(
    service, contact_path
) -> None:
    """Catches max(stored, now) keeping a future contact after clock correction."""
    jobs, sessions, clock = service
    claim = None
    if contact_path == "heartbeat":
        jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            ProtocolAgentOperation.RECIPE_STOP,
            COMMIT,
            STOP_PAYLOAD,
        )
        claim = claim_agent(jobs, NODE_A, "serial-a")
        assert claim is not None
    # The clock steps back inside the certificate validity interval.
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        contact_row(node).last_seen_at = clock() + timedelta(hours=1)
    if claim is None:
        claim_agent(jobs, NODE_A, "serial-a")
    else:
        jobs.heartbeat(claim, None, 30)
    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        seen = contact_row(node).last_seen_at
        assert seen is not None
        assert seen.replace(tzinfo=UTC) == clock()
