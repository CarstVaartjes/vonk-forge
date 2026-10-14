"""One classifier for what a failure does to its work: :func:`disposition`.

Real security boundaries are terminal (``SECURITY``): authentication and
authorization, identity and certificate expiry, node revocation, enrollment,
signed package metadata, host-helper authority and tombstone fencing, and
credential denial.

A cause that clears by itself (a connection or timeout, a lock or busy owner,
an unavailable dependency, a full disk) is a ``WAIT``: the work stays, says
what it waits for, and spends no retry.  An effect nobody could confirm is
``OBSERVE``d, the only verdict that spends the retry count.  Every other
exception is deterministic (``FAIL``): the same input raises the same error, so
the operation ends failed with :func:`error_detail` as its evidence instead of
looping.  Downloaded bytes that fail their digest are refused and fetched
again (:func:`is_redownload`).

The rows themselves (failure kinds, wait reasons, HTTP statuses) are the shared
contract's :mod:`vonk_agent_protocol.disposition`, the same table the agent and
the CLI read.
"""

from __future__ import annotations

import errno

import httpx2
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import OperationalError as DatabaseOperationalError
from sqlalchemy.exc import TimeoutError as DatabasePoolTimeout
from vonk_agent_protocol import (
    FAILURE_KIND_DISPOSITION,
    SECURITY_REFUSAL_SUFFIXES,
    WAIT_REASON_DISPOSITION,
    AgentFailureKind,
    Disposition,
    SecurityRefusalError,
    UnknownOutcomeError,
    http_status_disposition,
)

from .security_edges import SECURITY_REFUSAL_EDGES

# The codes of a real security refusal, including the agent's `helper_<code>`
# preflight codes, the helper's un-prefixed wire rejection codes, the agent's
# `runtime_helper_<cause>` receipt causes, and the controller/agent identity,
# enrollment and certificate codes, are the closed `SecurityRefusalReason` set
# of the shared contract, each bound to the security edge it guards; this
# module keeps no second copy.
_SECURITY_CODES = frozenset(reason.value for reason in SECURITY_REFUSAL_EDGES)
# Suffixes of the same families, so a new producer of an existing boundary is
# classified without editing the contract enum.
_SECURITY_SUFFIXES = SECURITY_REFUSAL_SUFFIXES
_REDOWNLOAD_SUFFIXES = (".digest_mismatch", ".archive_mismatch")


def _normalized(code: object) -> str:
    return code.strip().casefold() if isinstance(code, str) else ""


def is_security_failure(code: str | None) -> bool:
    """Return true only for a code that names a real security boundary."""

    value = _normalized(code)
    return bool(value) and (
        value in _SECURITY_CODES or value.endswith(_SECURITY_SUFFIXES)
    )


def is_redownload(code: str | None) -> bool:
    """Downloaded bytes failed their digest: discard them and download again."""

    value = _normalized(code)
    return value.endswith(_REDOWNLOAD_SUFFIXES) or value in {
        "digest_mismatch",
        "archive_mismatch",
    }


def _own_code(error: BaseException) -> str | None:
    if isinstance(error, SecurityRefusalError) and error.typed_reason is not None:
        return error.typed_reason.value
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return None


def error_code(error: BaseException) -> str | None:
    """Return an error's explicit code, preserving authenticated typed denials.

    Peer diagnostics are presentation, never authority. Prose cannot promote
    an unknown transport outcome to a terminal security refusal.

    A wrapper keeps the security code of the error it wraps, so re-raising an
    authority failure under a generic phase code cannot make it retryable.
    """

    codes: list[str | None] = []
    current: BaseException | None = error
    while current is not None and len(codes) < 16:
        codes.append(_own_code(current))
        current = current.__cause__
    return next((code for code in codes if is_security_failure(code)), codes[0])


