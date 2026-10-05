"""One classifier for failure codes that recovery must not retry past.

Only real security boundaries are terminal: authentication and authorization,
identity and certificate expiry, node revocation, enrollment, signed package
metadata, host-helper authority and tombstone fencing, and credential denial.
Everything else, including stale plans, busy admission, and missing
bookkeeping, is retried with backoff by its owner. Downloaded bytes that fail
their digest are refused and fetched again, which is also a retry.
"""

from __future__ import annotations

import re

from vonk_agent_protocol import SECURITY_REFUSAL_SUFFIXES, SecurityRefusalReason

# The codes of a real security refusal, including the agent's `helper_<code>`
# preflight codes, the helper's un-prefixed wire rejection codes, the agent's
# `runtime_helper_<cause>` receipt causes, and the controller/agent identity,
# enrollment and certificate codes, are the closed `SecurityRefusalReason` set
# of the shared contract; this module keeps no second copy.
_SECURITY_CODES = frozenset(reason.value for reason in SecurityRefusalReason)
# Suffixes of the same families, so a new producer of an existing boundary is
# classified without editing the contract enum.
_SECURITY_SUFFIXES = SECURITY_REFUSAL_SUFFIXES
_REDOWNLOAD_SUFFIXES = (".digest_mismatch", ".archive_mismatch")
_DOTTED_CODE = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+")


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
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    prefix = str(error).partition(":")[0].strip()
    return prefix if _DOTTED_CODE.fullmatch(prefix) else None


def error_code(error: BaseException) -> str | None:
    """Return an error's typed code, or the dotted code prefixing its message.

    A wrapper keeps the security code of the error it wraps, so re-raising an
    authority failure under a generic phase code cannot make it retryable.
    """

    codes: list[str | None] = []
    current: BaseException | None = error
    while current is not None and len(codes) < 16:
        codes.append(_own_code(current))
        current = current.__cause__
    return next((code for code in codes if is_security_failure(code)), codes[0])


__all__ = ["error_code", "is_redownload", "is_security_failure"]
