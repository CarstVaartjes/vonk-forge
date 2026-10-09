"""Parent completion and supersession collect background work without phase polling."""

from __future__ import annotations

from concurrent.futures import Future
from datetime import timedelta
from pathlib import Path
from threading import RLock
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState
from vonk_control.distribution_executor import CompositeDistributionPhaseExecutor
from vonk_control.models import AgentNode, Job
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchRuntimeImageResult,
)

from .test_run_switch_bookkeeping import _accepted


def _executor(accepted):
    executor = object.__new__(CompositeDistributionPhaseExecutor)
    executor._sessions = accepted.sessions
    executor._clock = accepted.service._clock
    executor._runtime_image_lock = RLock()
    executor._runtime_image_futures = {}
    return executor


@pytest.mark.parametrize(
    "ending",
    [LifecycleState.CANCELLED, LifecycleState.FAILED, LifecycleState.SUCCEEDED],
)
def test_ended_parent_cancels_queued_work_and_collects_completed_results(
    tmp_path: Path, ending: LifecycleState
) -> None:
    """Catches futures retained until an ended parent's phase is polled again."""
    accepted = _accepted(tmp_path)
    executor = _executor(accepted)
    pending: Future[RunSwitchRuntimeImageResult | None] = Future()
    done: Future[RunSwitchRuntimeImageResult | None] = Future()
    done.set_result(None)
    key = accepted.operation.request_key
    executor._runtime_image_futures[(key, 0, 0)] = (pending, "pending")
    executor._runtime_image_futures[(key, 1, 0)] = (done, "completed")
    with accepted.sessions.begin() as session:
        parent = session.get(Job, accepted.operation.operation_id)
        assert parent is not None
        parent.state = ending
    assert executor.reconcile_background()
    assert pending.cancelled()
    assert not executor._runtime_image_futures
    plan = accepted.service.preview(accepted.request, actor="admin")
    fresh = accepted.service.apply(
        RunSwitchApplyRequest(
            **accepted.request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid4()),
        ),
        actor="admin",
    )
    assert fresh.operation_id != accepted.operation.operation_id


def test_superseded_running_work_is_detached_and_queued_successor_survives(
    tmp_path: Path,
) -> None:
    """Catches obsolete work retaining the newer intent's future or queue head."""
    accepted = _accepted(tmp_path)
    executor = _executor(accepted)
    running: Future[RunSwitchRuntimeImageResult | None] = Future()
    assert running.set_running_or_notify_cancel()
    key = (accepted.operation.request_key, 0, 0)
    executor._runtime_image_futures[key] = (running, "running")
    with accepted.sessions.begin() as session:
        node = session.scalar(select(AgentNode))
        assert node is not None
        node.workload_intent_ordinal += 1
    assert executor.reconcile_background()
    assert not executor._runtime_image_futures
    assert not running.cancelled()
    # Its running effects are fenced by publication ownership. Its eventual
    # completion has no parent-held future or observation slot to retain.
    running.set_result(None)
    assert not executor.reconcile_background()
    plan = accepted.service.preview(accepted.request, actor="admin")
    fresh = accepted.service.apply(
        RunSwitchApplyRequest(
            **accepted.request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid4()),
        ),
        actor="admin",
    )
    successor: Future[RunSwitchRuntimeImageResult | None] = Future()
    successor_key = (fresh.request_key, 0, 0)
    executor._runtime_image_futures[successor_key] = (successor, "queued")
    assert not executor.reconcile_background()
    assert not successor.cancelled()
    executor.end_background(fresh.request_key)
    assert successor.cancelled()
    assert not executor._runtime_image_futures


