"""The outcome envelope and the lifecycle vocabulary are one closed contract."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from vonk_agent_protocol import (
    AgentFailureResult,
    AgentOperation,
    AgentProtocolError,
    AgentResult,
    BlockerCategory,
    CategorizedError,
    ErrorCategory,
    FailureCode,
    InvalidRequest,
    InvalidRequestError,
    InvalidRequestReason,
    OperationError,
    OperationOutcome,
    OutcomeDone,
    OutcomeFailed,
    OutcomeKind,
    OutcomeUnknown,
    SecurityRefusal,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownError,
    UnknownOutcomeError,
    WaitReason,
    error_category_of,
    outcome_body,
    outcome_state,
    validate_result_for_operation,
)

FENCE = str(uuid4())


def _report(state: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"fence": FENCE, "state": state, "result": result}


UNKNOWN = {
    "kind": "unknown",
    "wait_reason": "stop-unconfirmed",
    "reason": "workload stop remains unconfirmed",
}
CANCELLED = {
    "kind": "failed",
    "code": "operation_cancelled",
    "reason": "controller cancellation confirmed after exact workload stop",
}
FAILED = {
    "kind": "failed",
    "code": "recipe_start_failed",
    "reason": "container runtime could not start the workload",
    "failure_kind": "temporary-dependency",
    "retry_after_seconds": 5,
    "evidence": {"stage": "start", "helper_error_code": "operation_io"},
}
DONE = {"kind": "done", "result": {"installed_bytes": 3}}


@pytest.mark.parametrize(
    ("state", "document", "arm"),
    [
        ("succeeded", DONE, OutcomeDone),
        ("failed", FAILED, OutcomeFailed),
        ("cancelled", CANCELLED, OutcomeFailed),
        ("waiting-for-operator", UNKNOWN, OutcomeUnknown),
    ],
)
def test_every_outcome_arm_round_trips_through_the_wire_result(
    state: str, document: dict[str, Any], arm: type
) -> None:
    message = AgentResult.parse(_report(state, document))

    assert isinstance(message.result, arm)
    assert outcome_state(message.result) == state
    again = AgentResult.model_validate_json(message.model_dump_json())
    assert again == message


@pytest.mark.parametrize(
    ("state", "document"),
    [
        ("failed", UNKNOWN),
        ("succeeded", UNKNOWN),
        ("waiting-for-operator", CANCELLED),
        ("failed", CANCELLED),
        ("cancelled", FAILED),
        ("failed", DONE),
    ],
)
def test_a_typed_outcome_cannot_disagree_with_its_state_word(
    state: str, document: dict[str, Any]
) -> None:
    with pytest.raises(AgentProtocolError):
        AgentResult.parse(_report(state, document))


def test_the_tag_decides_the_arm_and_an_untagged_body_is_legacy() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(OperationOutcome)
    assert isinstance(adapter.validate_python(UNKNOWN), OutcomeUnknown)
    # A body that matches two arms in shape still resolves by its tag only.
    both = {**UNKNOWN, "code": "operation_failed"}
    with pytest.raises(ValidationError):
        adapter.validate_python(both)
    legacy = AgentResult.parse(_report("waiting-for-operator", {"reason": "legacy"}))
    assert isinstance(legacy.result, AgentFailureResult)


@pytest.mark.parametrize(
    "changes",
    [
        {"code": "made_up_code"},
        {"kind": "cancelled"},
        {"reason": ""},
        {"unexpected": True},
        {"failure_kind": "invented"},
    ],
)
def test_a_failed_outcome_is_closed(changes: dict[str, Any]) -> None:
    with pytest.raises(AgentProtocolError):
        AgentResult.parse(_report("failed", {**FAILED, **changes}))


def test_an_unknown_outcome_needs_a_typed_wait_reason() -> None:
    for changes in ({"wait_reason": "operator-should-look"}, {"wait_reason": None}):
        with pytest.raises(AgentProtocolError):
            AgentResult.parse(_report("waiting-for-operator", {**UNKNOWN, **changes}))
    document = {k: v for k, v in UNKNOWN.items() if k != "wait_reason"}
    with pytest.raises(AgentProtocolError):
        AgentResult.parse(_report("waiting-for-operator", document))


def test_the_stored_body_is_the_shape_every_reader_already_understands() -> None:
    unknown = AgentResult.parse(_report("waiting-for-operator", UNKNOWN))
    body = outcome_body(unknown.result)
    assert body["reason"] == UNKNOWN["reason"]
    assert body["wait_reason"] == "stop-unconfirmed"
    assert body["failure_kind"] == "uncertain-effect"
    # It validates as the stored legacy body of the operation.
    validate_result_for_operation(
        AgentOperation.RECIPE_STOP, body, state="waiting-for-operator"
    )

    cancelled = AgentResult.parse(_report("cancelled", CANCELLED))
    body = outcome_body(cancelled.result)
    assert body["error_code"] == "operation_cancelled"
    assert body.status is None
    validate_result_for_operation(AgentOperation.RECIPE_START, body, state="cancelled")

    failed = AgentResult.parse(_report("failed", FAILED))
    body = outcome_body(failed.result)
    assert body["status"] == "failed"
    assert body["error_code"] == "recipe_start_failed"
    assert body["helper_error_code"] == "operation_io"
    validate_result_for_operation(AgentOperation.RECIPE_START, body, state="failed")

    done = AgentResult.parse(_report("succeeded", DONE))
    assert outcome_body(done.result).model_dump(exclude_none=True) == {
        "installed_bytes": 3
    }


def test_a_typed_done_is_checked_against_the_operation_that_ran() -> None:
    validate_result_for_operation(
        AgentOperation.RECIPE_INSTALL, DONE, state="succeeded"
    )
    with pytest.raises(AgentProtocolError):
        validate_result_for_operation(
            AgentOperation.RECIPE_START, DONE, state="succeeded"
        )


def test_result_bodies_still_refuse_filesystem_paths() -> None:
    with pytest.raises(AgentProtocolError):
        AgentResult.parse(
            _report("waiting-for-operator", {**UNKNOWN, "reason": "stuck at /etc/x"})
        )


def test_the_error_categories_are_closed_per_kind() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(OperationError)
    refusal = adapter.validate_python(
        {"category": "security-refusal", "reason": "grant_invalid"}
    )
    assert isinstance(refusal, SecurityRefusal)
    invalid = adapter.validate_python(
        {"category": "invalid-request", "reason": "malformed", "field": "timeout"}
    )
    assert isinstance(invalid, InvalidRequest)
    unknown = adapter.validate_python({"category": "unknown", "reason": "lease-lapsed"})
    assert isinstance(unknown, UnknownError)

    # A reason of one kind is not a reason of another.
    for document in (
        {"category": "security-refusal", "reason": "malformed"},
        {"category": "invalid-request", "reason": "grant_invalid"},
        {"category": "unknown", "reason": "malformed"},
        {"category": "unknown", "reason": "grant_invalid"},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python(document)


def test_the_vocabulary_sets_do_not_overlap_where_a_word_would_be_ambiguous() -> None:
    sets = {
        "wait": {reason.value for reason in WaitReason},
        "invalid": {reason.value for reason in InvalidRequestReason},
        "security": {reason.value for reason in SecurityRefusalReason},
        "failure": {code.value for code in FailureCode},
    }
    names = list(sets)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            assert not sets[left] & sets[right], (left, right)


def test_blocker_categories_map_onto_the_three_error_categories() -> None:
    assert {error_category_of(category) for category in BlockerCategory} == set(
        ErrorCategory
    )
    assert error_category_of("security-edge") is ErrorCategory.SECURITY_REFUSAL
    assert error_category_of("input-validation") is ErrorCategory.INVALID_REQUEST
    assert error_category_of("bookkeeping-debt") is ErrorCategory.UNKNOWN


def test_the_outcome_kinds_cover_the_three_arms_and_a_cancel() -> None:
    assert {kind.value for kind in OutcomeKind} == {
        "done",
        "failed",
        "cancelled",
        "unknown",
    }
    assert json.loads(OutcomeDone.model_validate(DONE).model_dump_json())["kind"] == (
        "done"
    )


class _Legacy(ValueError):
    """An existing error type with its own constructor and base."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class _LegacyRefusal(SecurityRefusalError, _Legacy): ...


