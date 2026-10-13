"""One classifier for failures that recovery must not retry past.

Real security boundaries are terminal: authentication and authorization,
identity and certificate expiry, node revocation, enrollment, signed package
metadata, host-helper authority and tombstone fencing, and credential denial.

An exception is retried only when :func:`is_transient` names it: a condition
that clears by itself (a connection or timeout, a lock or busy owner, an
unavailable dependency, an outcome that is observed again).  Every other
exception is deterministic: the same input raises the same error, so the
operation ends failed with that error as its evidence instead of looping.
Downloaded bytes that fail their digest are refused and fetched again, which is
also a retry.
"""

from __future__ import annotations

import errno

import httpx2
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import OperationalError as DatabaseOperationalError
from sqlalchemy.exc import TimeoutError as DatabasePoolTimeout
from vonk_agent_protocol import (
    SECURITY_REFUSAL_SUFFIXES,
    SecurityRefusalError,
    UnknownOutcomeError,
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
# 408 Request Timeout, 425 Too Early, 429 Too Many Requests; 5xx is the peer's.
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429})


def _transient(error: BaseException) -> bool:
    if isinstance(error, UnknownOutcomeError):
        return True
    if isinstance(error, httpx2.HTTPStatusError):
        status = error.response.status_code
        return status in _TRANSIENT_HTTP_STATUSES or status >= 500
    if isinstance(error, httpx2.TransportError):
        return not isinstance(error, httpx2.UnsupportedProtocol)
    if isinstance(error, (DatabaseOperationalError, DatabasePoolTimeout)):
        return True
    if isinstance(error, DBAPIError):
        return bool(error.connection_invalidated)
    if isinstance(error, (ConnectionError, TimeoutError, BlockingIOError)):
        return True
    if isinstance(error, OSError):
        return error.errno in _TRANSIENT_ERRNOS
    return False


def is_transient(error: BaseException) -> bool:
    """Whether retrying the same input can succeed: the explicit allow-list.

    A wrapper raised ``from`` a transient cause stays transient.  Everything
    else (a programming error, a validation error, an identity or plan check, an
    HTTP 4xx contract refusal) is deterministic and must not be retried.
    """

    current: BaseException | None = error
    for _depth in range(16):
        if current is None or is_security_failure(_own_code(current)):
            return False
        if _transient(current):
            return True
        current = current.__cause__
    return False


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
    "error_code",
    "error_detail",
    "is_redownload",
    "is_security_failure",
    "is_transient",
]
