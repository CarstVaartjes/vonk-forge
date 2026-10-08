"""Errors."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urlsplit

from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    ModelCacheCode,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)

from ..artifact_lifecycle import ArtifactLifecycleError
from ..failure_classification import is_security_failure
from ..runtime_init import RuntimeSecretError
from .constants import (
    _MAX_RETRY_HINT_SECONDS,
    _RETRY_BASE_SECONDS,
    _TERMINAL_FAILURE_CODES,
)


class ModelCacheError(RuntimeError):
    """Base error carrying a stable API code and bounded operator detail."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retry_after_seconds: int | None = None,
        recovery: str | None = None,
        source_status: int | None = None,
    ) -> None:
        self.code = code
        #: The provider's HTTP status when the failure is an answer from it.
        self.source_status = source_status
        #: The manifest entry whose transfer raised this, set by its transfer.
        self.failed_artifact_key: str | None = None
        # Coerce before bounding: a caller that passes a sequence would
        # otherwise leave a non-string in place, and the ``str(error)``
        # fallback every failure path uses would then read ``[]``.
        self.detail = str(detail)[:512]
        self.retry_after_seconds = retry_after_seconds
        self.recovery = recovery
        super().__init__(self.detail)


class ModelCacheConflict(ModelCacheError):
    pass


class ModelCacheNotFound(ModelCacheError):
    pass


class ModelCacheResolutionError(ModelCacheError):
    pass


class ModelCacheStorageError(ModelCacheError):
    pass


def _refusal_reason(code: str) -> SecurityRefusalReason | None:
    """The contract reason a raise names by its code, when the code is one."""

    try:
        return SecurityRefusalReason(code)
    except ValueError:
        return None


class _CacheRefusal(SecurityRefusalError, ModelCacheError):
    """A refusal at a security boundary (credentials, source trust, artifact
    identity, a destructive-effect guard): fails closed."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        reason: SecurityRefusalReason | None = None,
        retry_after_seconds: int | None = None,
        recovery: str | None = None,
    ) -> None:
        ModelCacheError.__init__(
            self,
            code,
            detail,
            retry_after_seconds=retry_after_seconds,
            recovery=recovery,
        )
        self.typed_reason = reason if reason is not None else _refusal_reason(code)


class _CacheInvalid(InvalidRequestError, ModelCacheError):
    """A malformed, stale or out-of-contract request: rejected before effects."""

    default_reason: ClassVar[InvalidRequestReason] = InvalidRequestReason.MALFORMED

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        reason: InvalidRequestReason | None = None,
        retry_after_seconds: int | None = None,
        recovery: str | None = None,
    ) -> None:
        ModelCacheError.__init__(
            self,
            code,
            detail,
            retry_after_seconds=retry_after_seconds,
            recovery=recovery,
        )
        self.typed_reason = reason if reason is not None else self.default_reason
        self.typed_field = None


class _CacheUnknown(UnknownOutcomeError, ModelCacheError):
    """A busy owner, an unavailable source or unconfirmed bookkeeping: the worker
    observes and retries; it is never a refusal."""

    default_reason: ClassVar[WaitReason | None] = None

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        reason: WaitReason | None = None,
        retry_after_seconds: int | None = None,
        recovery: str | None = None,
        source_status: int | None = None,
    ) -> None:
        ModelCacheError.__init__(
            self,
            code,
            detail,
            retry_after_seconds=retry_after_seconds,
            recovery=recovery,
            source_status=source_status,
        )
        self.typed_reason = reason if reason is not None else self.default_reason


class ModelCacheConflictRefused(_CacheRefusal, ModelCacheConflict):
    pass


class ModelCacheConflictInvalid(_CacheInvalid, ModelCacheConflict):
    default_reason = InvalidRequestReason.CONFLICT


class ModelCacheConflictUnknown(_CacheUnknown, ModelCacheConflict):
    pass


class ModelCacheNotFoundRefused(_CacheRefusal, ModelCacheNotFound):
    pass


class ModelCacheNotFoundInvalid(_CacheInvalid, ModelCacheNotFound):
    default_reason = InvalidRequestReason.NOT_FOUND


class ModelCacheResolutionRefused(_CacheRefusal, ModelCacheResolutionError):
    pass


class ModelCacheResolutionInvalid(_CacheInvalid, ModelCacheResolutionError):
    pass


class ModelCacheStorageRefused(_CacheRefusal, ModelCacheStorageError):
    pass


class ModelCacheStorageInvalid(_CacheInvalid, ModelCacheStorageError):
    pass


class ModelCacheStorageUnknown(_CacheUnknown, ModelCacheStorageError):
    default_reason = WaitReason.OBSERVATION_UNAVAILABLE


class ModelCacheRemovalOwnerInvalid(UnknownOutcomeError, ArtifactLifecycleError):
    """Incomplete stored owner bookkeeping; its effects remain fenced."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        ArtifactLifecycleError.__init__(self, code, detail, retryable=retryable)
        self.typed_reason = reason
        self.typed_field = None


