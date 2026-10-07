"""One operation as the global Activity projection carries it, typed throughout.

Every operation family (agent operations, jobs, Run/Switch, model cache, recipe
update batches, profile applications) projects its durable rows into the same
:class:`OperationItem`.  The item replaces the decoded-JSON mapping the
providers used to pass around: each field is a contract, the failure is a typed
union, and the stored ``result`` of the family is reduced once, at the item's
construction, to the failure-relevant facts every consumer needs
(:class:`OperationResultFacts`) plus, for an agent operation, its validated
receipt.

``OperationRow`` is the transitional producer surface: a family that has not
yet built an :class:`OperationItem` still hands over its decoded document, and
:func:`operation_item` is the one place that document is read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from vonk_agent_protocol import AgentOperation, LifecycleState, OperationProgress
from vonk_agent_protocol.contracts import (
    AgentFailureResult,
    validate_result_for_operation,
)
from vonk_agent_protocol.failure_evidence import FailureDiagnostics
from vonk_agent_protocol.recipe_jobs import RecipeJobRunResult

from . import agent_operation_states
from .fleet_profile_contract import FleetProfileApplicationCancellationView
from .operation_blockers import OperationBlocker, read_blockers
from .operation_contract import (
    AvailabilityOperationFailure,
    OperationEvidenceDownload,
    OperationFailureEvidence,
    sanitize_failure_evidence,
)
from .strict_json import StrictModel, read_stored_model

#: The wire word for an agent operation kind.
AGENT_OPERATION_KINDS = frozenset(operation.value for operation in AgentOperation)

Text = Annotated[str, Field(max_length=4096)]


class OperationOwnerReference(StrictModel):
    """Exact durable owner and original request identity for Activity."""

    kind: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    id: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(
        default=None,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
        max_length=36,
    )


class OperationResultFacts(BaseModel):
    """What any family's stored result says about how the operation failed.

    The result documents differ per family (an agent receipt, a Run/Switch
    result, a stored failure); the facts below are the only part Activity, the
    recovery policy and the failure-evidence bundle read.  Unknown fields are
    ignored: this is a projection of the result, not its contract.
    """

    model_config = ConfigDict(extra="ignore")

    error_code: str | None = None
    code: str | None = None
    reason: Text | None = None
    summary: Text | None = None
    detail: Text | None = None
    diagnostic: Text | None = None
    helper_error_code: Text | None = None
    helper_exit_code: int | None = None
    stage: Text | None = None
    stdout: Text | None = None
    stderr: Text | None = None
    log_excerpt: Text | None = None
    retryable: bool | None = None
    uncertain: bool | None = None
    #: The failure nested in a result that wraps it (``{"failure": {...}}``).
    failure: OperationResultFacts | None = None
    diagnostics: FailureDiagnostics | None = None
    #: The failure this result names, bounded and redacted as stored evidence.
    bounded_failure: OperationFailureEvidence | None = None
    #: The result carried diagnostics that do not satisfy their contract.
    diagnostics_invalid: bool = False

    @model_validator(mode="before")
    @classmethod
    def _tolerate_damaged_parts(cls, value: object) -> object:
        """A damaged optional part reads as absent, never as a damaged result."""

        if not isinstance(value, Mapping):
            return {}
        document = dict(value)
        for name in ("failure", "diagnostics"):
            if document.get(name) is not None and not isinstance(
                document[name], Mapping
            ):
                document[name] = None
                document["diagnostics_invalid"] = name == "diagnostics" or bool(
                    document.get("diagnostics_invalid")
                )
        raw = document.get("diagnostics")
        if raw is not None:
            try:
                document["diagnostics"] = read_stored_model(FailureDiagnostics, raw)
            except (TypeError, ValueError):
                document["diagnostics"] = None
                document["diagnostics_invalid"] = True
        for name in (
            "error_code",
            "code",
            "reason",
            "summary",
            "detail",
            "diagnostic",
            "helper_error_code",
            "stage",
            "stdout",
            "stderr",
            "log_excerpt",
        ):
            if document.get(name) is not None and not isinstance(document[name], str):
                document[name] = str(document[name])
        for name in ("retryable", "uncertain"):
            if not isinstance(document.get(name), bool):
                document[name] = None
        if type(document.get("helper_exit_code")) is not int:
            document["helper_exit_code"] = None
        document["bounded_failure"] = _bounded_failure(value)
        return document

    @classmethod
    def of(cls, value: BaseModel | None) -> OperationResultFacts | None:
        """The facts of a family's typed result model."""

        return (
            None if value is None else cls.model_validate(value.model_dump(mode="json"))
        )

    @property
    def is_uncertain(self) -> bool:
        return self.uncertain is True

    def failure_evidence(self) -> OperationFailureEvidence | None:
        """The bounded, redacted failure this result names, if it names one."""

        return self.bounded_failure