# Local conditions that clear without a different input: space, a busy or
# briefly unreachable device or network, memory pressure.
_TRANSIENT_ERRNOS = frozenset(
    {
        errno.EAGAIN,
        errno.EBUSY,
        errno.EDQUOT,
        errno.EHOSTUNREACH,
        errno.EINTR,
        errno.EIO,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.ENOMEM,
        errno.ENOSPC,
        errno.ESTALE,
        errno.ETIMEDOUT,
    }
)


def _own_disposition(error: BaseException) -> Disposition | None:
    """This error's own verdict, or None when only its cause can tell."""

    # An error family that owns a typed code table states the verdict of each
    # code itself (the model cache refetches bytes that failed their pin).
    declared = getattr(error, "disposition", None)
    if isinstance(declared, Disposition):
        return declared
    if is_security_failure(_own_code(error)) or isinstance(error, SecurityRefusalError):
        return Disposition.SECURITY
    if isinstance(error, UnknownOutcomeError):
        reason = error.typed_reason
        return (
            Disposition.OBSERVE if reason is None else WAIT_REASON_DISPOSITION[reason]
        )
    if isinstance(error, httpx2.HTTPStatusError):
        return http_status_disposition(error.response.status_code)
    if isinstance(error, httpx2.TransportError):
        return (
            None if isinstance(error, httpx2.UnsupportedProtocol) else Disposition.WAIT
        )
    if isinstance(error, (DatabaseOperationalError, DatabasePoolTimeout)):
        return Disposition.WAIT
    if isinstance(error, DBAPIError):
        return Disposition.WAIT if error.connection_invalidated else None
    if isinstance(error, (ConnectionError, TimeoutError, BlockingIOError)):
        return Disposition.WAIT
    if isinstance(error, OSError) and error.errno in _TRANSIENT_ERRNOS:
        return Disposition.WAIT
    if isinstance(error, OSError) and error.errno is None:
        # An operating-system failure that names no condition: its effect is
        # unknown, so it is observed within the bounded count.
        return Disposition.OBSERVE
    return None


def disposition(cause: BaseException | AgentFailureKind | Disposition) -> Disposition:
    """What ``cause`` does to its work; the one classifier.

    An error states its own verdict when it knows it (a ``disposition``
    attribute, a typed wait reason, an HTTP status); otherwise a wrapper raised
    ``from`` a cause takes the cause's verdict, so re-raising a busy owner under
    a phase error keeps it a wait and re-raising an authority failure keeps it
    terminal.  Anything nothing names is deterministic: ``FAIL``.
    """

    if isinstance(cause, Disposition):
        return cause
    if isinstance(cause, AgentFailureKind):
        return FAILURE_KIND_DISPOSITION[cause]
    current: BaseException | None = cause
    for _depth in range(16):
        if current is None:
            break
        verdict = _own_disposition(current)
        if verdict is not None:
            return verdict
        current = current.__cause__
    return Disposition.FAIL


def is_definite(cause: BaseException | AgentFailureKind | Disposition) -> bool:
    """Whether retrying cannot help: ``FAIL`` or ``SECURITY``."""

    return disposition(cause) in {Disposition.FAIL, Disposition.SECURITY}


def error_detail(error: BaseException, *, limit: int = 400) -> str:
    """The cause an operator needs, bounded: a validation error names one
    failing field instead of repeating every alternative of a union."""

    text = str(error)
    if isinstance(error, ValidationError) and error.errors():
        issues = error.errors()
        # A model's own check names the cause; prefer it over a union's
        # alternatives that merely did not match.
        first = next(
            (issue for issue in issues if issue.get("type") == "value_error"),
            issues[0],
        )
        location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
        more = error.error_count() - 1
        text = f"{location}: {first.get('msg', 'invalid')}" + (
            f" (+{more} more)" if more > 0 else ""
        )
    return f"{type(error).__name__}: {text}"[:limit]


__all__ = [
    "disposition",
    "error_code",
    "error_detail",
    "is_definite",
    "is_redownload",
    "is_security_failure",
]
