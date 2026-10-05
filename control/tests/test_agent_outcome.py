"""The Controller reads typed and legacy agent results through one adapter.

``agent_outcome`` is the single function that interprets an agent's report.  These
tests pin its compatibility promise: every untyped body an agent already on a Spark
can send is read as the outcome it always meant, the lifecycle event the Controller
derives from it equals the one derived before the typed contract existed, and a
typed agent that says the same thing produces the same event and the same stored
body.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from vonk_agent_protocol import (
    AgentOperation,
    AgentProtocolError,
    AgentResult,
    FailureCode,
    OutcomeDone,
    OutcomeFailed,
    OutcomeUnknown,
    WaitReason,
)
from vonk_control.agent_jobs import AgentJobService, _safe_retry_failure
from vonk_control.agent_operation_facts import aware, operation_start_deadline
from vonk_control.agent_outcome import agent_outcome, stored_report
from vonk_control.agent_upgrade_status import (
    AGENT_UPGRADE_AWAITING_IDENTITY_PREDECESSOR_REASON,
    AGENT_UPGRADE_AWAITING_IDENTITY_REASON,
)
from vonk_control.lifecycle import Outcome, Reported
from vonk_control.lifecycle.agent_operation import cancel_requested_at

FENCE = str(uuid4())
NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


def _message(state: str, result: dict[str, Any]) -> AgentResult:
    return AgentResult.model_validate_json(
        json.dumps({"fence": FENCE, "state": state, "result": result})
    )


# (operation, state, legacy body, outcome arm, closed code or wait reason)
LEGACY = [
    ("recipe.install", "succeeded", {"installed_bytes": 9}, OutcomeDone, None),
    ("recipe.stop", "succeeded", {}, OutcomeDone, None),
    (
        "recipe.stop",
        "cancelled",
        {"reason": "cancelled", "error_code": "operation_cancelled"},
        OutcomeFailed,
        FailureCode.OPERATION_CANCELLED,
    ),
    (
        "recipe.start",
        "failed",
        {
            "reason": "start failed",
            "status": "failed",
            "error_code": "recipe_start_failed",
            "failure_kind": "temporary-dependency",
            "retry_after_seconds": 5,
        },
        OutcomeFailed,
        FailureCode.RECIPE_START_FAILED,
    ),
    (
        "recipe.start",
        "failed",
        {
            "reason": "unrecognised helper code",
            "status": "failed",
            "error_code": "helper_made_this_up",
            "failure_kind": "invalid-contract",
        },
        OutcomeFailed,
        FailureCode.RECIPE_START_FAILED,
    ),
    (
        "recipe.reconcile",
        "failed",
        {
            "reason": "installation reconciliation is waiting",
            "status": "failed",
            "error_code": "installation_reconciliation_busy",
            "failure_kind": "temporary-dependency",
        },
        OutcomeFailed,
        FailureCode.INSTALLATION_RECONCILIATION_BUSY,
    ),
    (
        "recipe.stop",
        "waiting-for-operator",
        {"reason": "workload stop remains unconfirmed"},
        OutcomeUnknown,
        WaitReason.LEGACY_UNCLASSIFIED,
    ),
    (
        "agent.upgrade.v1",
        "waiting-for-operator",
        {"reason": AGENT_UPGRADE_AWAITING_IDENTITY_REASON},
        OutcomeUnknown,
        WaitReason.UPGRADE_AWAITING_IDENTITY,
    ),
    (
        "agent.upgrade.v1",
        "waiting-for-operator",
        {"reason": AGENT_UPGRADE_AWAITING_IDENTITY_PREDECESSOR_REASON},
        OutcomeUnknown,
        WaitReason.UPGRADE_AWAITING_IDENTITY,
    ),
    (
        "recipe.stop",
        "waiting-for-operator",
        {
            "reason": "agent restarted with an operation in progress",
            "error_code": "agent_restart_interrupted",
            "failure_kind": "uncertain-effect",
            "uncertain": True,
            "operation": "recipe.stop",
        },
        OutcomeUnknown,
        WaitReason.LEGACY_UNCLASSIFIED,
    ),
]


@pytest.mark.parametrize(
    ("operation", "state", "body", "arm", "detail"),
    LEGACY,
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_legacy_body_is_read_as_the_outcome_it_always_meant(
    operation: str, state: str, body: dict[str, Any], arm: type, detail: object
) -> None:
    outcome = agent_outcome(operation, state, _message(state, body).result)

    assert isinstance(outcome, arm)
    if isinstance(outcome, OutcomeFailed):
        assert outcome.code is detail
    if isinstance(outcome, OutcomeUnknown):
        assert outcome.wait_reason is detail


@pytest.mark.parametrize(
    ("operation", "state", "body", "_arm", "_detail"),
    LEGACY,
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_legacy_report_is_stored_exactly_as_it_arrived(
    operation: str, state: str, body: dict[str, Any], _arm: type, _detail: object
) -> None:
    message = _message(state, body)

    stored, _outcome = stored_report(operation, message)

    assert stored is message


def _old_report_event(
    operation: Any, fence: str, parent: Any, state: str, result: dict[str, Any]
) -> Reported:
    """The event derivation before the typed contract, verbatim, as the oracle."""

    if state == "succeeded":
        return Reported(Outcome.DONE, fence=fence)
    if state == "cancelled":
        return Reported(Outcome.CANCELLED, fence=fence)
    raw_reason = result.get("reason")
    reason = (
        f"agent reported: {raw_reason[:200]}"
        if isinstance(raw_reason, str) and raw_reason
        else None
    )
    start_deadline = operation_start_deadline(operation)
    if (
        operation.kind == AgentOperation.RECIPE_START.value
        and start_deadline is not None
        and aware(NOW) >= aware(start_deadline)
    ):
        return Reported(Outcome.FAILED, fence=fence, retryable=False, reason=reason)
    retry_after = result.get("retry_after_seconds")
    due = (
        aware(NOW) + timedelta(seconds=retry_after)
        if type(retry_after) is int
        else None
    )
    if state == "failed":
        requested, _ = cancel_requested_at(parent, NOW)
        return Reported(
            Outcome.FAILED,
            fence=fence,
            retryable=requested is None
            and _safe_retry_failure(operation.kind, state, result),
            retry_after=due,
            reason=reason,
        )
    return Reported(Outcome.UNKNOWN, fence=fence, retry_after=due, reason=reason)


def _rows(kind: str, *, cancelled: bool, spent_budget: bool) -> tuple[Any, Any, Any]:
    payload = (
        {"start_deadline": (NOW - timedelta(seconds=1)).isoformat()}
        if spent_budget
        else {}
    )
    operation = SimpleNamespace(kind=kind, payload=payload)
    attempt = SimpleNamespace(fence=FENCE)
    parent = SimpleNamespace(
        result={"cancel_requested": True, "cancel_requested_at": NOW.isoformat()}
        if cancelled
        else {}
    )
    return operation, attempt, parent


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("spent_budget", [False, True])
@pytest.mark.parametrize(
    ("operation_kind", "state", "body", "_arm", "_detail"),
    LEGACY,
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_lifecycle_event_equals_the_one_derived_before_the_typed_contract(
    operation_kind: str,
    state: str,
    body: dict[str, Any],
    _arm: type,
    _detail: object,
    cancelled: bool,
    spent_budget: bool,
) -> None:
    operation, attempt, parent = _rows(
        operation_kind, cancelled=cancelled, spent_budget=spent_budget
    )
    message = _message(state, body)
    stored, outcome = stored_report(operation_kind, message)
    result = {k: v for k, v in dict(stored.result).items() if v is not None}

    event = AgentJobService._report_event(
        operation, attempt, parent, outcome, result, NOW
    )

    assert event == _old_report_event(operation, FENCE, parent, state, result)


TYPED = [
    (
        "recipe.stop",
        "waiting-for-operator",
        {
            "kind": "unknown",
            "wait_reason": "stop-unconfirmed",
            "reason": "workload stop remains unconfirmed",
            "retry_after_seconds": 9,
        },
    ),
    (
        "recipe.start",
        "failed",
        {
            "kind": "failed",
            "code": "recipe_start_failed",
            "reason": "start failed",
            "failure_kind": "temporary-dependency",
            "retry_after_seconds": 5,
        },
    ),
    (
        "recipe.start",
        "failed",
        {
            "kind": "failed",
            "code": "recipe_start_failed",
            "reason": "start refused",
            "failure_kind": "invalid-authority",
        },
    ),
    (
        "recipe.stop",
        "cancelled",
        {"kind": "failed", "code": "operation_cancelled", "reason": "stopped"},
    ),
    ("recipe.install", "succeeded", {"kind": "done", "result": {"installed_bytes": 9}}),
]


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize(
    ("operation_kind", "state", "body"),
    TYPED,
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_typed_report_derives_the_event_its_stored_legacy_twin_derives(
    operation_kind: str, state: str, body: dict[str, Any], cancelled: bool
) -> None:
    operation, attempt, parent = _rows(
        operation_kind, cancelled=cancelled, spent_budget=False
    )
    typed = _message(state, body)

    stored, outcome = stored_report(operation_kind, typed)

    # The stored message is what a legacy agent would have sent for the same fact,
    # and the legacy adapter reads it back as the same kind of outcome.
    legacy_outcome = agent_outcome(operation_kind, state, stored.result)
    assert type(legacy_outcome) is type(outcome)
    result = {k: v for k, v in dict(stored.result).items() if v is not None}
    typed_event = AgentJobService._report_event(
        operation, attempt, parent, outcome, result, NOW
    )
    legacy_event = AgentJobService._report_event(
        operation, attempt, parent, legacy_outcome, result, NOW
    )
    assert typed_event == legacy_event


def test_a_typed_report_keeps_its_state_word_and_projects_to_the_stored_body() -> None:
    typed = _message(
        "waiting-for-operator",
        {
            "kind": "unknown",
            "wait_reason": "stop-unconfirmed",
            "reason": "workload stop remains unconfirmed",
        },
    )

    stored, outcome = stored_report("recipe.stop", typed)

    assert stored.state == "waiting-for-operator"
    assert dict(stored.result)["wait_reason"] == "stop-unconfirmed"
    assert "kind" not in dict(stored.result)
    assert outcome is typed.result


def test_an_outcome_that_contradicts_its_state_is_refused() -> None:
    with pytest.raises(AgentProtocolError):
        agent_outcome(
            "recipe.stop",
            "failed",
            OutcomeUnknown.model_validate(
                {
                    "kind": "unknown",
                    "wait_reason": "stop-unconfirmed",
                    "reason": "x",
                }
            ),
        )


def test_a_legacy_body_that_is_no_valid_failure_is_refused() -> None:
    with pytest.raises(AgentProtocolError):
        agent_outcome("recipe.stop", "failed", {"unexpected": True})
