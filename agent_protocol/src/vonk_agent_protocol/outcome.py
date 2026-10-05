"""The one outcome envelope of an agent operation result.

An executor ends an operation in exactly one of three ways:

* ``done``: the effect is established and ``result`` is the operation's typed
  success body;
* ``failed``: a *definite* failure, carrying a closed :class:`FailureCode`.  The
  agent proves the failure, so the Controller can end the row (or retry it when
  the ``failure_kind`` says the cause is transient); an agent that cannot
  establish the effect reports ``unknown`` instead.  A cancellation that the
  agent confirmed is the ``operation_cancelled`` code;
* ``unknown``: the executor could not establish whether the effect happened.
  It carries a closed :class:`WaitReason` and typed :class:`OutcomeEvidence`,
  and the Controller observes the effect instead of parking the work.

``AgentResult.result`` carries one of these arms (tagged by ``kind``) for every
agent that speaks this contract.  The untyped bodies of earlier agents remain
valid until the Controller's single legacy adapter
(``vonk_control.agent_outcome``) is removed; they are the only other members of
``AgentResultPayload``.

The error categories at the bottom (:class:`SecurityRefusal`,
:class:`InvalidRequest`, :class:`UnknownError`) are the only things a lifecycle
adapter may raise or report; each carries a reason from its own closed set.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .build_import import RecipeBuildCleanupEvidence, RecipeBuildEvidence
from .contracts import (
    AgentFailureKind,
    AgentInstallResult,
    AgentUpgradeResult,
    ArtifactDistributionResult,
    RecipeStartResult,
)
from .failure_evidence import FailureDiagnostics
from .lifecycle_vocabulary import (
    AgentResultState,
    ErrorCategory,
    FailureCode,
    InvalidRequestReason,
    OutcomeKind,
    SecurityRefusalReason,
    WaitReason,
)
from .package_upgrade import PackageActivationReceipt
from .recipe_jobs import RecipeJobRunResult
from .recipe_operations import (
    RecipeReconcileResult,
    RecipeStopResult,
    RecipeUninstallResult,
)
from .runtime_preflight import RuntimePreflightResult
from .wire_model import WireModel, typed_tag

OutcomeResult = (
    RuntimePreflightResult
    | AgentInstallResult
    | RecipeStartResult
    | RecipeStopResult
    | RecipeReconcileResult
    | RecipeUninstallResult
    | RecipeBuildEvidence
    | RecipeBuildCleanupEvidence
    | RecipeJobRunResult
    | ArtifactDistributionResult
    | AgentUpgradeResult
)


class OutcomeEvidence(WireModel):
    """Typed facts an executor attaches to a failed or unknown outcome.

    Every field is bounded and already sanitized by the agent; the Controller
    sanitizes again at ingress.  ``diagnostics`` holds the bounded diagnostic
    logs, ``helper_error_code``/``helper_exit_code`` the privileged helper's own
    verdict, ``stage``/``diagnostic`` the phase and a one-line cause.
    """

    diagnostics: FailureDiagnostics | None = None
    helper_error_code: str | None = Field(default=None, min_length=1, max_length=128)
    helper_exit_code: int | None = Field(default=None, strict=True, ge=0, le=255)
    stage: str | None = Field(default=None, min_length=1, max_length=128)
    diagnostic: str | None = Field(default=None, min_length=1, max_length=512)
    package_activation: PackageActivationReceipt | None = None


class OutcomeDone(WireModel):
    """The effect is established; ``result`` is the operation's success body."""

    kind: Literal[OutcomeKind.DONE] = typed_tag()
    result: OutcomeResult


class OutcomeFailed(WireModel):
    """A definite failure.

    ``receipt`` is the process receipt of a one-shot recipe job whose process
    ran and exited nonzero; every other failure reports ``code`` and ``reason``.
    """

    kind: Literal[OutcomeKind.FAILED] = typed_tag()
    code: FailureCode
    reason: str = Field(min_length=1, max_length=1024)
    failure_kind: AgentFailureKind | None = None
    retry_after_seconds: int | None = Field(
        default=None, strict=True, ge=0, le=2**32 - 1
    )
    evidence: OutcomeEvidence | None = None
    receipt: RecipeJobRunResult | None = None


class OutcomeUnknown(WireModel):
    """The executor could not establish the effect."""

    kind: Literal[OutcomeKind.UNKNOWN] = typed_tag()
    wait_reason: WaitReason
    reason: str = Field(min_length=1, max_length=1024)
    retry_after_seconds: int | None = Field(
        default=None, strict=True, ge=0, le=2**32 - 1
    )
    evidence: OutcomeEvidence | None = None
    receipt: RecipeJobRunResult | None = None


OperationOutcome = Annotated[
    OutcomeDone | OutcomeFailed | OutcomeUnknown, Field(discriminator="kind")
]
OUTCOME_ARMS = (OutcomeDone, OutcomeFailed, OutcomeUnknown)
OUTCOME_KINDS = frozenset(
    {OutcomeKind.DONE.value, OutcomeKind.FAILED.value, OutcomeKind.UNKNOWN.value}
)

