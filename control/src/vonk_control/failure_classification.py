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

_SECURITY_CODES = frozenset(
    {
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "permission_denied",
        "agent.enrollment.submit.rejected",
        "agent.identity_mismatch",
        "agent.tombstone_fenced",
        "catalog.authentication_required",
        "controller.authentication_required",
        "controller.fleet.enrollment_denied",
        "distribution.revoked",
        "helper.authorization_invalid",
        "host_helper.authority_denied",
        "local.identity_expired",
        "local.identity_failed",
        "model_cache.credentials_denied",
        "model_cache.credentials_invalid",
        "model_cache.source_access_denied",
        "recipe_update.authority_denied",
        "runtime_image.authorization_invalid",
        "runtime_image.authorization_revoked",
        "tuf.metadata_invalid",
        "tuf.signature_invalid",
    }
)
# Suffixes of the same families, so a new producer of an existing boundary is
# classified without editing this list.
_SECURITY_SUFFIXES = (
    ".authentication_required",
    ".authorization_invalid",
    ".authorization_revoked",
    ".authority_denied",
    ".enrollment_denied",
    ".identity_expired",
    ".node_revoked",
    ".permission_denied",
    ".signature_invalid",
    ".tombstone_fenced",
)
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
