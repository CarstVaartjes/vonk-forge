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
