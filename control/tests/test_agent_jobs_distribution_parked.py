"""Agent jobs: distribution parked."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from vonk_agent_protocol.contracts import AgentFailureKind, AgentResult
from vonk_agent_protocol.contracts import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol.lifecycle_vocabulary import (
    AgentResultState,
    FailureCode,
    LifecycleState,
)
from vonk_control import agent_operation_states as aos
from vonk_control.agent_jobs.contracts import StaleAgentAttempt
from vonk_control.distribution.service import DistributionService
from vonk_control.distribution.sources import MemoryObjectSource
from vonk_control.distribution_assignment import NodeDistributionAssignment
from vonk_control.lifecycle.agent_operation import (
    retry_scheduled,
)
from vonk_control.models.fleet import AgentCertificate
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt

from .agent_fences import fenced_attempt, fenced_operation
from .agent_jobs_support import (
    _RESTART_INTERRUPTED,
    COMMIT,
    NODE_A,
    NODE_B,
    STOP_PAYLOAD,
    STOP_RESULT,
    _assert_parent_ending_admit_fresh_request,
    _claim_until_due,
    _restart_interrupted_result,
    canonical_start_payload,
    distribution_payload,
    job_state,
    parent,
)
from .runtime_identity_support import (
    claim_agent,
)


def test_a_foreign_container_refusal_waits_visibly_and_the_start_resumes_when_it_is_gone(
    service,
) -> None:
    """The typed refusal is a prerequisite wait, not an invalid contract.

    Catches the refusal read as ``invalid-contract``: the start order ended
    failed and the load with it, although the container may be removed a moment
    later.  The order stays re-issuable, names the container in its reason, and
    its parent stays open so the same intent starts once the name is free.
    """

    from vonk_agent_protocol.contracts import AgentResult

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
        stored.state = "backoff"
        stored.next_action_at = clock.now
        parent_job = session.get(Job, stored.parent_job_id)
        parent_job.result = {"cancel_requested": True}

    assert claim_agent(jobs, NODE_A, "serial-a") is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == "failed"
        assert stored.next_action_at is None
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        assert attempt is not None and attempt.state == "failed"
    assert job_state(sessions, operation.parent_job_id).state == "failed"


def test_start_failure_after_the_deadline_is_not_retried(service) -> None:
    from vonk_agent_protocol.contracts import AgentResult

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
    if body.get("failure_kind") == AgentFailureKind.INVALID_AUTHORITY:
        with sessions() as session:
            stored = session.get(AgentOperation, operation.id)
            assert stored is not None and stored.state == LifecycleState.FAILED
            assert stored.next_action_at is None
        _assert_parent_ending_admit_fresh_request(
            jobs, sessions, clock, job_state(sessions, operation.parent_job_id)
        )
        return
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state in aos.PARKED
        assert stored.next_action_at is not None  # the retry is scheduled
        assert stored.status_reason is not None
    assert job_state(sessions, operation.parent_job_id).state == "queued"
    clock.advance(seconds=120)
    retried = claim_agent(jobs, NODE_A, "serial-a")
    assert retried is not None
    assert fenced_operation(sessions, retried).id == operation.id
    assert fenced_attempt(sessions, retried).attempt == 2

    jobs.succeed(retried, STOP_RESULT)
    _assert_parent_ending_admit_fresh_request(
        jobs, sessions, clock, job_state(sessions, operation.parent_job_id)
    )


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


def test_distribution_retry_records_reobserved_progress_without_a_high_water_clamp(
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
        stored.state = "backoff"
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
        assert current.progress["completed_bytes"] == 50
        assert current.progress["completed_items"] == 0
        assert current.progress["phase"] == "verifying"


def test_exact_late_stop_success_settles_without_another_attempt(
    service,
) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.RECIPE_STOP,
        COMMIT,
        STOP_PAYLOAD,
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=31)
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    from vonk_agent_protocol.contracts import AgentResult

    late = AgentResult.model_validate(
        {
            **{key: claim.model_dump(mode="json")[key] for key in ("fence",)},
            "state": AgentResultState.SUCCEEDED,
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
        assert stored is not None and stored.state == LifecycleState.SUCCEEDED
        assert attempt is not None and attempt.state == LifecycleState.SUCCEEDED
        assert stored.current_attempt == 1
        assert attempt.result == STOP_RESULT.model_dump(mode="json")
    assert jobs.record_late_result(late) is False


def test_late_result_under_expired_fence_does_not_park_exact_resume_forever(
    service,
) -> None:
    """A result that arrives after the lease is evidence, not an operator gate.

    Live regression: a two-phase start's readiness failure reached the
    Controller an hour after its lease; the retained late result disqualified
    the parked order from exact re-issue, so the job, its run and every
    observation of that run waited for an operator indefinitely.
    """

    from vonk_agent_protocol.contracts import AgentResult

    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.RECIPE_STOP,
        COMMIT,
        STOP_PAYLOAD,
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=31)
    late = AgentResult.model_validate(
        {
            **{key: claim.model_dump(mode="json")[key] for key in ("fence",)},
            "state": AgentResultState.FAILED,
            "result": {
                "status": LifecycleState.FAILED,
                "error_code": FailureCode.RECIPE_STOP_FAILED,
                "failure_kind": AgentFailureKind.UNCERTAIN_EFFECT,
                "uncertain": True,
                "reason": "exact runtime stop could not be observed",
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
    assert (
        job_state(sessions, operation.parent_job_id).state == LifecycleState.SUCCEEDED
    )


def _deliver_result(jobs, result) -> None:
    """Deliver ``result`` as the agent API does: the live fence, else late."""
    try:
        jobs.record_result(result)
    except StaleAgentAttempt:
        assert jobs.record_late_result(result)


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
        parent(sessions, clock).id, NODE_A, kind, COMMIT, distribution_payload()
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
        parent(sessions, clock).id, NODE_A, kind, COMMIT, distribution_payload()
    )
    delays: list[float] = []
    for attempt_number in range(1, 5):
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
        # A progressing attempt still holds its lease when the agent is back.
        _deliver_result(jobs, _restart_interrupted_result(claim, kind))
        with sessions() as session:
            stored = session.get(AgentOperation, operation.id)
            delays.append(
                (stored.next_action_at.replace(tzinfo=UTC) - clock.now).total_seconds()
            )

    if progresses:
        assert max(delays) <= 90
    else:
        assert delays[3] >= 30
        assert max(delays) <= 600 * 5 // 4

    final = _claim_until_due(jobs, sessions, clock, operation.id, rounds=12)
    assert final is not None
    jobs.succeed(final, {"downloaded_bytes": 700 if progresses else 100})
    _assert_parent_ending_admit_fresh_request(
        jobs, sessions, clock, job_state(sessions, operation.parent_job_id)
    )


def _single_spark_start_payload(clock) -> dict[str, object]:
    """A start that binds no launch budget: its 30 s lease is its only clock."""

    payload = canonical_start_payload(start_deadline=clock.now)
    del payload["phase"], payload["start_deadline"]
    return payload


@pytest.mark.parametrize(
    "kind",
    [
        ProtocolAgentOperation.RECIPE_START.value,
        ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value,
    ],
)
def test_attempts_that_lapse_without_progress_slow_down_and_say_why(
    service, kind: str
) -> None:
    """Live regression (2026-10-16/17): a lapsing start was reissued 45-47 times.

    Each attempt of five single-Spark starts lapsed silently, and only the
    transfer kind slowed down: the start was reissued every 45-75 s until its
    one-hour recovery budget ended it. Every reissued kind now counts its
    consecutive lapses, slows down after three and names the loop.
    """

    jobs, sessions, clock = service
    payload = (
        _single_spark_start_payload(clock)
        if kind == ProtocolAgentOperation.RECIPE_START.value
        else distribution_payload()
    )
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    delays: list[float] = []
    for attempt_number in range(1, 6):
        claim = _claim_until_due(jobs, sessions, clock, operation.id, rounds=12)
        assert claim is not None
        assert fenced_attempt(sessions, claim).attempt == attempt_number
        # The attempt never renews and never reports; the Spark's next poll
        # finds its lease lapsed.
        clock.advance(seconds=45)
        assert claim_agent(jobs, NODE_A, "serial-a") is None
        with sessions() as session:
            stored = session.get(AgentOperation, operation.id)
            assert stored.state == aos.BACKOFF
            delays.append(
                (stored.next_action_at.replace(tzinfo=UTC) - clock.now).total_seconds()
            )
            reason = stored.status_reason or ""

    assert max(delays[:2]) <= 5
    assert delays[3] >= 30 and delays[4] >= 60
    assert "interrupted 5 times in a row" in reason


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


def test_a_retried_distribution_is_issued_a_fresh_grant(service) -> None:
    """Live regression: the retry reused the first attempt's expired grant.

    The grant lapses an hour after registration; a transfer parked or backing
    off for longer was refused ``distribution.expired`` and failed terminally
    although nothing but time had changed.
    """

    jobs, sessions, clock = service
    distribution = _registered_distribution_grant(sessions, clock)
    clock.advance(seconds=59 * 60)
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, kind, COMMIT, distribution_payload()
    )
    with sessions.begin() as session:
        session.get(AgentCertificate, "serial-a").not_after = clock.now + timedelta(
            hours=12
        )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=45)
    assert jobs.record_late_result(_restart_interrupted_result(claim, kind))
    # The first claim renewed the grant. Expire that retained grant while the
    # accepted recovery budget remains open, as a shorter ingress lease can.
    with sessions.begin() as session:
        from vonk_control.models.operations import ArtifactDistributionAssignment

        assignment = session.scalar(select(ArtifactDistributionAssignment))
        assert assignment is not None
        assignment.expires_at = clock.now + timedelta(seconds=60)
    clock.advance(seconds=2 * 60)
    with pytest.raises(Exception):  # noqa: B017 -- effects and fresh admission establish rejection
        distribution.authorize(node_id=NODE_A, plan_digest=COMMIT)

    retry = _claim_until_due(jobs, sessions, clock, operation.id)
    assert retry is not None and fenced_attempt(sessions, retry).attempt == 2
    fresh = DistributionService(distribution.source, clock=clock, sessions=sessions)
    grant = fresh.authorize(node_id=NODE_A, plan_digest=COMMIT)
    assert grant.expires_at > clock.now + timedelta(minutes=30)

    jobs.succeed(retry, {"downloaded_bytes": 7})
    _assert_parent_ending_admit_fresh_request(
        jobs, sessions, clock, job_state(sessions, operation.parent_job_id)
    )


@pytest.mark.parametrize("sibling_fails_first", [False, True])
def test_a_failed_job_does_not_leave_a_sibling_parked_behind_an_unclaimable_retry(
    service, sibling_fails_first: bool
) -> None:
    """Live regression: one node's terminal failure stranded the other's retry.

    The job failed, so the sibling's scheduled retry could never be claimed
    (``parent-not-claimable``) and no operator action applied to it.
    """

    from vonk_agent_protocol.contracts import AgentResult

    jobs, sessions, clock = service
    kind = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    job = parent(sessions, clock)
    first = jobs.enqueue(job.id, NODE_A, kind, COMMIT, distribution_payload())
    second = jobs.enqueue(job.id, NODE_B, kind, COMMIT, distribution_payload())
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

    from .non_blocking import assert_ended_without_blocking

    def released() -> None:
        with sessions() as session:
            parked = session.get(AgentOperation, first.id)
            assert parked.state == "cancelled"
            assert parked.next_action_at is None
            assert session.get(AgentOperation, second.id).state == "failed"

    def fresh(_world: None) -> Job:
        new_job = parent(sessions, clock)
        operation = jobs.enqueue(
            new_job.id, NODE_A, kind, COMMIT, distribution_payload()
        )
        claim = claim_agent(jobs, NODE_A, "serial-a")
        assert claim is not None
        assert fenced_operation(sessions, claim.fence).id == operation.id
        return job_state(sessions, new_job.id)

    def typed_reason(_ended: Job) -> None:
        from vonk_agent_protocol.contracts import AgentFailureResult
        from vonk_agent_protocol.lifecycle_vocabulary import FailureCode

        with sessions() as session:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == second.id
                )
            )
            assert attempt is not None
            failure = AgentFailureResult.model_validate_json(json.dumps(attempt.result))
            assert failure.error_code == FailureCode.ARTIFACT_DISTRIBUTION_FAILED

    def keep_ended(view: Job) -> Job:
        return view

    ended, _ = assert_ended_without_blocking(
        None,
        job_state(sessions, job.id),
        end=keep_ended,
        assert_released=released,
        assert_reason=typed_reason,
        fresh=fresh,
    )
    assert ended.state == "failed"


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
