"""Provider waits: a rate limit or quota is its provider's own wait.

Each provider kind of the provider table has its own cooldown: an anonymous
GitHub surface and Hugging Face are limited independently.  The provider opener
(``DownloadMixin._open_provider``) records a hinted rate limit the moment it is
answered, the failure path (``FailureMixin._finish_failed``) records every wait
it schedules; the opener refuses to contact a provider while its cooldown runs;
start-up rebuilds the cooldowns from the durable ``rate_limited`` rows once.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast

from vonk_agent_protocol import ModelCacheCode, OperatorActionName
from vonk_agent_protocol.http_failure import TransientReason

from ..model_cache_contract import (
    ModelCacheDownloadPayload,
    ModelCacheOperationPayload,
)
from ..models import ModelCacheOperation
from .catalog_helpers import _datetime
from .errors import ProviderNotAsked

if TYPE_CHECKING:
    from .service import ModelCacheService


class _ProviderArtifact(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def kind(self) -> str: ...


def provider_kinds(
    artifacts: Iterable[_ProviderArtifact], artifact_key: str | None = None
) -> frozenset[str]:
    """The provider kinds of these artifacts: only ``artifact_key``'s when it
    names one of them (the artifact a provider refused), else every kind."""

    artifacts = tuple(artifacts)
    named = frozenset(item.kind for item in artifacts if item.key == artifact_key)
    return named or frozenset(item.kind for item in artifacts)


def payload_kinds(
    payload: ModelCacheOperationPayload, artifact_key: str | None = None
) -> frozenset[str]:
    """The provider kinds of an operation's artifacts."""

    if not isinstance(payload, ModelCacheDownloadPayload):
        return frozenset()
    return provider_kinds(payload.manifest.artifacts, artifact_key)


def hinted_until(hint: object, now: datetime, *, at_least: int = 1) -> datetime | None:
    """When a provider's own wait hint ends, or ``None`` without a usable hint.

    A provider wait needs a hint of at least a second (``0`` says nothing
    about how long to wait); a retry schedule also takes ``0`` as "now"
    (``at_least=0``)."""

    if type(hint) is not int or hint < at_least:
        return None
    return now + timedelta(seconds=hint)


def provider_wait_end(payload: ModelCacheOperationPayload) -> datetime | None:
    """When the provider wait the operation itself last recorded ends (or ended)."""

    failure = payload.failure
    if (
        failure is None
        or failure.code != TransientReason.RATE_LIMITED
        or failure.retry_time is None
    ):
        return None
    try:
        return _datetime(datetime.fromisoformat(failure.retry_time))
    except ValueError:
        return None


class ProviderWaitsMixin:
    """The per-provider cooldowns of the cache service."""

    def _provider_cooldown(self, kinds: Iterable[str]) -> datetime | None:
        """The latest running cooldown over ``kinds``, else ``None``."""
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        with cache._cooldown_lock:
            running = [
                until
                for kind in kinds
                if (until := cache._provider_cooldowns.get(kind)) is not None
                and until > now
            ]
        return max(running, default=None)

    def _record_provider_cooldown(self, kind: str, until: datetime) -> None:
        cache = cast("ModelCacheService", self)
        with cache._cooldown_lock:
            current = cache._provider_cooldowns.get(kind)
            if current is None or until > current:
                cache._provider_cooldowns[kind] = until

    def _refuse_during_cooldown(self, kind: str) -> None:
        """Never contact a provider that asked to wait; the wait is typed."""
        cache = cast("ModelCacheService", self)
        until = cache._provider_cooldown((kind,))
        if until is None:
            return
        raise ProviderNotAsked(
            ModelCacheCode.RATE_LIMITED,
            "the artifact provider asked to wait; this download resumes automatically",
            retry_after_seconds=max(
                1, math.ceil((until - cache._clock()).total_seconds())
            ),
            recovery=OperatorActionName.RESUME,
        )

    def _adopt_provider_cooldowns(
        self, rows: Sequence[ModelCacheOperation], now: datetime
    ) -> None:
        """Rebuild the cooldowns after a restart from durable provider waits."""
        cache = cast("ModelCacheService", self)
        for operation in rows:
            payload = cache._payload_or_none(operation)
            if payload is None:
                continue  # unreadable: it carries no usable wait evidence
            until = provider_wait_end(payload)
            if until is None or until <= now:
                continue
            assert payload.failure is not None
            for kind in payload_kinds(payload, payload.failure.artifact_key):
                cache._record_provider_cooldown(kind, until)
