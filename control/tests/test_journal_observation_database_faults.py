"""Database observation faults consume the accepted journal repair budget."""

import pytest
from sqlalchemy.exc import OperationalError
from vonk_agent_protocol import canonical_message
from vonk_control import run_switch_journal_repair as repair
from vonk_control.job_documents import RunSwitchRunIntent
from vonk_control.models import RunSwitchJournalRepairPending
from vonk_control.run_switch_journal_contract import (
    JournalRepairDisposition,
    RunSwitchJournalRepairPendingState,
)

from .test_run_switch_zero_transfer_journal_repair_postgres import (
    faulty_install,  # noqa: F401
)


@pytest.mark.parametrize("expiry_fault", [False, True])
def test_database_observation_failure_ends_and_accepts_fresh(
    faulty_install,  # noqa: F811
    monkeypatch,
    expiry_fault,
):
    """Catches observation faults escaping the clock or resetting it after restart."""
    from types import SimpleNamespace
    from uuid import uuid4

    from psycopg.errors import SerializationFailure
    from vonk_control.run_switch_contract import RunSwitchApplyRequest
    from vonk_control.run_switch_operations import _run_switch_payload

    from .non_blocking import assert_ended_without_blocking

    sessions, lifecycle, _, _planner, _, clock, nodes, _, switch, *_ = faulty_install

    def unavailable(*args, **kwargs):
        raise OperationalError(None, None, SerializationFailure("observation conflict"))

    monkeypatch.setattr(repair, "_try_repair_once", unavailable)
    assert (
        repair.try_repair_zero_transfer_journal(sessions, switch, clock[0])
        == JournalRepairDisposition.DEFERRED
    )
    with sessions() as session:
        pending = session.get(RunSwitchJournalRepairPending, switch)
        state = RunSwitchJournalRepairPendingState.model_validate_json(
            canonical_message(pending.progress)
        )
        deadline = state.deadline_at
        assert state.attempts == 1
    from .test_profile_child_progress_projection import _measured_profile_service

    _, restarted = _measured_profile_service(sessions, lifecycle)
    clock[0] = deadline
    if expiry_fault:
        end_observer = repair._end_unproven_journal
        monkeypatch.setattr(repair, "_end_unproven_journal", unavailable)
        assert not restarted.tick()
        with sessions() as session:
            pending = session.get(RunSwitchJournalRepairPending, switch)
            assert pending is not None
            assert pending.deadline_at == deadline
        monkeypatch.setattr(repair, "_end_unproven_journal", end_observer)

    def end(_operation):
        assert restarted.tick()
        return restarted.get(switch)

    with sessions() as session:
        from vonk_control.models import Job

        job = session.get(Job, switch)
        assert job is not None
        parent = _run_switch_payload(job)
        assert parent is not None and isinstance(parent.intent, RunSwitchRunIntent)
        request = parent.intent.request

    def fresh(_world):
        return restarted.apply(
            RunSwitchApplyRequest.model_validate_json(
                canonical_message(
                    request.model_copy(
                        update={"request_key": str(uuid4()), "plan_digest": None}
                    )
                )
            ),
            actor="admin",
        )

    def cause(receipt):
        assert receipt.result.failure_code is not None

    _, admitted = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        restarted.get(switch),
        end=end,
        fresh=fresh,
        assert_reason=cause,
    )
    assert admitted.operation_id != switch
    assert admitted.node_ids == list(nodes)
