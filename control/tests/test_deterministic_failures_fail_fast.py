"""A deterministic failure ends the operation with evidence; a transient one retries.

The same input raises the same error on every attempt, so retrying or replanning
it can only loop.  Each layer that decides a retry is driven here by behaviour:
the run-switch phase handler, the run-switch tick guard, and an agent result
the Controller refuses at its input contract (see also
``test_agent_api.test_boundary_failures_record_a_correlated_operator_reason``).
"""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from sqlalchemy.exc import OperationalError
from vonk_control.failure_evidence import FailureEvidenceService
from vonk_control.models import Job
from vonk_control.run_switch_contract import ArtifactVerificationEvidence
from vonk_control.run_switch_operations import PhaseExecution, _phase_result

from .test_run_switch_lifecycle import _Harness, _result, _retrying, _ScriptedExecutor
from .test_run_switch_operations import (
    RecordingArtifactExecutor,
    _child_operation_id,
    _replace_child,
)


def _evidence(harness: _Harness) -> str:
    with harness.sessions() as session:
        job = session.get(Job, harness.id)
        assert job is not None
        attempt = max(1, job.current_attempt)
    bundle = FailureEvidenceService(
        harness.sessions, clock=lambda: harness.now[0]
    ).read(harness.id, attempt)
    return bundle.model_dump_json()


def _invalid_receipt(phase):
    return PhaseExecution(result=_phase_result({"verified": False}, phase=phase))


@pytest.mark.parametrize(
    ("phase", "fault", "cause"),
    [
        (
            "transfer",
            RuntimeError("verified model cache byte total does not match the plan"),
            "byte total does not match the plan",
        ),
        (
            "transfer",
            ValueError("distribution assignment objects are duplicated"),
            "assignment objects are duplicated",
        ),
        ("transfer", KeyError("plan_digest"), "plan_digest"),
        ("verify", _invalid_receipt, "verified"),
    ],
    ids=["identity-check", "validation", "programming-error", "invalid-receipt"],
)
def test_a_deterministic_phase_error_fails_after_one_attempt_with_evidence(
    tmp_path: Path, phase: str, fault, cause: str
) -> None:
    """Wrong implementation: any RuntimeError/ValueError was retried and replanned,
    so the same check failed again on every attempt for the whole window."""

    executor = _ScriptedExecutor()
    executor.faults[phase] = fault
    harness = _Harness(tmp_path, executor)
    for _ in range(8):
        harness.service.tick()
        harness.advance_to_due()
    view = harness.view()
    assert view.state == "failed", (view.state, view.status_reason)
    assert executor.calls.count(phase) == 1
    assert cause in (view.status_reason or "")
    assert cause in _evidence(harness)
    harness.assert_fresh_admitted()


def test_a_child_receipt_that_does_not_validate_fails_after_one_attempt(
    tmp_path: Path,
) -> None:
    """Wrong implementation: the child was reissued for a fresh receipt, which
    the same producer wrote the same invalid way, for the whole window."""

    executor = RecordingArtifactExecutor(child_transfer=True)
    harness = _Harness(tmp_path, executor)
    for _ in range(8):
        harness.service.tick()
        if _result(harness.view()).child_operation_id is not None:
            break
    child_id = _child_operation_id(harness.view())
    _replace_child(executor, child_id, state="succeeded")
    _replace_child(
        executor,
        child_id,
        result=executor.children[child_id].result.model_copy(
            update={
                "evidence": [
                    ArtifactVerificationEvidence.model_construct(
                        node_id="", downloaded_bytes=-1
                    )
                ]
            }
        ),
    )
    for _ in range(4):
        harness.service.tick()
        harness.advance_to_due()
    view = harness.view()
    assert view.state == "failed", (view.state, view.status_reason)
    assert executor.calls.count("transfer") == 1
    assert "phase receipt is invalid" in _evidence(harness)


@pytest.mark.parametrize(
    "fault",
    [
        OperationalError(
            "SELECT ... FOR UPDATE",
            None,
            TimeoutError("canceling statement due to lock timeout"),
        ),
        httpx2.ConnectError("connection refused"),
        httpx2.HTTPStatusError(
            "service unavailable",
            request=httpx2.Request("GET", "http://nas/"),
            response=httpx2.Response(503, request=httpx2.Request("GET", "http://nas/")),
        ),
    ],
    ids=["lock-timeout", "connection-refused", "unavailable"],
)
def test_a_transient_phase_error_is_retried_then_recovers(
    tmp_path: Path, fault
) -> None:
    executor = _ScriptedExecutor()
    executor.faults["transfer"] = fault
    harness = _Harness(tmp_path, executor)
    for _ in range(8):
        harness.service.tick()
        if executor.calls.count("transfer"):
            break
    held = harness.view()
    assert _retrying(held), (held.state, held.status_reason)
    held_phase = _result(held).phase_index
    executor.faults.clear()
    for _ in range(12):
        harness.advance_to_due()
        harness.service.tick()
        if _result(harness.view()).phase_index > held_phase:
            break
    assert _result(harness.view()).phase_index > held_phase
    assert harness.view().state != "failed"


def test_a_deterministic_advance_error_fails_once_through_the_tick_guard(
    tmp_path: Path,
) -> None:
    """Outside the phase handler the tick guard decides the same way."""

    harness = _Harness(tmp_path, _ScriptedExecutor())
    calls: list[str] = []

    def broken(operation_id: str) -> bool:
        calls.append(operation_id)
        raise TypeError("'NoneType' object is not subscriptable")

    harness.service._advance = broken  # type: ignore[method-assign]
    for _ in range(4):
        harness.service.tick()
        harness.advance_to_due()
    view = harness.view()
    assert view.state == "failed", (view.state, view.status_reason)
    assert calls == [harness.id]
    assert "not subscriptable" in _evidence(harness)
