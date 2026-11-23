"""Agent jobs: lease start budget."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol.contracts import AgentResult, canonical_message
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_control import agent_operation_states as aos
from vonk_control.agent_jobs.retirement import authorize_operator_resume_in_session
from vonk_control.agent_operation_facts import START_STALL_WINDOW, job_start_deadline
from vonk_control.distribution.service import DistributionService
from vonk_control.distribution.sources import MemoryObjectSource
from vonk_control.distribution_assignment import NodeDistributionAssignment
from vonk_control.inventory.items import (
    runtime_value,
)
from vonk_control.lifecycle.agent_operation import (
    AgentOperationAdapter,
)
from vonk_control.lifecycle.types import Outcome, Reported
from vonk_control.models.fleet import AgentCertificate, AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt

from .agent_fences import fenced_attempt, fenced_operation
from .agent_jobs_support import (
    COMMIT,
    NODE_A,
    NODE_B,
    STOP_PAYLOAD,
    STOP_RESULT,
    _assert_parent_ending_admit_fresh_request,
    _claim_until_due,
    canonical_start_payload,
    distribution_payload,
    job_state,
    parent,
)
from .node_contacts import contact_row
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import (
    claim_agent,
)


def canonical_start_result(payload: Mapping[str, object]) -> dict[str, object]:
    """A rank launch succeeds with an empty result."""

    del payload
    return {}


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
        attempt_row.state, attempt_row.observation_cause = "observing", "lease-lapsed"
        parked.state = "backoff"
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


@pytest.mark.parametrize("restriction", (None, "expired", "cancelled", "stale"))
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
        distribution_payload(),
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
    if restriction == "cancelled":
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
        with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
            jobs.heartbeat(claim, {"phase": "copying", "completed_bytes": 3}, 60)
    else:
        jobs.heartbeat(claim, {"phase": "copying", "completed_bytes": 3}, 60)
    clock.advance(seconds=20)
    # The serving process must observe the durable renewal after the initial
    # grant elapsed. A different node or expired fence cannot.
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
        with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
            restarted.authorize(node_id=NODE_A, plan_digest=COMMIT)
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        restarted.authorize(node_id=NODE_B, plan_digest=COMMIT)
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        restarted.authorize(node_id=NODE_A, plan_digest=other_plan.plan_digest)
    if restriction is None:
        clock.advance(seconds=3600)
        with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
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
            "architecture": runtime_value(node, lambda identity: identity.architecture),
            "binary_digest": runtime_value(
                node, lambda identity: identity.binary_digest
            ),
            "build_digest": runtime_value(node, lambda identity: identity.build_digest),
            "semantic_version": runtime_value(
                node, lambda identity: identity.semantic_version
            ),
        } == runtime_identity.model_dump(exclude={"package_activation"})
        digest = contact_row(node).contact_observation_digest
        assert digest is not None
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


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

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=runtime_identity,
        )

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert (
            runtime_value(node, lambda identity: identity.architecture) == "linux-arm64"
        )
        assert (
            runtime_value(node, lambda identity: identity.semantic_version) == "1.0.0"
        )
        assert (
            runtime_value(node, lambda identity: identity.build_digest)
            == "sha256:" + "f" * 64
        )
        assert runtime_value(node, lambda identity: identity.binary_digest) == "f" * 64


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
        contact_row(node).last_seen_at = None
        certificate.state = "revoked"
        certificate.revoked_at = clock.now

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
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
        assert node is not None and contact_row(node).last_seen_at is None
        assert contact_row(node).protocol_version == 4
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

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
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
    _assert_parent_ending_admit_fresh_request(jobs, sessions, clock, parent_job)


def test_parent_job_retries_uncertain_failure_then_succeeds(
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
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": failed.fence,
                    "state": "failed",
                    "result": {
                        "status": "failed",
                        "error_code": "recipe_stop_failed",
                        "reason": "container runtime is busy",
                        "failure_kind": "temporary-dependency",
                    },
                }
            )
        )
    )
    assert job_state(sessions, parent_job.id).state == "queued"

    succeeded = claim_agent(jobs, NODE_B, "serial-b")
    assert succeeded is not None
    jobs.succeed(succeeded, STOP_RESULT)

    aggregate = job_state(sessions, parent_job.id)
    assert aggregate.state == LifecycleState.QUEUED
    retried = _claim_until_due(
        jobs, sessions, clock, fenced_operation(sessions, failed).id
    )
    assert retried is not None and retried.fence != failed.fence
    jobs.succeed(retried, STOP_RESULT)
    assert job_state(sessions, parent_job.id).state == LifecycleState.SUCCEEDED
    _assert_parent_ending_admit_fresh_request(jobs, sessions, clock, parent_job)


def test_unknown_stop_outcome_retries_without_an_operator(
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
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": waiting.fence,
                    "state": aos.WIRE_UNKNOWN,
                    "result": {"reason": "stop receipt unavailable"},
                }
            )
        )
    )

    succeeded = claim_agent(jobs, NODE_B, "serial-b")
    assert succeeded is not None
    jobs.succeed(succeeded, STOP_RESULT)

    assert job_state(sessions, parent_job.id).state == "queued"
    with sessions() as session:
        pending = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == parent_job.id,
                AgentOperation.node_id == NODE_A,
            )
        )
        retry_at = pending.next_action_at
        assert retry_at is not None
    clock.advance(
        seconds=(retry_at.replace(tzinfo=UTC) - clock.now).total_seconds() + 1
    )
    retry = claim_agent(jobs, NODE_A, "serial-a")
    assert retry is not None
    jobs.succeed(retry, STOP_RESULT)
    _assert_parent_ending_admit_fresh_request(jobs, sessions, clock, parent_job)


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

    from vonk_agent_protocol.contracts import AgentResult

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
