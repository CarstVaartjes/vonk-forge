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

from typing import Annotated, ClassVar, Literal

from pydantic import Field

from .build_import import RecipeBuildCleanupEvidence, RecipeBuildEvidence
from .contracts import (
    AgentFailureKind,
    AgentFailureResult,
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


def outcome_body(
    outcome: OutcomeDone | OutcomeFailed | OutcomeUnknown,
) -> OutcomeResult | AgentFailureResult:
    """The stored result body of an outcome, as its typed model.

    This is the shape the Controller persists and every downstream reader
    already understands (an operation's success model, an
    :class:`~vonk_agent_protocol.AgentFailureResult`, or a job receipt), so a
    typed report changes nothing a stored row exposes.
    """

    if isinstance(outcome, OutcomeDone):
        return outcome.result
    if outcome.receipt is not None:
        return outcome.receipt
    evidence = outcome.evidence or OutcomeEvidence()
    if isinstance(outcome, OutcomeUnknown):
        return AgentFailureResult(
            reason=outcome.reason,
            retry_after_seconds=outcome.retry_after_seconds,
            wait_reason=outcome.wait_reason,
            failure_kind=AgentFailureKind.UNCERTAIN_EFFECT,
            uncertain=True,
            diagnostics=evidence.diagnostics,
            package_activation=evidence.package_activation,
            stage=evidence.stage,
            diagnostic=evidence.diagnostic,
            helper_error_code=evidence.helper_error_code,
            helper_exit_code=evidence.helper_exit_code,
        )
    return AgentFailureResult(
        reason=outcome.reason,
        retry_after_seconds=outcome.retry_after_seconds,
        error_code=outcome.code.value,
        failure_kind=outcome.failure_kind,
        status=None if outcome.code is FailureCode.OPERATION_CANCELLED else "failed",
        diagnostics=evidence.diagnostics,
        package_activation=evidence.package_activation,
        stage=evidence.stage,
        diagnostic=evidence.diagnostic,
        helper_error_code=evidence.helper_error_code,
        helper_exit_code=evidence.helper_exit_code,
    )


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


# ------------------------------------------------------------- raisable categories


class CategorizedError(Exception):
    """Base of the three error categories a lifecycle adapter may raise.

    The wire models above say what is *reported*; these exceptions are what is
    *raised* inside the Controller, so a raise names its category by its type:
    :class:`SecurityRefusalError`, :class:`InvalidRequestError` or
    :class:`UnknownOutcomeError`.  An existing error type joins a category by
    inheriting the base *beside* its current one
    (``class AuthError(SecurityRefusalError, ValueError)``), which changes
    no ``except`` clause.  The constructors take the positional arguments of
    :class:`Exception` unchanged and an optional keyword-only closed reason, so a
    subclass keeps its own signature.
    """

    category: ClassVar[ErrorCategory]


class SecurityRefusalError(CategorizedError):
    """A refusal at a security boundary: fails closed, never retried into success."""

    category = ErrorCategory.SECURITY_REFUSAL
    typed_reason: SecurityRefusalReason | None

    def __init__(
        self, *args: object, reason: SecurityRefusalReason | None = None
    ) -> None:
        super().__init__(*args)
        self.typed_reason = reason

    def typed_error(self) -> SecurityRefusal | None:
        """The wire form, or ``None`` while the error names no closed reason."""

        if self.typed_reason is None:
            return None
        return SecurityRefusal(
            category=ErrorCategory.SECURITY_REFUSAL, reason=self.typed_reason
        )


class InvalidRequestError(CategorizedError):
    """A malformed or out-of-contract request: rejected at submit time, before effects."""

    category = ErrorCategory.INVALID_REQUEST
    typed_reason: InvalidRequestReason | None

    def __init__(
        self,
        *args: object,
        reason: InvalidRequestReason | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(*args)
        self.typed_reason = reason
        self.typed_field = field

    def typed_error(self) -> InvalidRequest | None:
        if self.typed_reason is None:
            return None
        return InvalidRequest(
            category=ErrorCategory.INVALID_REQUEST,
            reason=self.typed_reason,
            field=self.typed_field,
        )


class UnknownOutcomeError(CategorizedError):
    """Anything else (bookkeeping, a busy owner, unavailable evidence): observed
    and reconciled by the lifecycle core, never parked and never a refusal."""

    category = ErrorCategory.UNKNOWN
    typed_reason: WaitReason | None

    def __init__(self, *args: object, reason: WaitReason | None = None) -> None:
        super().__init__(*args)
        self.typed_reason = reason

    def typed_error(self) -> UnknownError | None:
        if self.typed_reason is None:
            return None
        return UnknownError(category=ErrorCategory.UNKNOWN, reason=self.typed_reason)


#: The bases a raise in a lifecycle or operation path must derive from.
CATEGORIZED_ERROR_BASES: tuple[type[CategorizedError], ...] = (
    SecurityRefusalError,
    InvalidRequestError,
    UnknownOutcomeError,
)


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
    "CATEGORIZED_ERROR_BASES",
    "DONE_STATE",
    "FAILED_STATE",
    "OUTCOME_ARMS",
    "OUTCOME_KINDS",
    "UNKNOWN_STATE",
    "CategorizedError",
    "ErrorCatalog",
    "InvalidRequest",
    "InvalidRequestError",
    "OperationError",
    "OperationOutcome",
    "OutcomeCatalog",
    "OutcomeDone",
    "OutcomeEvidence",
    "OutcomeFailed",
    "OutcomeResult",
    "OutcomeUnknown",
    "SecurityRefusal",
    "SecurityRefusalError",
    "UnknownError",
    "UnknownOutcomeError",
    "outcome_body",
    "outcome_state",
]
