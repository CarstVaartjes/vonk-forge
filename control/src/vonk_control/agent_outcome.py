"""The Controller's single reading of an agent's operation result.

Agents that speak the typed contract send an
:class:`~vonk_agent_protocol.OperationOutcome` (``done`` | ``failed`` |
``unknown``) as ``AgentResult.result``.  Agents built before it send the four
legacy state words with an untyped body.  :func:`agent_outcome` is the **one**
function that turns either into an outcome, and the legacy half of it is the
only place the Controller still interprets an untyped body:

* a ``succeeded`` body is the operation's typed success model;
* ``cancelled`` is the definite ``operation_cancelled`` failure;
* ``failed`` is a definite failure (a one-shot job's process receipt keeps its
  receipt);
* ``waiting-for-operator`` is the old agents' word for "I could not confirm the
  effect": an ``unknown`` outcome whatever body it carries.

The legacy half is for one release, so agents already on the Sparks keep
working; delete it (and the legacy wait spelling) once no deployable agent
package predates the typed outcome.  Nothing stored changes: the outcome
projects back to the same stored body and state word every reader already
understands (:func:`stored_report`).
"""

from __future__ import annotations

from pydantic import ValidationError
from vonk_agent_protocol import (
    LEGACY_WAIT_STATE,
    AgentFailureResult,
    AgentOperation,
    AgentProtocolError,
    AgentResult,
    FailureCode,
    OperationOutcome,
    OutcomeDone,
    OutcomeEvidence,
    OutcomeFailed,
    OutcomeKind,
    OutcomeUnknown,
    WaitReason,
    canonical_message,
    outcome_body,
    outcome_state,
    validate_result_for_operation,
)
from vonk_agent_protocol.recipe_jobs import RecipeJobRunResult

from .agent_upgrade_status import AGENT_UPGRADE_AWAITING_IDENTITY_REASONS

_FAILURE_CODE_BY_OPERATION = {
    AgentOperation.AGENT_UPGRADE.value: FailureCode.AGENT_UPGRADE_FAILED,
    AgentOperation.ARTIFACT_DISTRIBUTION.value: FailureCode.ARTIFACT_DISTRIBUTION_FAILED,
    AgentOperation.RECIPE_BUILD.value: FailureCode.RECIPE_BUILD_FAILED,
    AgentOperation.RECIPE_JOB_RUN.value: FailureCode.RECIPE_JOB_RUN_FAILED,
    AgentOperation.RECIPE_INSTALL.value: FailureCode.RECIPE_INSTALL_FAILED,
    AgentOperation.RECIPE_START.value: FailureCode.RECIPE_START_FAILED,
    AgentOperation.RECIPE_STOP.value: FailureCode.RECIPE_STOP_FAILED,
    AgentOperation.RECIPE_UNINSTALL.value: FailureCode.RECIPE_UNINSTALL_FAILED,
}
_KNOWN_FAILURE_CODES = frozenset(code.value for code in FailureCode)
_LEGACY_REASON = "agent reported no reason"


def _evidence(body: AgentFailureResult) -> OutcomeEvidence | None:
    fields = {
        "diagnostics": body.diagnostics,
        "helper_error_code": body.helper_error_code,
        "helper_exit_code": body.helper_exit_code,
        "stage": body.stage,
        "diagnostic": body.diagnostic,
        "package_activation": body.package_activation,
    }
    present = {name: value for name, value in fields.items() if value is not None}
    return OutcomeEvidence(**present) if present else None


def _legacy_wait_reason(body: object) -> WaitReason:
    if isinstance(body, AgentFailureResult):
        if body.wait_reason is not None:
            return body.wait_reason
        if body.reason in AGENT_UPGRADE_AWAITING_IDENTITY_REASONS:
            return WaitReason.UPGRADE_AWAITING_IDENTITY
    return WaitReason.LEGACY_UNCLASSIFIED


def agent_outcome(
    operation_kind: str, state: str, result: object
) -> OutcomeDone | OutcomeFailed | OutcomeUnknown:
    """The outcome an agent reported, from a typed or a legacy result body.

    ``result`` is the parsed ``AgentResult.result`` (a model) or a stored
    mapping.  An invalid body raises :class:`AgentProtocolError`.
    """

    if isinstance(result, (OutcomeDone, OutcomeFailed, OutcomeUnknown)):
        if outcome_state(result) != state:
            raise AgentProtocolError("agent outcome does not match its state")
        return result
    parsed = validate_result_for_operation(operation_kind, result, state=state)
    return _legacy_outcome(operation_kind, state, parsed)


def _legacy_outcome(
    operation_kind: str, state: str, parsed: object
) -> OutcomeDone | OutcomeFailed | OutcomeUnknown:
    try:
        if state == "succeeded":
            return OutcomeDone.model_validate(
                {"kind": OutcomeKind.DONE.value, "result": parsed}
            )
        receipt = parsed if isinstance(parsed, RecipeJobRunResult) else None
        body = parsed if isinstance(parsed, AgentFailureResult) else None
        reason = (
            (body.reason if body is not None else None)
            or (receipt.reason if receipt is not None else None)
            or _LEGACY_REASON
        )
        retry_after = body.retry_after_seconds if body is not None else None
        if state == LEGACY_WAIT_STATE:
            return OutcomeUnknown.model_validate(
                {
                    "kind": OutcomeKind.UNKNOWN.value,
                    "wait_reason": _legacy_wait_reason(parsed).value,
                    "reason": reason,
                    "retry_after_seconds": retry_after,
                    "evidence": None if body is None else _evidence(body),
                    "receipt": receipt,
                }
            )
        if state == "cancelled":
            code = FailureCode.OPERATION_CANCELLED
        elif (
            body is not None
            and body.error_code in _KNOWN_FAILURE_CODES
            and body.error_code != FailureCode.OPERATION_CANCELLED.value
        ):
            code = FailureCode(body.error_code)
        else:
            code = _FAILURE_CODE_BY_OPERATION.get(
                operation_kind, FailureCode.OPERATION_FAILED
            )
        return OutcomeFailed.model_validate(
            {
                "kind": OutcomeKind.FAILED.value,
                "code": code.value,
                "reason": reason,
                "failure_kind": None if body is None else body.failure_kind,
                "retry_after_seconds": retry_after,
                "evidence": None if body is None else _evidence(body),
                "receipt": receipt,
            }
        )
    except ValidationError as error:
        raise AgentProtocolError(
            f"{operation_kind} legacy result cannot be read as an outcome"
        ) from error


def stored_report(
    operation_kind: str, message: AgentResult
) -> tuple[AgentResult, OutcomeDone | OutcomeFailed | OutcomeUnknown]:
    """A report as the Controller stores it, and the outcome it carries.

    A legacy report is returned unchanged.  A typed report keeps its state word
    and projects to the body shape every stored-row reader understands, so the
    persisted rows (and the OpenAPI views over them) do not change.
    """

    outcome = agent_outcome(operation_kind, message.state, message.result)
    if message.result is outcome:
        message = AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": message.fence,
                    "state": message.state,
                    "result": outcome_body(outcome),
                }
            )
        )
    return message, outcome


__all__ = ["OperationOutcome", "agent_outcome", "stored_report"]