def test_each_raisable_category_names_its_error_category() -> None:
    assert SecurityRefusalError.category is ErrorCategory.SECURITY_REFUSAL
    assert InvalidRequestError.category is ErrorCategory.INVALID_REQUEST
    assert UnknownOutcomeError.category is ErrorCategory.UNKNOWN
    for base in (SecurityRefusalError, InvalidRequestError, UnknownOutcomeError):
        assert issubclass(base, CategorizedError)


def test_a_raisable_category_wraps_the_wire_error_of_its_reason() -> None:
    refusal = SecurityRefusalError(
        "token is invalid", reason=SecurityRefusalReason.GRANT_INVALID
    )
    assert str(refusal) == "token is invalid"
    assert refusal.typed_error() == SecurityRefusal(
        category=ErrorCategory.SECURITY_REFUSAL,
        reason=SecurityRefusalReason.GRANT_INVALID,
    )
    invalid = InvalidRequestError(
        "limit is invalid", reason=InvalidRequestReason.OUT_OF_RANGE, field="limit"
    )
    assert invalid.typed_error() == InvalidRequest(
        category=ErrorCategory.INVALID_REQUEST,
        reason=InvalidRequestReason.OUT_OF_RANGE,
        field="limit",
    )
    unknown = UnknownOutcomeError(reason=WaitReason.RECEIPT_MISSING)
    assert unknown.typed_error() == UnknownError(
        category=ErrorCategory.UNKNOWN, reason=WaitReason.RECEIPT_MISSING
    )
    for bare in (
        SecurityRefusalError("x"),
        InvalidRequestError("x"),
        UnknownOutcomeError("x"),
    ):
        assert bare.typed_error() is None  # no closed reason named yet


def test_an_existing_error_type_joins_a_category_without_changing_its_handlers() -> (
    None
):
    error = _LegacyRefusal("x.bad", "nope")
    assert isinstance(error, _Legacy) and isinstance(error, ValueError)
    assert isinstance(error, SecurityRefusalError)
    assert str(error) == "x.bad: nope" and error.code == "x.bad"
    with pytest.raises(ValueError, match="x.bad"):
        raise error
