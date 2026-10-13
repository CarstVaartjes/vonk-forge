"""A building row is owned by its live build Job, never by the row alone.

A fresh build request for the same input adopts the live Job. When that Job
ended or is gone, the row has no owner and the request starts a fresh attempt
instead of being refused as a stale preview.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import LifecycleState
from vonk_control.models import AgentOperation, Job

from .test_build_cancellation_recovery import _issue, _services

_MISSING = "missing"


def _build(operations, plan):
    return operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )


def _live_build_jobs(sessions, build_id):
    with sessions() as session:
        return {
            job.id
            for job in session.scalars(
                select(Job).where(
                    Job.kind == WireAgentOperation.RECIPE_BUILD.value,
                    Job.payload["owner_id"].as_string() == build_id,
                    Job.state.in_(
                        (LifecycleState.QUEUED.value, LifecycleState.RUNNING.value)
                    ),
                )
            )
        }


@pytest.mark.parametrize(
    "ending", [LifecycleState.CANCELLED.value, LifecycleState.FAILED.value, _MISSING]
)
def test_a_building_row_whose_job_ended_admits_a_fresh_build(
    tmp_path, postgres_engine, ending
):
    sessions, _builds, operations, *_rest, plan = _services(tmp_path, postgres_engine)
    held = _build(operations, plan)
    with sessions.begin() as session:
        if ending == _MISSING:
            session.execute(
                delete(AgentOperation).where(AgentOperation.parent_job_id == held.id)
            )
            session.execute(delete(Job).where(Job.id == held.id))
        else:
            job = session.get(Job, held.id)
            assert job is not None
            job.state = ending

    fresh = _build(operations, plan)

    assert fresh.id != held.id
    assert _live_build_jobs(sessions, plan.build_id) == {fresh.id}
    with sessions() as session:
        assert session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == fresh.id)
        )


@pytest.mark.parametrize("issued", [False, True])
def test_a_fresh_request_adopts_the_live_build_of_the_same_input(
    tmp_path, postgres_engine, issued
):
    sessions, _builds, operations, *_rest, plan = _services(tmp_path, postgres_engine)
    held = _build(operations, plan)
    if issued:
        _issue(sessions, held.id)

    again = _build(operations, plan)

    assert again.id == held.id
    assert _live_build_jobs(sessions, plan.build_id) == {held.id}
