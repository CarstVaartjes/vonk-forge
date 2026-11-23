"""One persisted wait observation; the owner's SQL due column owns its clock."""

from __future__ import annotations

from typing import Annotated

from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError

from vonk_agent_protocol.lifecycle_vocabulary import (
    OperatorActionName,
    WaitOwnerKind,
    WaitResumeCondition,
)
from vonk_agent_protocol.reason_codes.vocabulary import OperationReason
from vonk_agent_protocol.wire_model import canonical_message

from .operations.blockers import BlockingOperation, OperationBlocker
from .strict_json import StrictModel


class WaitEvidence(StrictModel):
    """Stored with the owner's waiting state, never independently scheduled.

    Retry counts, reads and lease heartbeats do not advance ``last_progress_at``.
    A nullable deadline explicitly means the dependency has no expiry proof;
    expiry always requires reconciliation rather than assuming effects absent.
    """

    owner: BlockingOperation
    owner_kind: WaitOwnerKind
    reason: OperationReason
    blockers: Annotated[list[OperationBlocker], Field(min_length=1)]
    first_blocked_at: AwareDatetime
    #: Required nullable: absence of measured progress is unknown, not now.
    last_progress_at: AwareDatetime | None
    dependency_deadline: AwareDatetime | None = None
    resume_condition: WaitResumeCondition
    operator_action: OperatorActionName | None = None


class QueueWait(WaitEvidence):
    """The same evidence projected with its one canonical scheduling field.

    ``next_check`` is never stored in the evidence JSON; it is read from the
    durable owner's due column in the same snapshot as the evidence.
    """

    next_check: AwareDatetime


_REASON = TypeAdapter(OperationReason)


def typed_wait_reason(value: str | None, fallback: OperationReason) -> OperationReason:
    """Adopt a declared code, never classify the human-readable detail."""
    try:
        return _REASON.validate_json(canonical_message(value))
    except ValidationError:
        return fallback