class ModelCacheDeletionFenceLost(SecurityRefusalError, ArtifactLifecycleError):
    """The destructive-effect fence of an artifact was lost: the removal stops."""

    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        ArtifactLifecycleError.__init__(self, code, detail, retryable=retryable)
        self.typed_reason = None


class ModelCacheCredentialPathUnsafe(SecurityRefusalError, RuntimeSecretError):
    """A credential path that is not a private regular file: never read."""


class _ArtifactWriterBusy(_CacheUnknown):
    """A dependency wait, not a failed transfer or consumed retry."""

    def __init__(self, digest: str) -> None:
        super().__init__(
            ModelCacheCode.OBJECT_BUSY,
            f"waiting for the managed-cache writer of object {digest}; resumes after that writer releases its lock",
            retry_after_seconds=_RETRY_BASE_SECONDS,
            recovery="resume",
        )


def model_cache_failure_is_terminal(code: object) -> bool:
    """Whether a typed model-cache failure waits for a changed credential or request."""

    return code in _TERMINAL_FAILURE_CODES or (
        isinstance(code, str) and is_security_failure(code)
    )


def _retryable_failure(error: BaseException) -> bool:
    """Classify by typed code: only authorization and source policy are terminal."""

    return not model_cache_failure_is_terminal(getattr(error, "code", None))


def _retry_after_seconds(headers: Mapping[str, str], *, now: datetime) -> int | None:
    """Provider retry delays from HTTP headers and rate-limit reset hints."""

    values: list[int] = []
    raw_retry = headers.get("retry-after")
    if raw_retry:
        try:
            if raw_retry.strip().isdigit():
                values.append(max(0, int(raw_retry.strip())))
            else:
                retry_at = datetime.strptime(
                    raw_retry.strip(), "%a, %d %b %Y %H:%M:%S GMT"
                ).replace(tzinfo=UTC)
                values.append(max(0, int((retry_at - now).total_seconds())))
        except (TypeError, ValueError):
            pass
    for name in ("ratelimit-reset", "x-ratelimit-reset"):
        raw_reset = headers.get(name)
        if not raw_reset:
            continue
        try:
            reset = int(raw_reset.strip())
        except (TypeError, ValueError):
            continue
        # Providers use both a delta and a Unix timestamp.  Values near the
        # current epoch are timestamps; small values are delays.
        values.append(
            max(0, reset - int(now.timestamp()))
            if reset > 1_000_000_000
            else max(0, reset)
        )
    raw_rate_limit = headers.get("ratelimit") or headers.get("RateLimit")
    if raw_rate_limit:
        # The IETF RateLimit draft permits policy parameters such as `RateLimit:
        # "resolvers";r=0;t=123`.  The `t` value is a delta in seconds.
        values.extend(
            int(match.group(1))
            for match in re.finditer(
                r"(?:^|;)\s*t\s*=\s*(\d+)\s*(?=;|$)", raw_rate_limit
            )
        )
    return min(max(values), _MAX_RETRY_HINT_SECONDS) if values else None


def _huggingface_access_url(source: str) -> str:
    """Return a safe model page URL without revision or signed query data."""

    parsed = urlsplit(source)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 1 and "resolve" in parts:
        parts = parts[: parts.index("resolve")]
    if len(parts) < 2:
        return "https://huggingface.co/"
    return "https://huggingface.co/" + "/".join(parts[:2])
