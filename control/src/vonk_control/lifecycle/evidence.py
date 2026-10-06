"""The one rule for damaged or mismatched bookkeeping: rebuild from evidence.

A persisted plan, result, receipt or stored scope that cannot be read, or that
disagrees with what is observed now, is *unknown*, never a reason to refuse a
load (core rule 5).  The owner of the damaged state applies this order and no
other:

1. **Rebuild from evidence.**  Re-derive the value from receipts, children, the
   inventory or the catalog (the ``rebuild`` callable of :func:`read_or_rebuild`).
2. **Retire as unknown.**  When nothing can re-derive it, the caller gets a
   :class:`Residue` (a typed ``unknown`` with a reason code and a note), retires
   the damaged row or skips the damaged element, and continues.  Nothing waits
   for an operator and no load stops on it.

Reconciliation of a *mismatch* (a stored scope that changed, an executor that is
not bound yet) is the same shape: the caller returns :func:`unknown` for the
adapter's ``observe`` and the core retries it, bounded by ``OBSERVE_BUDGET``.

The reason codes live here so the contract models can take them over.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .types import Effect, Observed

_LOG = logging.getLogger(__name__)
_REPORTED: set[tuple[str, str, str]] = set()
_REPORTED_LIMIT = 4096


class BookkeepingReason(StrEnum):
    """Why bookkeeping was treated as unknown instead of refused."""

    #: A stored plan, result, receipt or document does not parse.
    PERSISTED_STATE_DAMAGED = "persisted-state-damaged"
    #: A stored fact no longer matches what is observed (scope, identity, key).
    EVIDENCE_MISMATCH = "evidence-mismatch"
    #: The evidence needed is not available yet (executor, inventory, receipt).
    EVIDENCE_UNAVAILABLE = "evidence-unavailable"
    #: The stored row is incomplete (a group with no members, a missing child).
    ROW_INCOMPLETE = "row-incomplete"


@dataclass(frozen=True, slots=True)
class Residue:
    """A typed ``unknown``: what was damaged, why, and what the caller does next.

    It is a value, not an exception: the caller retires the damaged row or skips
    the element and carries on.
    """

    kind: str
    subject: str
    reason: BookkeepingReason
    note: str = ""

    @property
    def effect(self) -> Effect:
        return Effect.UNKNOWN

    def observed(self) -> Observed:
        return unknown(self.reason, f"{self.kind}:{self.subject}")


@dataclass(frozen=True, slots=True)
class Damaged:
    """What a ``read`` callable *returns* when the stored value does not hold.

    The read of a stored document answers with the value or with ``Damaged``;
    :func:`read_or_rebuild` then rebuilds it from evidence or retires it as
    unknown.  Returning it, not raising, keeps a damaged document out of the
    exception path of every caller (core rule 5).
    """

    note: str


def unknown(reason: BookkeepingReason, detail: str = "") -> Observed:
    """The adapter ``observe`` answer for bookkeeping the core must reconcile."""

    return Observed(Effect.UNKNOWN, f"{reason.value}:{detail}" if detail else reason)


def retire_as_unknown(
    kind: str,
    subject: str,
    reason: BookkeepingReason = BookkeepingReason.PERSISTED_STATE_DAMAGED,
    note: str = "",
) -> Residue:
    """Record the residue of state nothing can re-derive and return it."""

    residue = Residue(kind=kind, subject=subject, reason=reason, note=note)
    # A damaged row is read again on every pass of its worker: it is reported once
    # per process (at WARNING) and then only at DEBUG, so it cannot flood the log.
    key = (kind, subject, reason.value)
    first = key not in _REPORTED
    if first and len(_REPORTED) < _REPORTED_LIMIT:
        _REPORTED.add(key)
    _LOG.log(
        logging.WARNING if first else logging.DEBUG,
        "bookkeeping retired as unknown",
        extra={
            "residue_kind": kind,
            "residue_subject": subject,
            "residue_reason": reason.value,
            "residue_note": note[:200],
        },
    )
    return residue


def read_or_rebuild[T](
    *,
    kind: str,
    subject: str,
    read: Callable[[], T | Damaged],
    rebuild: Callable[[], T | None] | None = None,
    reason: BookkeepingReason = BookkeepingReason.PERSISTED_STATE_DAMAGED,
) -> T | Residue:
    """Read persisted state; rebuild it from evidence; else retire it as unknown.

    ``read`` returns :class:`Damaged` for a document that does not hold; a
    parser it calls may still raise ``TypeError``, ``ValueError`` or ``KeyError``
    (a damaged document); anything else is a defect and propagates.  ``rebuild``
    returns the re-derived value or ``None`` when the evidence does not exist.
    """

    try:
        value = read()
    except (TypeError, ValueError, KeyError) as error:
        note = f"{type(error).__name__}: {error}"
    else:
        if not isinstance(value, Damaged):
            return value
        note = value.note
    if rebuild is not None:
        try:
            rebuilt = rebuild()
        except (TypeError, ValueError, KeyError) as error:
            note = f"{note}; rebuild: {type(error).__name__}: {error}"
        else:
            if rebuilt is not None:
                _LOG.info(
                    "bookkeeping rebuilt from evidence",
                    extra={"residue_kind": kind, "residue_subject": subject},
                )
                return rebuilt
    return retire_as_unknown(kind, subject, reason, note)
