"""Request-owned recovery and the irreversible job effect boundary."""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from vonk_agent_protocol import FailureStage, OutcomeKind
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_jobs.retry import JOB_PRE_LAUNCH_STAGES
from vonk_control.lifecycle import State

from .agent_fences import fenced_attempt
from .runtime_identity_support import claim_agent
from .test_agent_jobs import (
    COMMIT,
    NODE_A,
    STOP_PAYLOAD,
    _restart_interrupted_result,
    job_state,
    parent,
)
from .test_agent_jobs import service as _service_fixture

agent_service = _service_fixture
from .test_agent_operation_lifecycle import KIND_STOP, _job_run_payload, _stored


# Wrong implementation caught: exhausted recovery retains the queue head
# and prevents a fresh request after the dependency recovers.
def test_dependency_recovery_is_bounded_and_fresh_request_is_claimable(
    agent_service,
) -> None:
    from vonk_agent_protocol import (
        AgentFailureKind,
        AgentFailureResult,
        AgentResult,
        AgentResultState,
        FailureCode,
        OutcomeFailed,
        RecipeStopResult,
    )
    from vonk_control.recovery_policy import RecoveryPolicy

    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    policy = RecoveryPolicy()
    original_deadline = None
    # A request-owned deadline permits retries beyond the fallback count budget.
    for ordinal in range(1, policy.max_failures + 2):
        jobs = AgentJobService(sessions, clock=clock)
        claim = claim_agent(jobs, NODE_A, "serial-a")
        assert claim is not None
        jobs.record_result(
            AgentResult(
                fence=claim.fence,
                state=AgentResultState.FAILED,
                result=OutcomeFailed(
                    kind=OutcomeKind.FAILED,
                    code=FailureCode.RUNTIME_OBSERVATION_UNAVAILABLE,
                    reason="runtime storage unavailable",
                    failure_kind=AgentFailureKind.TEMPORARY_DEPENDENCY,
                ),
            )
        )
        stored = _stored(sessions, operation.id)
        assert stored.current_attempt == ordinal
        assert stored.recovery_deadline is not None
        if original_deadline is None:
            original_deadline = stored.recovery_deadline
        assert stored.recovery_deadline == original_deadline
        assert stored.state == State.BACKOFF.value
        assert stored.next_action_at is not None
        due = stored.next_action_at.replace(tzinfo=UTC)
        assert (
            timedelta(seconds=1)
            <= due - clock.now
            <= timedelta(seconds=policy.max_delay_seconds)
        )
        clock.now = due + timedelta(seconds=1)
    import json

    failure = AgentFailureResult.model_validate_json(
        json.dumps(fenced_attempt(sessions, claim).result)
    )
    reason_code = failure.error_code
    assert reason_code == FailureCode.RUNTIME_OBSERVATION_UNAVAILABLE
    assert original_deadline is not None
    clock.now = original_deadline.replace(tzinfo=UTC)
    jobs = AgentJobService(sessions, clock=clock)
    jobs.reconcile_orders()
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    ended = _stored(sessions, operation.id)
    assert ended.state == State.FAILED.value
    assert ended.next_action_at is None
    assert ended.current_attempt == policy.max_failures + 1
    assert job_state(sessions, operation.parent_job_id).state == State.FAILED.value
    fresh = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    admitted = claim_agent(jobs, NODE_A, "serial-a")
    assert admitted is not None
    assert _stored(sessions, fresh.id).current_attempt == 1
    jobs.succeed(admitted, RecipeStopResult())
    assert _stored(sessions, fresh.id).state == State.SUCCEEDED.value


