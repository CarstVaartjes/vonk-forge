"""A source that keeps serving bytes that fail the pinned digest.

The digest of bytes at ingress is the one edge where the cache refuses: the
first mismatch is retried from byte zero (a truncated or corrupted transfer),
and a second consecutive mismatch of the same pinned file means the source
serves other content than the pin, so the download ends with that evidence
instead of repeating. A fresh exact request has independent accounting, and
any other outcome in between (a success, another failure) starts the count over.
"""

from __future__ import annotations

from enum import StrEnum

from ..model_cache_contract import ModelCacheIngressMismatch

#: Consecutive mismatches of the same file after which the download ends.
INGRESS_MISMATCH_LIMIT = 2


class IngressMismatchStep(StrEnum):
    """What a mismatch leads to: ``RETRY`` (counted) or ``END`` (the limit is met)."""

    RETRY = "retry"
    END = "end"


def observe_ingress_mismatch(
    previous: ModelCacheIngressMismatch | None, *, artifact_key: str, sha256: str
) -> tuple[ModelCacheIngressMismatch, IngressMismatchStep]:
    """Count one more mismatch of ``sha256`` and say what happens next."""
    observations = (
        previous.observations + 1
        if previous is not None
        and previous.artifact_key == artifact_key
        and previous.sha256 == sha256
        else 1
    )
    observation = ModelCacheIngressMismatch(
        artifact_key=artifact_key, sha256=sha256, observations=observations
    )
    return observation, (
        IngressMismatchStep.END
        if observations >= INGRESS_MISMATCH_LIMIT
        else IngressMismatchStep.RETRY
    )


def ingress_mismatch_detail(path: str, observation: ModelCacheIngressMismatch) -> str:
    """The evidence that ends the download: the file, its pin and the count."""
    return (
        f"source serves other bytes than the pin: file {path} failed SHA-256 "
        f"{observation.sha256} on {observation.observations} consecutive downloads"
    )[:512]
