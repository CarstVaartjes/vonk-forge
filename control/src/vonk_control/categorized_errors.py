"""Raisable error types of the three categories, beside the builtin they replace.

A lifecycle or operation path raises one of the contract's three categories
(:class:`~vonk_agent_protocol.SecurityRefusalError`,
:class:`~vonk_agent_protocol.InvalidRequestError`,
:class:`~vonk_agent_protocol.UnknownOutcomeError`).  A raise that used to be a
bare ``ValueError``, ``TypeError``, ``KeyError`` or ``RuntimeError`` takes the
matching class here, which inherits the builtin beside the category, so no
``except`` clause changes.  Every class takes the closed ``reason`` of its
category as a keyword.
"""

from __future__ import annotations

from vonk_agent_protocol import (
    InvalidRequestError,
    SecurityRefusalError,
    UnknownOutcomeError,
)


class InvalidValue(InvalidRequestError, ValueError):
    """A value outside its contract (malformed, out of range, unsupported)."""


class InvalidType(InvalidRequestError, TypeError):
    """A value of the wrong type for its contract."""


class MissingRecord(InvalidRequestError, KeyError):
    """A named record the request refers to does not exist."""


class UnsettledOutcome(UnknownOutcomeError, RuntimeError):
    """Bookkeeping or evidence that cannot settle the outcome here: the caller
    observes and reconciles instead of refusing."""


class BookkeepingUnknown(UnknownOutcomeError, ValueError):
    """Stored or derived state that does not parse or agree: unknown, to rebuild."""


class SecurityRefused(SecurityRefusalError, RuntimeError):
    """A refusal at a security boundary."""
