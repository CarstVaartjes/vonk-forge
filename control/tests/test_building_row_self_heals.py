"""A building row is owned by its live build Job, never by the row alone.

A fresh build request for the same input adopts the live Job instead of being
refused as a stale preview or starting a duplicate build. (A row whose Job ended
or is gone admits a fresh attempt: test_claims_never_outlive_owners_postgres.)
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import LifecycleState
from vonk_control.models import Job

from .test_build_cancellation_recovery import _issue, _services


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
