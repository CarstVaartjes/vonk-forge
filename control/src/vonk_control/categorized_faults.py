"""Categorized builtin-compatible faults for plumbing that predates the categories.

A lifecycle or operation module may raise only the three error categories of the
contract (``SecurityRefusalError``, ``InvalidRequestError``,
``UnknownOutcomeError``).  Many internal readers and validators raise a builtin
``ValueError``, ``TypeError``, ``KeyError`` or ``InterruptedError`` that a caller
catches by that builtin (``read_or_rebuild`` turns it into a residue).  These
types keep the builtin in their bases, so every ``except ValueError`` keeps
working, and add the category, so the raise is no longer a bare builtin.

* ``StoredState*`` is *unknown*: a persisted document does not parse or lacks a
  key.  The reader rebuilds it from evidence or retires it as unknown; it is
  never a refusal of the request that happened to read it.
* ``Request*Fault`` is *invalid request*: the caller's own argument is wrong.
* ``OperationInterrupted`` is *unknown*: a cooperative stop of a running
  transfer; the owner observes what is on disk and resumes or ends the work.
"""

from __future__ import annotations

from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)

from .request_fault import RequestFault


def security_reason(code: object) -> SecurityRefusalReason | None:
    """The contract reason a raise names by its code, when the code is one."""

    if not isinstance(code, str):
        return None
    try:
        return SecurityRefusalReason(code)
    except ValueError:
        return None


class StoredStateDamaged(UnknownOutcomeError, ValueError):
    """A persisted document does not parse; the reader rebuilds or retires it."""

    def __init__(
        self,
        *args: object,
        reason: WaitReason | None = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


class StoredStateTypeDamaged(UnknownOutcomeError, TypeError):
    """A persisted document has the wrong shape; the reader rebuilds or retires it."""

    def __init__(
        self,
        *args: object,
        reason: WaitReason | None = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


class StoredStateKeyMissing(UnknownOutcomeError, KeyError):
    """A persisted document or index lacks a key; the reader rebuilds or retires it."""

    def __init__(
        self,
        *args: object,
        reason: WaitReason | None = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


class OperationInterrupted(UnknownOutcomeError, InterruptedError):
    """A running transfer was stopped cooperatively; its owner observes and resumes."""

    def __init__(self, *args: object, reason: WaitReason | None = None) -> None:
        super().__init__(*args, reason=reason)


class RequestTypeFault(InvalidRequestError, TypeError):
    """An argument has the wrong type or document shape."""

    def __init__(
        self,
        *args: object,
        reason: InvalidRequestReason | None = InvalidRequestReason.MALFORMED,
        field: str | None = None,
    ) -> None:
        super().__init__(*args, reason=reason, field=field)


class RequestKeyFault(InvalidRequestError, KeyError):
    """An argument names a key that is not there."""

    def __init__(
        self,
        *args: object,
        reason: InvalidRequestReason | None = InvalidRequestReason.NOT_FOUND,
        field: str | None = None,
    ) -> None:
        super().__init__(*args, reason=reason, field=field)


__all__ = [
    "OperationInterrupted",
    "RequestFault",
    "RequestKeyFault",
    "RequestTypeFault",
    "StoredStateDamaged",
    "StoredStateKeyMissing",
    "StoredStateTypeDamaged",
    "security_reason",
]
