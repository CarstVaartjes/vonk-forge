"""Agent jobs: distribution recovery claims."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from vonk_agent_protocol.agent_operations import (
    AgentOperation as ProtocolAgentOperation,
)
from vonk_agent_protocol.agent_operations import ArtifactDistributionResult
from vonk_agent_protocol.contracts import AgentProgress, AgentResult, canonical_payload
from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control import agent_operation_states as aos
from vonk_control.agent_jobs.predicates import _claim_predicate
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.fleet_projection.nas_transfer_rate import nas_rates
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt

from .agent_fences import fenced_attempt, fenced_operation
from .agent_jobs_support import (
    COMMIT,
    NODE_A,
    STOP_PAYLOAD,
    STOP_RESULT,
    _enqueue_successor_mutation,
    distribution_payload,
    job_state,
    parent,
    superseded_order_payload,
)
from .runtime_identity_support import (
    claim_agent,
)


def test_transient_distribution_failure_recovers_after_repeated_faults_and_restart(
    service,
) -> None:

    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        kind,
        COMMIT,
        distribution_payload(),
    )
    for attempt_number in range(1, 5):
        claim = claim_agent(
            jobs,
            NODE_A,
            "serial-a",
        )
        assert claim is not None, attempt_number
        assert fenced_attempt(sessions, claim).attempt == attempt_number, attempt_number
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
    assert recovered is not None
    assert fenced_operation(sessions, recovered).id == operation.id
    jobs.succeed(recovered, ArtifactDistributionResult(downloaded_bytes=0))
    with sessions() as session:
        assert session.get(AgentOperation, operation.id).state == "succeeded"


def test_a_refused_distribution_stores_the_agents_denial_evidence(service) -> None:
    # The agent's real body for a Controller-refused distribution (the shared
    # vector its Rust suite produces): ingress keeps the diagnostic and stage.

    jobs, sessions, clock = service
    vector = json.loads(
        (
            Path(__file__).parents[2]
            / "agent_protocol/src/vonk_agent_protocol/vectors/controller-denial-results-v1.json"
        ).read_text()
    )
    case = next(
        case
        for case in vector["cases"]
        if case["name"] == "distribution-assignment-refused"
    )
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value,
        COMMIT,
        distribution_payload(),
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult.model_validate_json(
            json.dumps(
                {
                    "fence": claim.model_dump(mode="json")["fence"],
                    "state": case["state"],
                    "result": case["result"],
                }
            )
        )
    )
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        assert attempt is not None
        assert attempt.result is not None
        evidence = case["result"]["evidence"]
        assert attempt.result["diagnostic"] == evidence["diagnostic"]
        assert attempt.result["stage"] == evidence["stage"]


def test_successful_distribution_receipt_preserves_observed_order_wide_bytes(
    service,
) -> None:
    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        kind,
        COMMIT,
        distribution_payload(),
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
        assert attempt.progress["completed_bytes"] == 100
        assert attempt.progress["completed_items"] == 2
        assert attempt.progress["phase"] == "completed"


def test_the_nas_rate_survives_the_distribution_receipt(service) -> None:
    # The receipt rebuilds the final progress; what the agent measured of its
    # network fetch must still be there for the rate to read.
    jobs, sessions, clock = service
    jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value,
        COMMIT,
        distribution_payload(),
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.heartbeat(
        claim,
        {
            "phase": "copying",
            "completed_bytes": 40 * 10**9,
            "total_bytes": 40 * 10**9,
            "total_bytes_known": True,
            "fetched": {"bytes": 20 * 10**9, "seconds": 40.0},
        },
        60,
    )
    jobs.succeed(claim, {"downloaded_bytes": 40 * 10**9})

    with sessions() as session:
        rates = nas_rates(session, [NODE_A], clock() - timedelta(days=1))
    assert rates[NODE_A].bytes_per_second == 5 * 10**8


@pytest.mark.parametrize("explicit_null", [False, True])
def test_queue_stores_and_claims_the_canonical_payload_hash(
    service, explicit_null: bool
) -> None:

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
        assert stored is not None
        assert old_parent is not None
        stored.state = "needs-operator"
        stored.current_attempt = 1
        stored.status_reason = "operation outcome uncertain"
        session.add(
            AgentOperationAttempt(
                operation_id=stored.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=clock.now + timedelta(minutes=1),
                agent_certificate_serial="serial-a",
                state="observing",
                observation_cause="reported-unknown",
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
    old = jobs.enqueue(
        old_parent.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
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
    assert job_state(sessions, old_parent.id).state == "cancelled"


def test_disarmed_cancellation_records_the_defect(service) -> None:
    """A flag without a usable timestamp disarms cleanup; say so loudly."""

    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    old = jobs.enqueue(
        old_parent.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
    _supersede_waiting_mutation(
        sessions, clock, old_parent, old, cancel_requested_at=None
    )
    _enqueue_successor_mutation(jobs, sessions, clock)

    assert claim_agent(jobs, NODE_A, "serial-a") is not None


def test_live_cancellation_still_blocks_later_work(service) -> None:
    """A cancellation inside its cleanup window still fences later mutations."""

    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    old = jobs.enqueue(
        old_parent.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
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
        assert blocked is not None
        assert blocked.state == "queued"
        assert waiting is not None
        assert waiting.state in aos.PARKED


def test_live_prior_mutation_still_blocks_later_work(service) -> None:
    jobs, sessions, clock = service
    old_parent = parent(sessions, clock)
    old = jobs.enqueue(old_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None
    assert fenced_operation(sessions, first).id == old.id
    clock.advance(seconds=1)
    new_parent = parent(sessions, clock)
    new = jobs.enqueue(new_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        blocked = session.get(AgentOperation, new.id)
        running = session.get(AgentOperation, old.id)
        assert blocked is not None
        assert blocked.state == "queued"
        assert running is not None
        assert running.state == "running"


@pytest.mark.parametrize("dead_attempt", ["missing", "stopped"])
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
    assert first is not None
    assert fenced_operation(sessions, first).id == old.id

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
            attempt.state, attempt.observation_cause = "observing", "lease-lapsed"
    clock.advance(seconds=1)
    new_parent = parent(sessions, clock)
    new = jobs.enqueue(new_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)

    # The first claim reconciles the dead order and refuses itself so the park
    # commits; the next claim must then admit the successor.
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    claimed = claim_agent(jobs, NODE_A, "serial-a")

    assert claimed is not None
    assert fenced_operation(sessions, claimed).id == new.id
    with sessions() as session:
        parked = session.get(AgentOperation, old.id)
        successor = session.get(AgentOperation, new.id)
        assert parked is not None
        assert parked.state in aos.PARKED
        assert parked.status_reason is not None
        assert successor is not None
        assert successor.state == "running"


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
        attempt.state, attempt.observation_cause = "observing", "lease-lapsed"
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
        assert stored is not None
        assert stored.status_reason is not None


def test_excluded_work_refusal_names_both_ordinals(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(
        parent_job.id, NODE_A, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
    with sessions.begin() as session:
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 2

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        assert stored.status_reason is not None


def test_excluded_work_refusal_records_a_cancelled_parent(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).result = {"cancel_requested": True}

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        assert stored.status_reason is not None


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
        assert stored is not None
        assert stored.status_reason is not None


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
        assert stored is not None
    assert claim_agent(jobs, NODE_A, "serial-a") is not None


@pytest.mark.parametrize("malformed", [1, "true"])
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
    assert claim is not None
    assert fenced_operation(sessions, claim).id == operation.id

    with sessions() as session:
        job = session.get(Job, parent_job.id)
        assert job is not None
        assert job.status_reason is not None


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
        assert stored is not None
        assert stored.status_reason is None


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
    # Only an order that is not cleanup is superseded (a Stop keeps its intent).
    _set_operation(sessions, operation, kind="recipe.start")


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
    _set_operation(
        sessions,
        operation,
        state="needs-operator",
        current_attempt=1,
        next_action_at=None,
        wait_evidence=None,
    )


def _scenario_upgrade_safety_not_elapsed(
    sessions, clock, parent_job, operation
) -> None:
    _set_operation(
        sessions,
        operation,
        kind=ProtocolAgentOperation.AGENT_UPGRADE.value,
        state="backoff",
        current_attempt=1,
        next_action_at=clock.now,
    )
    _add_attempt(sessions, operation, clock, state="running", lease_seconds=60)


def _scenario_operator_retry_not_due(sessions, clock, parent_job, operation) -> None:
    _set_operation(
        sessions,
        operation,
        state="backoff",
        current_attempt=1,
        next_action_at=clock.now + timedelta(seconds=60),
    )


def _scenario_operator_retry_attempt_not_ready(
    sessions, clock, parent_job, operation
) -> None:
    _set_operation(
        sessions,
        operation,
        state="backoff",
        current_attempt=1,
        next_action_at=clock.now,
    )


def _scenario_queued_observation_not_due(
    sessions, clock, parent_job, operation
) -> None:
    _set_operation(
        sessions,
        operation,
        recovery_deadline=clock.now + timedelta(hours=1),
        next_action_at=clock.now + timedelta(seconds=60),
    )


#: One scenario per condition the predicate can fail.  Each leaves exactly one
#: named condition false, so the classifier must name that condition.
_REFUSAL_SCENARIOS: dict[str, Callable[..., None]] = {
    WaitReason.OBSERVATION_UNAVAILABLE: _scenario_queued_observation_not_due,
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


#: Conditions that are never a refusal: the order is the node's own, executing
#: or awaiting its result, so an idle poll names nothing (a wait, not a refusal).
_NEVER_REFUSED = frozenset({"running-lease-live"})


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

    if check in _NEVER_REFUSED:
        assert excluded is None
        return
    assert excluded is not None
    _, refusal, _ = excluded
    assert refusal == check
    assert refusal != "unclassified-unclaimable"
