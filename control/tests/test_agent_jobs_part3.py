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
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentProgress,
    AgentResult,
    AgentResultState,
    FailureCode,
    LifecycleState,
    OutcomeKind,
    SupersedeCode,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol.contracts import ArtifactDistributionResult, canonical_payload
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control import agent_operation_states as aos
from vonk_control.agent_jobs import (
    AgentJobService,
    _claim_predicate,
)
from vonk_control.lifecycle import Outcome
from vonk_control.lifecycle.agent_operation import (
    AgentOperationAdapter,
    retry_scheduled,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RecipeRun,
)

from . import agent_jobs_support as _support
from .agent_fences import fenced_attempt, fenced_operation
from .agent_jobs_support import (
    _RESTART_INTERRUPTED,
    COMMIT,
    NODE_A,
    NODE_B,
    STOP_PAYLOAD,
    STOP_RESULT,
    _claim_until_due,
    _restart_interrupted_result,
    canonical_start_payload,
    distribution_payload,
    job_state,
    parent,
    superseded_order_payload,
)
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import (
    claim_agent,
)

service = _support.service


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
        parent(sessions, clock).id, NODE_A, kind, COMMIT, distribution_payload()
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
        attempt.state, attempt.observation_cause = "observing", "lease-lapsed"
        attempt.result = {**_RESTART_INTERRUPTED, "operation": kind}
        stored.state = "needs-operator"
        stored.next_action_at = None
        stored.status_reason = "attempt 2 lease expired; the effect is unobserved"
        session.get(Job, stored.parent_job_id).state = "needs-operator"

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
        distribution_payload(),
    )
    for attempt_number in range(1, 5):
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
    jobs.succeed(recovered, ArtifactDistributionResult(downloaded_bytes=0))
    with sessions() as session:
        assert session.get(AgentOperation, operation.id).state == "succeeded"


def test_a_refused_distribution_stores_the_agents_denial_evidence(service) -> None:
    # The agent's real body for a Controller-refused distribution (the shared
    # vector its Rust suite produces): ingress keeps the diagnostic and stage.
    from vonk_agent_protocol import AgentResult

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
        assert attempt is not None and attempt.result is not None
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
    from vonk_agent_protocol import canonical_message

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
        distribution_payload(),
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
            attempt.state, attempt.observation_cause = "observing", "lease-lapsed"
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
        assert stored is not None and stored.status_reason is not None


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
        assert stored is not None and stored.status_reason is not None


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
    from vonk_agent_protocol import (
        AgentFailureResult,
        LifecycleState,
        ObservationCause,
        OutcomeFailed,
    )

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
        assert row.current_attempt == 1 and row.payload == original_payload
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
    assert fresh is not None and fenced_operation(sessions, fresh).id == successor.id
    jobs.succeed(fresh, STOP_RESULT)
    assert (
        job_state(sessions, successor.parent_job_id).state == LifecycleState.SUCCEEDED
    )


def test_cancelled_build_waits_for_platform_cleanup_as_an_observation(service) -> None:
    """A rebuildable child cannot turn pending cleanup into a human gate."""
    from vonk_control.jobs.documents import RecipeBuildParent
    from vonk_control.recipe_lifecycle_contract import RecipeOperationCancellationResult

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
    from types import SimpleNamespace

    from .non_blocking import assert_ended_without_blocking

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
    from types import SimpleNamespace

    from .non_blocking import assert_ended_without_blocking

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
    from vonk_control.agent_jobs import _release_retired_owner_in_session

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
        _release_retired_owner_in_session(
            session, stored_owner, "unobserved", clock.now
        )
        assert run.state == "running"
        assert run.route_state == "published"
        assert run.route_error is None


def test_non_blocking_guard_still_rejects_a_live_unknown_claim(service):
    """A reported-unknown observation is distinct from a relinquished lease."""
    from .non_blocking import assert_no_orphaned_holds

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
    from vonk_control.agent_jobs.stored import column_message, column_value
    from vonk_control.jobs.documents import RecipeStartParent

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
    from sqlalchemy import update
    from vonk_control.lifecycle.evidence import Residue

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
