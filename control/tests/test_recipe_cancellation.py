"""Cancellation input cannot mutate intent; valid input ends and frees admission."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState
from vonk_control.models import AgentOperation, Job

from .test_recipe_operations import setup_services


def test_invalid_cancel_has_no_effect_and_valid_cancel_admits_fresh(tmp_path) -> None:
    sessions, service, _queue, mapping, build, _nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping, build)
    original = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
    )
    with sessions() as session:
        before = tuple(session.execute(select(Job.id, Job.state, Job.result)))
    with pytest.raises(Exception) as _ending:
        service.cancel(
            original.id, actor="admin", request_id=str(uuid4()), reason="   "
        )
    with sessions() as session:
        assert tuple(session.execute(select(Job.id, Job.state, Job.result))) == before
    cancelled = service.cancel(
        original.id, actor="admin", request_id=str(uuid4()), reason="x" * 900
    )
    assert cancelled.state == LifecycleState.CANCELLED
    with sessions() as session:
        assert set(
            session.scalars(
                select(AgentOperation.state).where(
                    AgentOperation.parent_job_id == original.id
                )
            )
        ) == {LifecycleState.CANCELLED}
    fresh_plan = service.preview_install(mapping, build)
    assert fresh_plan.allowed
    fresh = service.install(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    assert fresh.id != cancelled.id
    assert fresh.state in {LifecycleState.QUEUED, LifecycleState.RUNNING}


def test_cancel_waits_for_a_live_order_and_the_order_ends_it(tmp_path) -> None:
    """A parent is not cancelled under an order that is still live.

    An order backing off for its retry is live: the cancel is recorded on the
    parent, the order's own lifecycle ends it, and the parent then ends.
    """
    from vonk_control.agent_jobs import AgentJobService
    from vonk_control.agent_jobs.persistence import _OrderStore
    from vonk_control.lifecycle.agent_operation import AgentOperationAdapter

    from .test_recipe_operations import NOW

    sessions, service, _queue, mapping, build, _nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping, build)
    original = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
    )
    with sessions.begin() as session:
        orders = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == original.id
                )
            )
        )
        assert orders
        for order in orders:
            order.state = LifecycleState.BACKOFF.value
            order.next_action_at = NOW
    flagged = service.cancel(
        original.id, actor="admin", request_id=str(uuid4()), reason="stop it"
    )
    assert flagged.state not in {LifecycleState.CANCELLED, LifecycleState.FAILED}

    store = _OrderStore(
        AgentJobService(sessions, clock=lambda: NOW),
        AgentOperationAdapter(sessions=sessions, clock=lambda: NOW),
    )
    for order in orders:
        store.redrive(order.id)
    with sessions() as session:
        assert set(
            session.scalars(
                select(AgentOperation.state).where(
                    AgentOperation.parent_job_id == original.id
                )
            )
        ) == {LifecycleState.CANCELLED}
    assert service.get(original.id).state == LifecycleState.CANCELLED
    fresh_plan = service.preview_install(mapping, build)
    assert fresh_plan.allowed
    fresh = service.install(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    assert fresh.state in {LifecycleState.QUEUED, LifecycleState.RUNNING}
