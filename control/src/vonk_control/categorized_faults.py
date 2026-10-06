"""Categorized builtin-compatible faults beside ``categorized_errors``.

``categorized_errors`` holds the categorized types for a bare ``ValueError``,
``TypeError``, ``KeyError`` and ``RuntimeError``.  These cover the rest of the
builtins a lifecycle or operation path used to raise, each keeping the builtin in
its bases so every ``except`` clause keeps working:

* ``StoredStateTypeDamaged`` and ``StoredStateKeyMissing`` are *unknown*: a
  persisted document has the wrong shape or lacks a key.  The reader rebuilds it
  from evidence or retires it as unknown; it is never a refusal of the request
  that happened to read it.
* ``OperationInterrupted`` is *unknown*: a cooperative stop of a running
  transfer; the owner observes what is on disk and resumes or ends the work.
"""

from __future__ import annotations

from vonk_agent_protocol import SecurityRefusalReason, UnknownOutcomeError, WaitReason


def security_reason(code: object) -> SecurityRefusalReason | None:
    """The contract reason a raise names by its code, when the code is one."""

    if not isinstance(code, str):
        return None
    try:
        return SecurityRefusalReason(code)
    except ValueError:
        return None


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


__all__ = [
    "OperationInterrupted",
    "StoredStateKeyMissing",
    "StoredStateTypeDamaged",
    "security_reason",
]