@pytest.mark.parametrize("stage", sorted(JOB_PRE_LAUNCH_STAGES))
def test_job_preparation_retries_without_replaying_an_uncertain_job(
    agent_service, stage: str
) -> None:
    """A wait reported before the job's process was dispatched (its artifacts,
    image, model, inputs, storage or launch layout) is retried: this attempt
    never ran the job. Production trigger: a transient artifact distribution
    or image pull failure ended a user's job failed."""
    from vonk_agent_protocol import (
        AgentFailureKind,
        AgentResult,
        AgentResultState,
        FailureCode,
        ObservationCause,
        OutcomeEvidence,
        OutcomeFailed,
    )

    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.FAILED,
            result=OutcomeFailed(
                kind=OutcomeKind.FAILED,
                code=FailureCode.RUNTIME_OBSERVATION_UNAVAILABLE,
                reason="preparation storage unavailable",
                failure_kind=AgentFailureKind.TEMPORARY_DEPENDENCY,
                evidence=OutcomeEvidence(stage=stage),
            ),
        )
    )
    scheduled = _stored(sessions, operation.id)
    assert scheduled.next_action_at is not None
    clock.now = scheduled.next_action_at.replace(tzinfo=UTC) + timedelta(seconds=1)
    next_claim = claim_agent(jobs, NODE_A, "serial-a")
    assert next_claim is not None and next_claim.fence != claim.fence
    # An executed job does not carry the preparation boundary; its uncertain
    # result must never become a second blind execute claim.
    jobs.record_result(_restart_interrupted_result(next_claim, kind))
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    from vonk_control.lifecycle.core import OBSERVE_BUDGET

    for _ in range(OBSERVE_BUDGET + 2):
        retained = _stored(sessions, operation.id)
        assert retained.next_action_at is not None
        clock.now = retained.next_action_at.replace(tzinfo=UTC) + timedelta(seconds=1)
        jobs.reconcile_orders()
        assert claim_agent(jobs, NODE_A, "serial-a") is None
    # A spent budget never declares the job failed while it may run: it is lost.
    assert _stored(sessions, operation.id).state == State.OBSERVING.value
    ended_attempt = fenced_attempt(sessions, next_claim)
    # Reconciliation may replace the report cause when it lapses the fence.
    # Validate the retained diagnostic, and prove that the fence cannot own work.
    from vonk_control.agent_operation_facts import attempt_is_live

    ObservationCause(ended_attempt.observation_cause)
    assert not attempt_is_live(
        _stored(sessions, operation.id), ended_attempt, clock.now
    )
    fresh = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    assert _stored(sessions, fresh.id).current_attempt == 1


@pytest.mark.parametrize(
    "stage",
    [
        FailureStage.JOB_STATE.value,
        FailureStage.OUTPUT_STORAGE.value,
        FailureStage.RUNTIME_ADAPTER.value,
        FailureStage.JOB_STOP.value,
    ],
)
def test_a_job_wait_after_dispatch_is_never_reissued(agent_service, stage: str) -> None:
    """A stage a job also reaches after its process was dispatched never proves
    the attempt did not run: the job is never issued a second time."""
    from vonk_agent_protocol import (
        AgentFailureKind,
        AgentResult,
        AgentResultState,
        FailureCode,
        OutcomeEvidence,
        OutcomeFailed,
    )

    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.FAILED,
            result=OutcomeFailed(
                kind=OutcomeKind.FAILED,
                code=FailureCode.RUNTIME_OBSERVATION_UNAVAILABLE,
                reason="storage unavailable",
                failure_kind=AgentFailureKind.TEMPORARY_DEPENDENCY,
                evidence=OutcomeEvidence(stage=stage),
            ),
        )
    )
    # Ended once, with the agent's own typed evidence.
    ended = fenced_attempt(sessions, claim).result
    assert ended is not None
    reason_code = ended["error_code"]
    assert reason_code == FailureCode.RUNTIME_OBSERVATION_UNAVAILABLE.value
    assert _stored(sessions, operation.id).state == State.FAILED.value
    clock.now += timedelta(hours=1)
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    fresh = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    assert _stored(sessions, fresh.id).current_attempt == 1


def test_a_reports_stage_decides_a_job_reissue_in_one_place() -> None:
    """Whole tree: only the pre-launch predicate reads a report's stage, so no
    second rule can reissue (or refuse to reissue) a job by its own list."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "vonk_control"
    readers = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "stage"
    )
    assert readers == ["agent_jobs/retry.py"]


def test_every_wait_the_agent_reports_before_a_job_launches_is_reissued() -> None:
    """Across the boundary: each stage the agent reports a job's pre-launch
    wait at (its job and installation preparation) is one the Controller
    reissues from, so no such wait ends a job that never ran."""
    import re
    from pathlib import Path

    executor = (
        Path(__file__).resolve().parents[2] / "rust/crates/vonk-agent/src/executor"
    )
    waits = re.compile(
        r"(?:job_step_before_launch|temporary_preparation_failure)\(\s*"
        r"FailureStage::(\w+)"
    )
    installation = (executor / "installation.rs").read_text()
    # A job prepares its installation (artifacts, image, model) up to the
    # install order's own entry point.
    preparation = installation[
        installation.index("fn prepare_installation") : installation.index(
            "fn execute_install"
        )
    ]
    stages = {
        re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()
        for source in ((executor / "jobs.rs").read_text(), preparation)
        for name in waits.findall(source)
    }
    assert {"job-inputs", "run-storage", "runtime-projection"} <= stages
    assert stages <= JOB_PRE_LAUNCH_STAGES