#: ``AgentResult.state`` for each outcome arm.  The wire keeps the four legacy
#: state words; the arm decides which one is truthful.
DONE_STATE = AgentResultState.SUCCEEDED
FAILED_STATE = AgentResultState.FAILED
CANCELLED_STATE = AgentResultState.CANCELLED
UNKNOWN_STATE = AgentResultState.WAITING_FOR_OPERATOR


def outcome_state(
    outcome: OutcomeDone | OutcomeFailed | OutcomeUnknown,
) -> AgentResultState:
    """The wire state word an outcome reports under."""

    if isinstance(outcome, OutcomeDone):
        return DONE_STATE
    if isinstance(outcome, OutcomeUnknown):
        return UNKNOWN_STATE
    return (
        CANCELLED_STATE
        if outcome.code is FailureCode.OPERATION_CANCELLED
        else FAILED_STATE
    )


def _evidence_fields(evidence: OutcomeEvidence | None) -> dict[str, object]:
    if evidence is None:
        return {}
    document: dict[str, object] = {}
    if evidence.stage is not None:
        document["stage"] = evidence.stage
    if evidence.diagnostic is not None:
        document["diagnostic"] = evidence.diagnostic
    if evidence.helper_error_code is not None:
        document["helper_error_code"] = evidence.helper_error_code
    if evidence.helper_exit_code is not None:
        document["helper_exit_code"] = evidence.helper_exit_code
    if evidence.diagnostics is not None:
        document["diagnostics"] = evidence.diagnostics.model_dump(mode="json")
    if evidence.package_activation is not None:
        document["package_activation"] = evidence.package_activation.model_dump(
            mode="json"
        )
    return document


def outcome_body(
    outcome: OutcomeDone | OutcomeFailed | OutcomeUnknown,
) -> dict[str, object]:
    """The stored result body of an outcome.

    This is the shape the Controller persists and every downstream reader
    already understands (an operation's success model, an
    :class:`~vonk_agent_protocol.AgentFailureResult`, or a job receipt), so a
    typed report changes nothing a stored row exposes.
    """

    if isinstance(outcome, OutcomeDone):
        return outcome.result.model_dump(mode="json", exclude_none=True)
    if outcome.receipt is not None:
        return outcome.receipt.model_dump(mode="json", exclude_none=True)
    document: dict[str, object] = {"reason": outcome.reason}
    if outcome.retry_after_seconds is not None:
        document["retry_after_seconds"] = outcome.retry_after_seconds
    document.update(_evidence_fields(outcome.evidence))
    if isinstance(outcome, OutcomeUnknown):
        document["wait_reason"] = outcome.wait_reason.value
        document["failure_kind"] = AgentFailureKind.UNCERTAIN_EFFECT.value
        document["uncertain"] = True
        return document
    document["error_code"] = outcome.code.value
    if outcome.failure_kind is not None:
        document["failure_kind"] = outcome.failure_kind.value
    if outcome.code is not FailureCode.OPERATION_CANCELLED:
        document["status"] = "failed"
    return document


class SecurityRefusal(WireModel):
    """A refused request at a security boundary; it fails closed."""

    category: Literal[ErrorCategory.SECURITY_REFUSAL] = typed_tag()
    reason: SecurityRefusalReason


class InvalidRequest(WireModel):
    """A malformed or out-of-contract request; it fails closed at submit time."""

    category: Literal[ErrorCategory.INVALID_REQUEST] = typed_tag()
    reason: InvalidRequestReason
    field: str | None = Field(default=None, min_length=1, max_length=128)


class UnknownError(WireModel):
    """Anything else: observed and reconciled, never parked."""

    category: Literal[ErrorCategory.UNKNOWN] = typed_tag()
    reason: WaitReason


OperationError = Annotated[
    SecurityRefusal | InvalidRequest | UnknownError, Field(discriminator="category")
]


class OutcomeCatalog(WireModel):
    """Carrier that publishes the outcome union into the wire schema.

    Never sent: it lets the schema exporter and the generators emit the tagged
    union as one ``OperationOutcome``.
    """

    outcome: OperationOutcome


class ErrorCatalog(WireModel):
    """Carrier that publishes the error-category union into every generated surface.

    Never sent: the wire schema, OpenAPI and the TypeScript client all emit the
    tagged union as one ``OperationError``.
    """

    error: OperationError


__all__ = [
    "CANCELLED_STATE",
    "DONE_STATE",
    "FAILED_STATE",
    "OUTCOME_ARMS",
    "OUTCOME_KINDS",
    "UNKNOWN_STATE",
    "ErrorCatalog",
    "InvalidRequest",
    "OperationError",
    "OperationOutcome",
    "OutcomeCatalog",
    "OutcomeDone",
    "OutcomeEvidence",
    "OutcomeFailed",
    "OutcomeResult",
    "OutcomeUnknown",
    "SecurityRefusal",
    "UnknownError",
    "outcome_body",
    "outcome_state",
]
