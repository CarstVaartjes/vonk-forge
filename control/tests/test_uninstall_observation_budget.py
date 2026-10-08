"""Uninstall observation ends without retaining a queue head or claiming deletion."""

from datetime import timedelta
from uuid import uuid4

from vonk_agent_protocol import (
    AgentOperation,
    AgentResult,
    AgentResultState,
    LifecycleState,
    OutcomeDone,
    OutcomeKind,
    OutcomeUnknown,
    RecipeUninstallPayload,
    RecipeUninstallResult,
    WaitReason,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_jobs.contracts import _GRANT_LIFETIME
from vonk_control.models import AgentCertificate, Job

from .agent_fences import fenced_operation
from .runtime_identity_support import claim_agent
from .test_agent_jobs import NODE_A, parent
from .test_agent_jobs import service as service  # noqa: PLC0414
from .test_agent_jobs_postgres import (
    service as pg_service,  # noqa: F401 - pytest fixture
)


def exercise_unknown_cleanup(jobs, sessions, clock) -> None:
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
    unknown = OutcomeUnknown(
        kind=OutcomeKind.UNKNOWN,
        wait_reason=WaitReason.CLEANUP_UNCONFIRMED,
        reason="local installation observation is unavailable",
    )
    jobs.record_result(
        AgentResult(fence=claim.fence, state=AgentResultState.OBSERVING, result=unknown)
    )
    with sessions() as session:
        stored = session.get(Job, owner.id)
        assert stored is not None and stored.state == LifecycleState.QUEUED
    # The immutable request age survives service recreation and later attempts.
    clock.now += _GRANT_LIFETIME + timedelta(seconds=1)
    jobs = AgentJobService(sessions, clock=clock)
    assert jobs.reconcile_orders()
    with sessions() as session:
        stored = session.get(Job, owner.id)
        assert stored is not None and stored.state == LifecycleState.CANCELLED
    fresh_owner = parent(sessions, clock)
    fresh = jobs.enqueue(
        fresh_owner.id, NODE_A, AgentOperation.RECIPE_UNINSTALL, "a" * 64, payload
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    assert claim.payload == payload
    assert fenced_operation(sessions, claim).id == fresh.id
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeUninstallResult()),
        )
    )
    with sessions() as session:
        stored = session.get(Job, fresh_owner.id)
        assert stored is not None and stored.state == LifecycleState.SUCCEEDED


def test_unknown_cleanup_ends_and_a_fresh_request_completes(service) -> None:
    exercise_unknown_cleanup(*service)


def test_unknown_cleanup_budget_on_postgres(pg_service) -> None:  # noqa: F811 - pytest fixture
    sessions, clock = pg_service
    exercise_unknown_cleanup(AgentJobService(sessions, clock=clock), sessions, clock)
