"""Restart-safe canonical progress and independent exact child identity."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from vonk_control.lifecycle.run_switch import RunSwitchAdapter, _stored_child
from vonk_control.lifecycle.types import State
from vonk_control.models import Job
from vonk_control.run_switch_contract import RunSwitchOperationResult
from vonk_control.run_switch_observation_contract import (
    RunSwitchArtifactGuardEvidence,
    RunSwitchStoredIdentity,
)
from vonk_control.run_switch_operations import _persisted_result, _read_progress


def test_canonical_retry_recovers_after_persisted_restart() -> None:
    now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    job = Job(
        id=str(uuid4()),
        kind="recipe.run-switch.v2",
        state="running",
        request_id=str(uuid4()),
        actor="operator",
        targets=["spk_" + "1" * 32],
        payload={"workload_intent_ordinal": 7, "plan": {"damaged": True}},
        result={},
        created_at=now,
        updated_at=now,
    )
    progress = RunSwitchOperationResult(workload_intent_ordinal=7)
    adapter = RunSwitchAdapter(clock=lambda: now)
    pending = adapter.retry(job, progress, "receipt unavailable", now)
    assert pending.state is State.BACKOFF
    assert progress.observation_due_at is not None
    job.result = _persisted_result(progress)
    assert job.result is not None
    assert isinstance(job.result["observation_due_at"], str)

    restarted = RunSwitchAdapter(clock=lambda: now + timedelta(seconds=60))
    restored = _read_progress(job.result)
    assert restored.observation_due_at == progress.observation_due_at
    assert restarted.adopt(job).intent_ordinal == 7
    recovered = restarted.succeed(job, restored, now + timedelta(seconds=60))
    assert recovered.state is State.SUCCEEDED
    assert job.state == "succeeded"
    assert restored.retry_reason is None


def test_damaged_sibling_bookkeeping_retains_exact_child_without_coercion() -> None:
    child_id = str(uuid4())
    assert (
        _stored_child({"child_operation_id": child_id, "phase_results": ["bad"]})
        == child_id
    )
    assert _stored_child({"child_operation_id": 3}) is None
    now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    job = Job(
        id=str(uuid4()),
        kind="recipe.run-switch.v2",
        state="running",
        payload={"workload_intent_ordinal": 7},
        result={"child_operation_id": child_id, "phase_results": ["bad"]},
    )
    assert RunSwitchAdapter(clock=lambda: now).adopt(job).effect.value == "issued"
    identity = RunSwitchStoredIdentity.model_validate(
        {
            "action": "run",
            "plan_digest": "a" * 64,
            "workload_intent_ordinal": True,
            "plan": {"damaged": True},
        }
    )
    assert identity.action == "run"
    assert identity.plan_digest == "a" * 64
    assert identity.workload_intent_ordinal is None
    with pytest.raises(ValidationError):
        RunSwitchArtifactGuardEvidence.model_validate({"downloaded_bytes": True})
