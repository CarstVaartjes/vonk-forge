"""Journal cancellation writes a result the owning strict reader can consume."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, OperationProgress
from vonk_control.model_primitives import Base
from vonk_control.models.jobs import Job
from vonk_control.run_switch_contract.operations import RunSwitchOperationResult
from vonk_control.run_switch_identity_contract import RunSwitchCancellation
from vonk_control.run_switch_journal_repair.pending import record_repair_cancellation
from vonk_control.run_switch_operations.result_helpers import _persisted_result
from vonk_control.run_switch_operations.results import _stored_result


def test_repair_cancellation_producer_round_trips_through_strict_result_reader() -> (
    None
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    operation_id = str(uuid4())
    now = datetime.now(UTC)
    progress = RunSwitchOperationResult(
        child_operation_id=str(uuid4()),
        operation=OperationProgress(
            phase="copying",
            completed_bytes=0,
            total_bytes=None,
            total_bytes_known=False,
        ),
    )
    with sessions.begin() as session:
        session.add(
            Job(
                id=operation_id,
                request_id=str(uuid4()),
                kind="recipe.run-switch.v2",
                state=LifecycleState.RUNNING.value,
                actor="operator",
                authority_revision="a" * 64,
                payload_digest="b" * 64,
                targets=[],
                payload={},
                result=_persisted_result(progress),
                created_at=now,
                updated_at=now,
            )
        )
    cancellation = RunSwitchCancellation(
        request_key=str(uuid4()),
        actor="operator",
        reason="observation ended",
        requested_at=now,
    )
    record_repair_cancellation(sessions, operation_id, cancellation)
    with sessions() as session:
        stored = session.get(Job, operation_id)
        assert stored is not None
        expected = progress.model_copy(update={"cancellation": cancellation})
        assert stored.result == _persisted_result(expected)
        result = _stored_result(stored.result)
        assert isinstance(result, RunSwitchOperationResult)
        assert result == expected
    engine.dispose()
