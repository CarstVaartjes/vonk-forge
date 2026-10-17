"""Uninstall observation ends without retaining a queue head or claiming deletion."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from vonk_agent_protocol import (
    TERMINAL_LIFECYCLE_STATES,
    AgentFailureKind,
    AgentOperation,
    AgentResult,
    AgentResultState,
    FailureCode,
    HelperErrorCode,
    LifecycleState,
    OutcomeDone,
    OutcomeEvidence,
    OutcomeFailed,
    OutcomeKind,
    OutcomeUnknown,
    RecipeStopPayload,
    RecipeStopResult,
    RecipeUninstallPayload,
    RecipeUninstallResult,
    WaitReason,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_jobs.contracts import _GRANT_LIFETIME
from vonk_control.models import AgentCertificate, AgentNode, Job

from .agent_fences import fenced_operation
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import claim_agent
from .test_agent_jobs import NODE_A, parent
from .test_agent_jobs import service as service  # noqa: PLC0414
from .test_agent_jobs_postgres import (
    service as pg_service,  # noqa: F401 - pytest fixture
)


def _present(sessions, now) -> None:
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.last_seen_at = now


def exercise_unknown_cleanup(
    jobs,
    sessions,
    clock,
    failure=None,
    *,
    operation_kind=AgentOperation.RECIPE_UNINSTALL,
    wait_reason=WaitReason.CLEANUP_UNCONFIRMED,
) -> None:
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.not_after = clock.now + _GRANT_LIFETIME * 3
    owner = parent(sessions, clock)
    payload = RecipeUninstallPayload(
        installation_id=str(uuid4()),
        plan_digest="a" * 64,
        recipe_content_sha256="b" * 64,
        cleanup_model_content_sha256=None,
    )
    if operation_kind is AgentOperation.RECIPE_STOP:
        payload = RecipeStopPayload.model_validate_json(
            json.dumps(recipe_stop_payload(NODE_A, plan_digest="a" * 64))
        )
    jobs.enqueue(owner.id, NODE_A, operation_kind, "a" * 64, payload)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    unknown = OutcomeUnknown(
        kind=OutcomeKind.UNKNOWN,
        wait_reason=wait_reason,
        reason="local installation observation is unavailable",
    )
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.FAILED if failure else AgentResultState.OBSERVING,
            result=failure or unknown,
        )
    )
    if failure is None:
        with sessions() as session:
            stored = session.get(Job, owner.id)
            assert stored is not None and stored.state == LifecycleState.QUEUED
        # The immutable recovery deadline survives recreation and later attempts.
        clock.now += _GRANT_LIFETIME + timedelta(seconds=1)
        # Its Spark is present the whole time: an unknown it does not resolve
        # there has stalled (a Spark that is away is waited for instead).
        _present(sessions, clock.now)
        jobs = AgentJobService(sessions, clock=clock)
        assert jobs.reconcile_orders()
    # A definite failure (no transient kind) ends at once; it is not reissued.
    with sessions() as session:
        stored = session.get(Job, owner.id)
        assert stored is not None and stored.state in TERMINAL_LIFECYCLE_STATES
        assert stored.state != LifecycleState.SUCCEEDED
    fresh_owner = parent(sessions, clock)
    fresh = jobs.enqueue(fresh_owner.id, NODE_A, operation_kind, "a" * 64, payload)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    assert claim.payload == payload
    assert fenced_operation(sessions, claim).id == fresh.id
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(
                kind=OutcomeKind.DONE,
                result=(
                    RecipeStopResult()
                    if operation_kind is AgentOperation.RECIPE_STOP
                    else RecipeUninstallResult()
                ),
            ),
        )
    )
    with sessions() as session:
        stored = session.get(Job, fresh_owner.id)
        assert stored is not None and stored.state == LifecycleState.SUCCEEDED


@pytest.mark.parametrize(
    ("operation_kind", "wait_reason"),
    [
        (AgentOperation.RECIPE_UNINSTALL, WaitReason.CLEANUP_UNCONFIRMED),
        (AgentOperation.RECIPE_UNINSTALL, WaitReason.OBSERVATION_UNAVAILABLE),
        (AgentOperation.RECIPE_STOP, WaitReason.STOP_UNCONFIRMED),
        (AgentOperation.RECIPE_STOP, WaitReason.RECEIPT_MISSING),
    ],
)
def test_unknown_cleanup_ends_and_a_fresh_request_completes(
    service, operation_kind, wait_reason
) -> None:
    exercise_unknown_cleanup(
        *service, operation_kind=operation_kind, wait_reason=wait_reason
    )


def test_unknown_cleanup_budget_on_postgres(pg_service) -> None:  # noqa: F811 - pytest fixture
    sessions, clock = pg_service
    exercise_unknown_cleanup(AgentJobService(sessions, clock=clock), sessions, clock)


def test_delayed_first_claim_does_not_consume_recovery_budget(service) -> None:
    jobs, sessions, clock = service
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        certificate.not_after = clock.now + _GRANT_LIFETIME * 3
    owner = parent(sessions, clock)
    payload = RecipeUninstallPayload(
        installation_id=str(uuid4()),
        plan_digest="a" * 64,
        recipe_content_sha256="b" * 64,
        cleanup_model_content_sha256=None,
    )
    operation = jobs.enqueue(
        owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload
    )
    clock.now += _GRANT_LIFETIME - timedelta(seconds=10)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.now += timedelta(seconds=11)
    jobs.reconcile_orders()
    from vonk_control.models import AgentOperation as StoredOperation

    with sessions() as session:
        stored = session.get(StoredOperation, operation.id)
        assert stored.state == LifecycleState.RUNNING
        assert stored.recovery_deadline is None
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeUninstallResult()),
        )
    )
    fresh_owner = parent(sessions, clock)
    jobs.enqueue(
        fresh_owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None


def test_observation_lock_contention_is_a_nonblocking_miss(service) -> None:
    from threading import Event, Thread

    jobs, sessions, clock = service
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.not_after = clock.now + _GRANT_LIFETIME * 3
    owner = parent(sessions, clock)
    payload = RecipeUninstallPayload(
        installation_id=str(uuid4()),
        plan_digest="a" * 64,
        recipe_content_sha256="b" * 64,
        cleanup_model_content_sha256=None,
    )
    jobs.enqueue(owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.OBSERVING,
            result=OutcomeUnknown(
                kind=OutcomeKind.UNKNOWN,
                wait_reason=WaitReason.CLEANUP_UNCONFIRMED,
                reason="stored observation unavailable",
            ),
        )
    )
    clock.now += _GRANT_LIFETIME + timedelta(seconds=1)
    _present(sessions, clock.now)
    entered, release = Event(), Event()

    def occupy():
        with jobs._claim_lock:
            entered.set()
            assert release.wait(timeout=30)

    holder = Thread(target=occupy)
    holder.start()
    assert entered.wait(timeout=30)
    try:
        # A contended pass must return while the foreground owner still holds it.
        jobs.reconcile_orders()
        assert not release.is_set()
    finally:
        release.set()
        holder.join(timeout=30)
    jobs.reconcile_orders()
    fresh_owner = parent(sessions, clock)
    payload = RecipeUninstallPayload(
        installation_id=str(uuid4()),
        plan_digest="a" * 64,
        recipe_content_sha256="b" * 64,
        cleanup_model_content_sha256=None,
    )
    jobs.enqueue(
        fresh_owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None


def test_live_retry_crosses_its_recovery_deadline_without_losing_ownership(
    service,
) -> None:
    jobs, sessions, clock = service
    from vonk_control.models import AgentOperation as StoredOperation

    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.not_after = clock.now + _GRANT_LIFETIME * 3
    owner = parent(sessions, clock)
    payload = RecipeUninstallPayload(
        installation_id=str(uuid4()),
        plan_digest="a" * 64,
        recipe_content_sha256="b" * 64,
        cleanup_model_content_sha256=None,
    )
    operation = jobs.enqueue(
        owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.OBSERVING,
            result=OutcomeUnknown(
                kind=OutcomeKind.UNKNOWN,
                wait_reason=WaitReason.CLEANUP_UNCONFIRMED,
                reason="stored observation unavailable",
            ),
        )
    )
    clock.now += _GRANT_LIFETIME - timedelta(seconds=5)
    retry = claim_agent(jobs, NODE_A, "serial-a")
    assert retry is not None
    jobs.heartbeat(retry, None, 60)
    clock.now += timedelta(seconds=6)
    jobs.reconcile_orders()
    with sessions() as session:
        stored = session.get(StoredOperation, operation.id)
        assert stored is not None and stored.state == LifecycleState.RUNNING
    jobs.record_result(
        AgentResult(
            fence=retry.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeUninstallResult()),
        )
    )
    fresh_owner = parent(sessions, clock)
    jobs.enqueue(
        fresh_owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None


@pytest.mark.parametrize("failure_kind", [None, AgentFailureKind.INVALID_CONTRACT])
def test_a_definite_cleanup_failure_ends_at_once_and_admits_fresh(
    service, failure_kind
) -> None:
    failure = OutcomeFailed(
        kind=OutcomeKind.FAILED,
        code=FailureCode.RECIPE_UNINSTALL_FAILED,
        reason="stored cleanup evidence unavailable",
        failure_kind=failure_kind,
    )
    exercise_unknown_cleanup(*service, failure=failure)


def test_explicit_helper_denial_has_no_reissue_and_allows_fresh_admission(
    service,
) -> None:
    jobs, sessions, clock = service
    owner = parent(sessions, clock)
    payload = RecipeUninstallPayload(
        installation_id=str(uuid4()),
        plan_digest="a" * 64,
        recipe_content_sha256="b" * 64,
        cleanup_model_content_sha256=None,
    )
    jobs.enqueue(owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.FAILED,
            result=OutcomeFailed(
                kind=OutcomeKind.FAILED,
                code=FailureCode.RECIPE_UNINSTALL_FAILED,
                reason="helper denied authority",
                evidence=OutcomeEvidence(
                    helper_error_code=HelperErrorCode.GRANT_UNAUTHORIZED
                ),
            ),
        )
    )
    with sessions() as session:
        ended = session.get(Job, owner.id)
        assert ended is not None and ended.state in TERMINAL_LIFECYCLE_STATES
    jobs.reconcile_orders()
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    fresh_owner = parent(sessions, clock)
    fresh = jobs.enqueue(
        fresh_owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload
    )
    independent = claim_agent(jobs, NODE_A, "serial-a")
    assert (
        independent is not None
        and fenced_operation(sessions, independent).id == fresh.id
    )


def test_an_order_waits_for_its_spark_to_return_past_the_old_budget(service) -> None:
    """Catches the fixed one-hour order budget failing work whose Spark is away.

    An enrolled Spark that is away may return: its order waits for it however
    long, and the window restarts when it is back (then a present Spark that
    leaves the order unresolved for a whole window ends it).
    """
    from vonk_control.models import AgentOperation as StoredOperation

    jobs, sessions, clock = service
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.not_after = clock.now + _GRANT_LIFETIME * 30
    owner = parent(sessions, clock)
    payload = RecipeUninstallPayload(
        installation_id=str(uuid4()),
        plan_digest="a" * 64,
        recipe_content_sha256="b" * 64,
        cleanup_model_content_sha256=None,
    )
    operation = jobs.enqueue(
        owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.OBSERVING,
            result=OutcomeUnknown(
                kind=OutcomeKind.UNKNOWN,
                wait_reason=WaitReason.CLEANUP_UNCONFIRMED,
                reason="the Spark went away before confirming",
            ),
        )
    )
    # Away for many old budgets: the order is still waiting for its Spark.
    for _ in range(6):
        clock.now += _GRANT_LIFETIME + timedelta(seconds=1)
        jobs.reconcile_orders()
    with sessions() as session:
        stored = session.get(StoredOperation, operation.id)
        assert stored is not None
        assert stored.state not in TERMINAL_LIFECYCLE_STATES
        job = session.get(Job, owner.id)
        assert job is not None and job.state not in TERMINAL_LIFECYCLE_STATES
    # Back, and it stays unresolved there for a whole window: it has stalled.
    _present(sessions, clock.now)
    clock.now += _GRANT_LIFETIME + timedelta(seconds=1)
    _present(sessions, clock.now)
    jobs.reconcile_orders()
    with sessions() as session:
        job = session.get(Job, owner.id)
        assert job is not None and job.state in TERMINAL_LIFECYCLE_STATES
    fresh_owner = parent(sessions, clock)
    jobs.enqueue(
        fresh_owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
