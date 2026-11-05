"""A terminal transfer child cannot hold its parent in an observation loop."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from vonk_agent_protocol import DistributionCode, LifecycleState, WaitReason
from vonk_agent_protocol.agent_words import ProfileChildPhase
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchDistributionEndedResult,
)

from .non_blocking import assert_ended_without_blocking
from .test_recipe_operations import NOW, installed_recipe, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _replace_child,
    _request,
    _service,
)


@pytest.mark.parametrize("uncertain", [True, False])
def test_terminal_transfer_child_ends_parent_with_evidence(
    tmp_path, postgres_engine, uncertain: bool
) -> None:
    """Catches polling a failed child's immutable receipt until a moving deadline."""
    sessions, lifecycle, _queue, mapping, build, nodes = setup_services(
        tmp_path, engine=postgres_engine, nodes=1
    )
    installed_recipe(lifecycle, mapping, build, nodes, request_id=str(uuid4()))
    executor = RecordingArtifactExecutor(child_transfer=True)
    now = [NOW]

    def worker():
        service = _service(
            sessions,
            now[0],
            lifecycle,
            executor,
            artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        )
        service._clock = lambda: now[0]
        return service

    service = worker()
    request = _request(sessions, nodes[0])
    accepted = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid4())),
        actor="admin",
    )
    assert service.tick()
    pending = service.get(accepted.operation_id)
    assert pending.result is not None and pending.result.child_operation_id is not None
    child_id = pending.result.child_operation_id
    _replace_child(
        executor,
        child_id,
        state=LifecycleState.FAILED.value,
        result=RunSwitchDistributionEndedResult(
            phase=ProfileChildPhase.TRANSFER,
            subphase=ProfileChildPhase.TARGET_COPY,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
            error_code=DistributionCode.UNASSIGNED,
            uncertain=uncertain,
        ),
    )
    service = worker()
    assert service.tick()
    ended = service.get(accepted.operation_id)
    assert ended.state == LifecycleState.FAILED.value, ended.status_reason
    assert ended.result is not None
    assert ended.result.failure_code == DistributionCode.UNASSIGNED
    assert ended.result.retryable is uncertain
    assert ended.result.child_operation_id == child_id
    assert ended.next_attempt_at is None
    assert (
        ended.status_reason is not None
        and WaitReason.OBSERVATION_UNAVAILABLE in ended.status_reason
    )
    now[0] += timedelta(hours=2)
    service = worker()
    assert not service.tick()
    assert service.get(accepted.operation_id) == ended
    assert len(executor.children) == 1

    def assert_cause(receipt) -> None:
        assert receipt.result is not None
        assert receipt.result.failure_code == DistributionCode.UNASSIGNED

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        accepted,
        end=lambda _operation: ended,
        fresh=lambda _world: service.apply(
            RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid4())),
            actor="admin",
        ),
        assert_reason=assert_cause,
    )


def test_a_transfer_child_cancelled_by_another_consumer_is_asked_for_again(
    tmp_path, postgres_engine
) -> None:
    """A shared download cancelled by its other owner never fails this order."""
    sessions, lifecycle, _queue, mapping, build, nodes = setup_services(
        tmp_path, engine=postgres_engine, nodes=1
    )
    installed_recipe(lifecycle, mapping, build, nodes, request_id=str(uuid4()))
    executor = RecordingArtifactExecutor(child_transfer=True)
    now = [NOW]

    def worker():
        service = _service(
            sessions,
            now[0],
            lifecycle,
            executor,
            artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        )
        service._clock = lambda: now[0]
        return service

    service = worker()
    request = _request(sessions, nodes[0])
    accepted = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid4())),
        actor="admin",
    )
    assert service.tick()
    started = service.get(accepted.operation_id).result
    assert started is not None
    first = started.child_operation_id
    assert first is not None
    _replace_child(executor, first, state=LifecycleState.CANCELLED.value)
    service = worker()
    assert service.tick()
    waiting = service.get(accepted.operation_id)
    assert waiting.state != LifecycleState.FAILED.value, waiting.status_reason
    assert waiting.result is not None and waiting.result.child_operation_id is None
    # The re-request is due within the dependency window, not after it.
    for _ in range(6):
        now[0] += timedelta(minutes=5)
        service = worker()
        service.tick()
    again = service.get(accepted.operation_id)
    assert again.state != LifecycleState.FAILED.value, again.status_reason
    assert len(executor.children) == 2