def test_stalled_ended_parent_cannot_monopolize_unrelated_preparation(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches the single executor thread keeping later requests behind old I/O."""
    from threading import Event

    from vonk_agent_protocol.agent_words import ProfileChildPhase
    from vonk_control.agent_jobs import AgentJobService
    from vonk_control.distribution import DistributionService, MemoryObjectSource
    from vonk_control.run_switch_contract import (
        RunSwitchOperationResult,
        RunSwitchPhase,
    )

    from .test_run_switch_operations import _runtime_receipt

    accepted = _accepted(tmp_path)
    executor = CompositeDistributionPhaseExecutor(
        accepted.sessions,
        AgentJobService(accepted.sessions, clock=accepted.service._clock),
        DistributionService(MemoryObjectSource(), sessions=accepted.sessions),
        clock=accepted.service._clock,
        model_cache=object(),
        async_runtime_image_preparation=True,
    )
    old_entered, fresh_entered, release = Event(), Event(), Event()
    old_key = accepted.operation.request_key
    from vonk_agent_protocol import canonical_message
    from vonk_control.runtime_image_preparation import RuntimeImageReceipt

    receipt = RuntimeImageReceipt.model_validate_json(
        canonical_message(
            _runtime_receipt(accepted.plan, image="sha256:" + "a" * 64, layout="b" * 64)
        )
    )
    prepared = RunSwitchRuntimeImageResult(
        phase=ProfileChildPhase.PREPARE.value,
        subphase="runtime-image",
        runtime_image=receipt,
        image_digest=receipt.image_digest,
        oci_layout_sha256=receipt.oci_archive_sha256,
        image_bytes=receipt.image_bytes,
        build_id=receipt.build_id,
    )

    def prepare(_plan, _phase, *, request_key, **_kwargs):
        if request_key == old_key:
            old_entered.set()
            assert release.wait(5)
        else:
            fresh_entered.set()
        return prepared

    monkeypatch.setattr(executor, "_prepare_runtime_image", prepare)
    phase = RunSwitchPhase.model_construct(
        index=2, kind=ProfileChildPhase.PREPARE, subphase="runtime-image"
    )
    progress = RunSwitchOperationResult()
    try:
        assert executor.execute(
            accepted.plan,
            phase,
            item_index=0,
            actor="admin",
            request_key=old_key,
            progress=progress,
        ).waiting
        assert old_entered.wait(1)
        with executor._runtime_image_lock:
            old_future = executor._runtime_image_futures[(old_key, 2, 0)][0]
        with accepted.sessions.begin() as session:
            parent = session.get(Job, accepted.operation.operation_id)
            assert parent is not None
            parent.state = LifecycleState.FAILED
        executor.reconcile_background()
        plan = accepted.service.preview(accepted.request, actor="admin")
        fresh = accepted.service.apply(
            RunSwitchApplyRequest(
                **accepted.request.model_dump(),
                plan_digest=plan.plan_digest,
                request_key=str(uuid4()),
            ),
            actor="admin",
        )
        assert executor.execute(
            plan,
            phase,
            item_index=0,
            actor="admin",
            request_key=fresh.request_key,
            progress=progress,
        ).waiting
        assert fresh_entered.wait(1)
        with executor._runtime_image_lock:
            future = executor._runtime_image_futures[(fresh.request_key, 2, 0)][0]
        assert future.result(timeout=1) == prepared
        result = executor.execute(
            plan,
            phase,
            item_index=0,
            actor="admin",
            request_key=fresh.request_key,
            progress=progress,
        )
        assert not result.waiting and result.result is not None
        assert not old_future.done()
    finally:
        release.set()
        executor.close()


def test_completed_callback_leaves_database_observation_to_worker_tick(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Catches a completion callback racing the submitting SQL transaction."""
    accepted = _accepted(tmp_path)
    executor = _executor(accepted)
    done: Future[RunSwitchRuntimeImageResult | None] = Future()
    done.set_result(None)
    executor._runtime_image_inflight = {done}

    def forbidden_session():
        raise AssertionError("completion callback opened a database transaction")

    monkeypatch.setattr(executor, "_sessions", forbidden_session)
    executor._background_completed(done)
    assert not executor._runtime_image_inflight


def test_background_collection_uses_the_parents_recovery_deadline(
    tmp_path: Path,
) -> None:
    """Catches collection discarding a completed result inside its owned budget."""
    from vonk_control.run_switch_operations.constants import (
        _FINAL_VERIFICATION_MAX_SECONDS,
    )
    from vonk_control.run_switch_operations.result_helpers import (
        _persisted_result,
        _read_progress,
    )

    accepted = _accepted(tmp_path)
    executor = _executor(accepted)
    done: Future[RunSwitchRuntimeImageResult | None] = Future()
    done.set_result(None)
    key = (accepted.operation.request_key, 0, 0)
    executor._runtime_image_futures[key] = (done, "completed")
    now = executor._clock()
    with accepted.sessions.begin() as session:
        parent = session.get(Job, accepted.operation.operation_id)
        assert parent is not None
        progress = _read_progress(parent.result)
        progress.recovery_deadline_at = now + timedelta(hours=1)
        parent.result = _persisted_result(progress)
    executor._clock = lambda: (
        now + timedelta(seconds=_FINAL_VERIFICATION_MAX_SECONDS + 1)
    )
    assert not executor.reconcile_background()
    assert executor._runtime_image_futures[key][0] is done
