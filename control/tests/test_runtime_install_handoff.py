"""Persisted preflight recovery must dispatch the accepted installation."""

from pathlib import Path
from typing import get_args

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentOperation as OperationKind
from vonk_agent_protocol import LifecycleState, RuntimePreflightCode, canonical_message
from vonk_control.models import AgentNode, AgentOperation, Job
from vonk_control.run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchRuntimeInstallResult,
)
from vonk_control.run_switch_operations import _persisted_result

from .test_lifecycle_preflight import _clear, _finish
from .test_run_switch_operations import _cold_compile_switch


@pytest.mark.parametrize("damaged_attempts", [False, True])
def test_completed_preflight_recovers_and_dispatches_install(
    tmp_path: Path, damaged_attempts: bool
) -> None:
    """Catches constructor-only persistence and orphaned probe retry counters.

    The Controller reloads every checkpoint from SQLite. A host change after a
    successful probe requires a fresh probe, then the same accepted request
    queues its installation. No counters or operation identity are reset to
    escape the wait, including for a checkpoint already damaged by the bug.
    """
    switch = _cold_compile_switch(tmp_path, seconds=0, slow_compiles=0)
    _clear(switch.sessions)
    switch.drive()
    pending = switch.service.get(switch.operation.operation_id)
    assert pending.result is not None and pending.result.preflight is not None
    checkpoint = pending.result.preflight
    node_id = switch.nodes[0]
    assert checkpoint.attempts[node_id] == 1
    assert checkpoint.pending_job_id is not None
    with switch.sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.preflight_fingerprint = "b" * 64
        if damaged_attempts:
            parent = session.get(Job, pending.operation_id)
            assert parent is not None
            result = RunSwitchOperationResult.model_validate_json(
                canonical_message(parent.result)
            )
            assert result.preflight is not None
            result.preflight.attempts.clear()
            parent.result = _persisted_result(result)

    switch.drive()
    waiting = switch.service.get(pending.operation_id)
    assert waiting.result is not None and waiting.result.preflight is not None
    checkpoint = waiting.result.preflight
    assert checkpoint.last_failure_code == RuntimePreflightCode.HOST_CHANGED
    assert checkpoint.attempts[node_id] == 1
    assert checkpoint.next_check_at is not None
    assert waiting.status_reason is not None
    assert waiting.status_reason.startswith(RuntimePreflightCode.HOST_CHANGED)
    switch.clock.now = checkpoint.next_check_at
    switch.service.tick()
    retry = switch.service.get(pending.operation_id)
    assert retry.result is not None and retry.result.preflight is not None
    checkpoint = retry.result.preflight
    assert checkpoint.attempts[node_id] == 2
    assert checkpoint.pending_job_id is not None
    _finish(switch.sessions, checkpoint, switch.clock.now, fingerprint="b" * 64)

    install_subphase = get_args(
        RunSwitchRuntimeInstallResult.model_fields["subphase"].annotation
    )[0]
    for _ in range(16):
        switch.drive()
        if install_subphase in switch.executor.events:
            break
    assert switch.executor.events.count(install_subphase) == 1
    installed = switch.service.get(pending.operation_id)
    assert installed.state == LifecycleState.RUNNING
    assert installed.result is not None
    assert installed.result.child_operation_id is not None
    with switch.sessions() as session:
        child = session.get(Job, installed.result.child_operation_id)
        assert child is not None and child.kind == OperationKind.RECIPE_INSTALL
        orders = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == child.id)
            )
        )
        assert len(orders) == 1
        assert orders[0].kind == OperationKind.RECIPE_INSTALL
        assert orders[0].node_id == node_id
    assert installed.result.preflight is not None
    assert installed.result.preflight.receipts[node_id].fingerprint == "b" * 64
    assert installed.result.phase_results
