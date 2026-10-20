"""Real SQL/worker/child recovery preserves issued effects across damaged progress."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentOperation as OperationKind
from vonk_agent_protocol import LifecycleState
from vonk_control.models import AgentOperation, Job, RecipeRun

from .test_run_switch_postgres import _awaiting_final_verification
from .test_run_switch_postgres import (
    migrated_engine as migrated_engine,  # noqa: PLC0414 -- shared fixture
)
from .test_run_switch_transfer_restart_postgres import (
    AgentPeer,
    _complete_parent,
    _observe_transfer,
)
from .test_run_switch_transfer_restart_postgres import (
    transfer as transfer,  # noqa: PLC0414 -- shared fixture
)


@pytest.mark.parametrize("damage", ["measurement", "checkpoint", "item"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_final_verify_reobserves_the_run_without_starting_another(
    tmp_path, migrated_engine, damage
):
    """Catches progress repair retiring a live run or reissuing its accepted Start."""
    sessions, _lifecycle, routes, _publisher, service, operation = (
        _awaiting_final_verification(tmp_path, migrated_engine)
    )
    with sessions() as session:
        run_id = session.scalar(select(RecipeRun.id))
        start_ids = tuple(
            session.scalars(
                select(Job.id).where(Job.kind == OperationKind.RECIPE_START)
            )
        )
    assert run_id is not None
    with sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None and row.result is not None
        fault = {"completed_bytes": 2, "total_bytes": 1, "total_bytes_known": True}
        if damage == "checkpoint":
            fault = {"phase_index": True}
        elif damage == "item":
            fault = {"item_index": True}
        row.result = {**row.result, **fault}
    routes.publish_run(run_id)
    for _ in range(24):
        service.tick()
        view = service.get(operation.operation_id)
        assert view.state != LifecycleState.FAILED, view.status_reason
        if view.state == LifecycleState.SUCCEEDED:
            break
        if view.next_attempt_at is not None:
            due = view.next_attempt_at
            service._clock = lambda due=due: due
    else:
        pytest.fail(view.model_dump_json())
    with sessions() as session:
        assert (
            tuple(
                session.scalars(
                    select(Job.id).where(Job.kind == OperationKind.RECIPE_START)
                )
            )
            == start_ids
        )
        assert tuple(session.scalars(select(RecipeRun.id))) == (run_id,)


@pytest.mark.parametrize("damage", ["measurement", "checkpoint", "item"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_transfer_reconnects_to_the_original_child_and_finishes(
    transfer, tmp_path, damage
):
    """Catches resetting an issued child's identity while repairing its parent meter."""
    sessions, _lifecycle, jobs, _executor, service, parent_id, claim, _now = transfer
    current = service.get(parent_id)
    assert current.result is not None
    child_id = current.result.child_operation_id
    with sessions.begin() as session:
        row = session.get(Job, parent_id)
        assert row is not None and row.result is not None
        fault = {"completed_bytes": 2, "total_bytes": 1, "total_bytes_known": True}
        if damage == "checkpoint":
            fault = {"phase_index": True}
        elif damage == "item":
            fault = {"item_index": True}
        row.result = {**row.result, **fault}
    service._advance(parent_id)
    recovered = service.get(parent_id)
    assert recovered.result is not None
    assert recovered.result.child_operation_id == child_id
    peer = AgentPeer(jobs, tmp_path / (str(uuid4()) + ".json"))
    peer.finish_without_reporting(claim)
    peer.replay()
    _observe_transfer(transfer)
    _complete_parent(transfer)
    with sessions() as session:
        orders = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
            )
        )
        assert len(orders) == 1 and orders[0].current_attempt == 1
