"""The one retry clock and the one failure limit for interrupted work.

The lifecycle core (``lifecycle/core.py``) owns the retry count of every
lifecycle row.  An owner outside the core (route publication, distributed
recovery) schedules through :func:`schedule`, which reads the failure's
:class:`~vonk_agent_protocol.Disposition`: a ``WAIT`` keeps the count, a
``FAIL`` ends at once, only an ``OBSERVE`` spends a retry.  A request-level
HTTP retry is deliberately outside this budget.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from vonk_agent_protocol import AgentFailureKind, Disposition, FailureCode

FailureKind = AgentFailureKind


def kind_for_failure_fields(
    failure_kind: str | None, error_code: str | None, uncertain: bool = False
) -> FailureKind:
    """The same failure decision for already validated typed observations."""
    raw_kind = failure_kind
    if isinstance(raw_kind, str):
        try:
            return FailureKind(raw_kind)
        except ValueError:
            return FailureKind.INVALID_CONTRACT
    if error_code == FailureCode.RETAINED_CONTAINER_FOREIGN.value:
        # Another party's container holds the name the start needs and was left
        # untouched: a prerequisite that clears when it is gone, never a broken
        # contract.  The agent states the kind itself; this keeps a body that
        # carries only the code from ending the load.
        return FailureKind.RESOURCE_PREREQUISITE
    if uncertain is True or error_code == "operation_outcome_uncertain":
        return FailureKind.UNCERTAIN_EFFECT
    if error_code == "agent_lease_expired":
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
        *,
        ongoing_intent: bool = False,
    ) -> datetime | None:
        """Return a stable, bounded retry time or ``None`` when exhausted.

        ``ongoing_intent=True`` bounds the retry rate instead of the lifetime of
        authorized intent. Use it only when the owner reconciles exact effects.

        ``failed_attempts`` includes the failure just observed.  Stable jitter
        prevents worker restarts from shifting the persisted schedule.
        ``retry_after`` is a lower bound supplied by the remote dependency.
        """
        if not operation_id or failed_attempts < 1:
            raise ValueError("operation ID and positive failure count are required")
        if (
            self.max_failures < 1
            or self.base_delay_seconds < 1
            or self.max_delay_seconds < self.base_delay_seconds
        ):
            raise ValueError("invalid recovery policy")
        if not ongoing_intent and failed_attempts >= self.max_failures:
            return None
        if now.tzinfo is None or (
            retry_after is not None and retry_after.tzinfo is None
        ):
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
        scheduled = now.astimezone(UTC) + timedelta(
            seconds=lower + entropy % (upper - lower + 1)
        )
        if retry_after is not None:
            scheduled = max(scheduled, retry_after.astimezone(UTC))
        return scheduled

    def exhausted(self, failures: int) -> bool:
        """Whether ``failures`` recorded failures spend this policy's limit."""

        return failures >= self.max_failures


#: The backoff step a wait is re-checked at: prompt enough that a cleared
#: dependency resumes within seconds, never a tight loop, and independent of
#: any failure count (a wait spends none).
WAIT_STEP = 2

#: The shared default: every lifecycle kind and every owner without its own rate.
RECOVERY = RecoveryPolicy()
#: Route publication and route reconciliation: a prompt first retry, so a
#: momentary supervisor hiccup does not delay a ready run, capped at a minute.
ROUTE_RECOVERY = RecoveryPolicy(
    max_failures=6, base_delay_seconds=5, max_delay_seconds=60
)
#: Distributed run recovery: inspected promptly, then backed off to a five
#: minute cooldown after repeated issued recoveries.
DISTRIBUTED_RECOVERY = RecoveryPolicy(
    max_failures=5, base_delay_seconds=5, max_delay_seconds=300
)


@dataclass(frozen=True, slots=True)
class Scheduled:
    """An owner's next try: the failure count to persist and when to try again.

    ``next_at`` is ``None`` when the work ends: a deterministic or security
    failure on its first occurrence, or an uncertain effect that recurred past
    the policy's limit.
    """

    failures: int
    next_at: datetime | None


def schedule(
    policy: RecoveryPolicy,
    operation_id: str,
    failures: int,
    verdict: Disposition,
    now: datetime,
    *,
    retry_after: datetime | None = None,
) -> Scheduled:
    """The one retry decision for an owner outside the lifecycle core.

    ``failures`` is the count already recorded.  A ``WAIT`` never spends one and
    never ends the work; only an ``OBSERVE`` spends one.
    """

    if verdict in {Disposition.FAIL, Disposition.SECURITY}:
        return Scheduled(failures, None)
    if verdict is Disposition.WAIT:
        return Scheduled(
            failures,
            policy.next_attempt(
                operation_id,
                WAIT_STEP,
                now,
                retry_after=retry_after,
                ongoing_intent=True,
            ),
        )
    spent = failures + 1
    return Scheduled(
        spent, policy.next_attempt(operation_id, spent, now, retry_after=retry_after)
    )
