"""Small, operation-owned recovery decisions for interrupted work.

The caller persists the returned due time and its logical failure count.  A
request-level HTTP retry is deliberately outside this budget.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from collections.abc import Mapping
from vonk_agent_protocol import AgentFailureKind

FailureKind = AgentFailureKind


class RecoveryDecision(StrEnum):
    RETRY = "retry"
    OBSERVE = "observe"
    BLOCK = "block"


def classify(kind: FailureKind) -> RecoveryDecision:
    if kind is FailureKind.TEMPORARY_DEPENDENCY:
        return RecoveryDecision.RETRY
    if kind is FailureKind.UNCERTAIN_EFFECT:
        return RecoveryDecision.OBSERVE
    return RecoveryDecision.BLOCK


def kind_for_agent_error(evidence: Mapping[str, object]) -> FailureKind:
    """Interpret bounded agent evidence; unknown failures fail closed.

    A broad operation-level error code such as ``artifact_distribution_failed``
    cannot prove a transient failure: it also covers integrity and custody
    failures. Exact producers provide ``failure_kind`` for the precise boundary.
    """
    raw_kind = evidence.get("failure_kind")
    if isinstance(raw_kind, str):
        try:
            return FailureKind(raw_kind)
        except ValueError:
            return FailureKind.INVALID_CONTRACT
    if evidence.get("uncertain") is True or evidence.get("error_code") == "operation_outcome_uncertain":
        return FailureKind.UNCERTAIN_EFFECT
    if evidence.get("error_code") == "agent_lease_expired":
        return FailureKind.UNCERTAIN_EFFECT
    return FailureKind.INVALID_CONTRACT


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    max_failures: int = 5
    base_delay_seconds: int = 2
    max_delay_seconds: int = 60

    def next_attempt(
        self,
        operation_id: str,
        failed_attempts: int,
        now: datetime,
        retry_after: datetime | None = None,
    ) -> datetime | None:
        """Return a stable, bounded retry time or ``None`` when exhausted.

        ``failed_attempts`` includes the failure just observed.  Stable jitter
        prevents worker restarts from shifting the persisted schedule.
        ``retry_after`` is a lower bound supplied by the remote dependency.
        """
        if not operation_id or failed_attempts < 1:
            raise ValueError("operation ID and positive failure count are required")
        if self.max_failures < 1 or self.base_delay_seconds < 1 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("invalid recovery policy")
        if failed_attempts >= self.max_failures:
            return None
        if now.tzinfo is None or (retry_after is not None and retry_after.tzinfo is None):
            raise ValueError("recovery times must be timezone aware")
        cap = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** min(failed_attempts - 1, 30)),
        )
        lower = max(1, cap * 3 // 4)
        upper = max(lower, min(self.max_delay_seconds, cap * 5 // 4))
        entropy = int.from_bytes(
            hashlib.sha256(f"{operation_id}:{failed_attempts}".encode()).digest()[:8],
            "big",
        )
        scheduled = now.astimezone(timezone.utc) + timedelta(seconds=lower + entropy % (upper - lower + 1))
        if retry_after is not None:
            scheduled = max(scheduled, retry_after.astimezone(timezone.utc))
        return scheduled