def _bounded_failure(raw: Mapping[str, object]) -> OperationFailureEvidence | None:
    """Reduce a stored result to the bounded failure it names (or none)."""

    candidate = raw.get("failure", raw)
    if not isinstance(candidate, Mapping) or "error_code" not in candidate:
        return None
    try:
        safe = sanitize_failure_evidence(candidate)
        error_code = str(safe["error_code"])
        summary = safe.get("summary") or safe.get("reason") or error_code
        detail = safe.get("detail")
        return OperationFailureEvidence(
            error_code=error_code,
            summary=str(summary)[:256],
            detail=detail if isinstance(detail, str) else None,
            retryable=safe.get("retryable") is True,
            uncertain=safe.get("uncertain") is True,
        )
    except (KeyError, TypeError, ValueError):
        return None


class OperationItem(BaseModel):
    """One operation of any family, as Activity projects it."""

    model_config = ConfigDict(extra="ignore")

    id: str
    job_id: str | None = None
    parent_id: str | None = None
    owner: OperationOwnerReference | None = None
    node_id: str | None = None
    node_ids: list[str] = Field(default_factory=list)
    kind: str
    state: str
    attempt: int
    progress: OperationProgress | None = None
    created_at: str | None = None
    updated_at: str | None = None
    supported_actions: list[str] | None = None
    status_reason: str | None = None
    detail: str | None = None
    blockers: list[OperationBlocker] | None = None
    next_attempt_at: str | None = None
    #: The failure the family itself recorded, by its own contract.
    failure: AvailabilityOperationFailure | OperationFailureEvidence | None = None
    cancellation: FleetProfileApplicationCancellationView | None = None
    evidence_download: OperationEvidenceDownload | None = None
    #: The failure facts of the family's stored result.
    result: OperationResultFacts | None = None
    #: An agent operation's validated failure or process receipt.
    agent_receipt: AgentFailureResult | RecipeJobRunResult | None = None
    #: Why a superseded application ended, and by which successor.
    superseded_by: str | None = None
    reason_code: str | None = None
    #: The stored agent result does not satisfy its contract.
    result_unreadable: bool = False

    @field_validator("node_ids", mode="before")
    @classmethod
    def _node_ids(cls, value: object) -> object:
        return [] if value is None else value

    @field_validator("owner", "cancellation", mode="before")
    @classmethod
    def _tolerate_damaged_bookkeeping(
        cls, value: object, info: ValidationInfo
    ) -> object:
        """One damaged historical owner or receipt reads as absent.

        Activity keeps the operation's durable identity visible; a broken
        bookkeeping detail never turns the page into an unavailable response.
        """

        if value is None or isinstance(
            value, OperationOwnerReference | FleetProfileApplicationCancellationView
        ):
            return value
        model = (
            OperationOwnerReference
            if info.field_name == "owner"
            else FleetProfileApplicationCancellationView
        )
        try:
            return read_stored_model(model, value)
        except (TypeError, ValueError):
            return None

    @field_validator("blockers", mode="before")
    @classmethod
    def _readable_blockers(cls, value: object) -> object:
        """An unreadable blocker is dropped, never fatal (``read_blockers``)."""

        if value is None:
            return None
        if isinstance(value, list) and all(
            isinstance(item, OperationBlocker) for item in value
        ):
            return value
        return read_blockers(value)

    @property
    def failure_recorded(self) -> bool:
        """Whether the family stated its failure (even to say there is none)."""

        return "failure" in self.model_fields_set

    def with_evidence_download(
        self, download: OperationEvidenceDownload
    ) -> OperationItem:
        return self.model_copy(update={"evidence_download": download})


#: A family that has not yet built an :class:`OperationItem` hands over its
#: decoded document.  Remove once every provider returns the model.
type OperationRow = OperationItem | Mapping[str, object]


def agent_receipt_for(
    kind: str, state: str, result: object
) -> tuple[AgentFailureResult | RecipeJobRunResult | None, bool]:
    """Validate an agent operation's failure receipt: ``(receipt, unreadable)``."""

    if kind not in AGENT_OPERATION_KINDS:
        return None, False
    if (
        state not in {LifecycleState.FAILED.value, *agent_operation_states.PARKED}
        or result is None
    ):
        return None, False
    if not isinstance(result, Mapping):
        return None, True
    try:
        parsed = validate_result_for_operation(kind, dict(result), state=state)
    except (ValidationError, ValueError, TypeError):
        return None, True
    if isinstance(parsed, AgentFailureResult | RecipeJobRunResult):
        return parsed, False
    return None, True


def operation_item(row: OperationRow) -> OperationItem:
    """The one read of a provider row: the typed item, or a decoded document."""

    if isinstance(row, OperationItem):
        return row
    document = dict(row)
    kind = document.get("kind")
    state = document.get("state")
    if (
        "agent_receipt" not in document
        and isinstance(kind, str)
        and isinstance(state, str)
    ):
        receipt, unreadable = agent_receipt_for(kind, state, document.get("result"))
        document["agent_receipt"] = receipt
        document["result_unreadable"] = unreadable
    return OperationItem.model_validate(document)
