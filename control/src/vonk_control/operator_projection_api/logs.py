"""Operator projection api: logs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    ObservationCause,
)

from .. import agent_operation_states
from ..failure_evidence import (
    AttemptPhase,
    FailedAttempt,
    FailureEvidenceBundle,
    collect_failure,
    failed_attempt_condition,
)
from ..logging import redact_text
from ..models import AgentOperation, AgentOperationAttempt
from ..operation_item_contract import OperationResultFacts
from .contracts import FleetLogEntry, FleetLogResponse, LogLevel, LogSource

#: How far back the log projection narrates failed agent attempts by default.
_AGENT_LOG_LOOKBACK = timedelta(days=14)

#: A fixed number of Controller job-log blobs per query, matching the previous
#: bounded read.
#: A fixed number of failed agent attempts per query.
_AGENT_LOG_SCAN_LIMIT = 128

#: A hard ceiling on projected agent entries before the caller's ``lines`` cut.
_AGENT_LOG_ENTRY_LIMIT = 4_096

#: Which attempts an operator must be able to read back is owned by
#: ``failed_attempt_condition`` in the failure-evidence module, so this
#: projection and the diagnostics download cannot disagree about it.
#: The headline and level one attempt state narrates.  A lapse and a wait are
#: things an operator must act on, not errors that claim the start died.
_ATTEMPT_OUTCOME: Mapping[str, tuple[str, LogLevel]] = {
    "failed": ("failed", "error"),
    ObservationCause.LEASE_LAPSED.value: ("lease expired", "warning"),
    ObservationCause.REPORTED_UNKNOWN.value: ("waiting for operator", "warning"),
}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _agent_log_source(kind: str) -> LogSource:
    """Map an agent operation kind onto the log source an operator already uses.

    Every agent-executed operation narrates the runtime path except a recipe job
    run, which owns the ``job`` source.  ``client`` and ``monitor`` have no
    producer in this build, so a query for them reports absence truthfully
    instead of claiming an empty retained store.
    """
    return "job" if kind == "recipe.job.run.v1" else "runtime"


def _phase_start_deadline(operation: AgentOperation) -> str | None:
    """Return the immutable start deadline a two-phase start bound, if any.

    Only a distributed start persists one, and it is the immutable budget the
    start may not outlive.  It is reported verbatim: the repository preserves a
    formatted timestamp's spelling rather than rewriting it.
    """

    payload = operation.payload if isinstance(operation.payload, Mapping) else {}
    value = payload.get("start_deadline")
    return value if isinstance(value, str) and value else None


def _attempt_is_parked(
    operation: AgentOperation, attempt: AgentOperationAttempt
) -> bool:
    """Return whether this exact attempt is why the order is waiting.

    The park sets the operation's state and reason, and the attempt keeps no
    timestamp of its own, so a lapse can only be dated while its attempt is
    still the current one.
    """

    return (
        agent_operation_states.order_is_parked(operation.state)
        and attempt.attempt == operation.current_attempt
    )


def _lease_clock(
    operation: AgentOperation, attempt: AgentOperationAttempt
) -> str | None:
    """Name the clock that lapsed and the numbers that bound it.

    A lease lapse is the Controller's own outcome, so the projection reports the
    Controller's facts: which clock stopped authorising renewal, the deadline it
    stopped renewing before, the instant the lapse was recorded, how far into the
    operation that instant was, and the separate start deadline that had not
    elapsed.  The requested lease window is deliberately not among them, and no
    existing field dates the grant: the attempt row keeps no timestamp, and the
    node's ``last_seen_at`` dates its last accepted *contact* -- which may be a
    claim or a result, not the renewal that set this deadline -- so subtracting
    it yields a lower bound rather than the window.  A missing number is
    acceptable here; an invented one is not.
    """

    if not agent_operation_states.attempt_lapsed(attempt):
        return None
    parts = [
        "clock=operation-lease",
        f"lease_deadline={_aware(attempt.lease_deadline).isoformat()}",
    ]
    if _attempt_is_parked(operation, attempt):
        expired_at = _aware(operation.updated_at)
        parts.append(f"expired_at={expired_at.isoformat()}")
        parts.append(
            "elapsed_seconds="
            f"{int((expired_at - _aware(operation.created_at)).total_seconds())}"
        )
    start_deadline = _phase_start_deadline(operation)
    if start_deadline is not None:
        parts.append(f"start_deadline={start_deadline}")
    return " ".join(parts)


def _failure_log_entries(
    *,
    operation_id: str,
    kind: str,
    source: LogSource,
    bundle: FailureEvidenceBundle,
    observed_at: datetime,
    state: str,
    clock: str | None,
    controller_reason: str | None,
    has_receipt: bool,
) -> list[FleetLogEntry]:
    """Project one bounded attempt narrative into retrievable log entries.

    The agent's own reason already names the stable refusal code for the
    protocol causes, whose ``diagnostic()`` is deliberately ``None``; the entry
    records that reason and the operation error code rather than any unbounded
    or unredacted payload.  An attempt that stopped renewing is the Controller's
    wait rather than an agent refusal, so it is narrated as a wait, it names the
    clock that lapsed with its numbers, and it claims no agent error code it
    never received.
    """

    entries: list[FleetLogEntry] = []
    headline, default_level = _ATTEMPT_OUTCOME.get(state, _ATTEMPT_OUTCOME["failed"])

    def add(message: str, level: LogLevel | None = None) -> None:
        text = redact_text(message)[:4_096]
        if text:
            entries.append(
                FleetLogEntry(
                    observed_at=observed_at,
                    source=source,
                    level=level or default_level,
                    message=text,
                    evidence_id=operation_id,
                )
            )

    add(f"{kind} {headline}: {bundle.summary}")
    # An error code is an agent's own stable refusal.  A lapsed lease came with
    # no receipt at all, so the default "operation_failed" would name a refusal
    # that never happened.
    if has_receipt:
        add(f"error_code={bundle.error_code}")
    if bundle.detail:
        add(f"detail={bundle.detail}")
    # The Controller's own record of the wait, which the agent's receipt cannot
    # carry: it is what says the effect is unobserved rather than dead.
    if controller_reason is not None and controller_reason != bundle.summary:
        add(f"controller: {controller_reason}", level="warning")
    if clock is not None:
        add(f"wait: {clock}", level="warning")
    for line in bundle.diagnostics.stderr.text.splitlines():
        add(f"stderr: {line}")
    for line in bundle.diagnostics.stdout.text.splitlines():
        add(f"stdout: {line}")
    # The refusing rule and its measured bound, e.g. "rule=… limit=4096
    # observed=518", so an operator sees how far over the request was.
    for property in bundle.diagnostics.preflight:
        add(f"preflight: {property.name}={property.value}", level="warning")
    for item in bundle.diagnostics.collector_errors:
        add(f"collector={item}", level="warning")
    for item in bundle.collector_errors:
        add(f"evidence-collector={item}", level="warning")
    return entries


class AgentFailureLogProvider:
    """Project retained, redacted agent failure evidence for one Spark.

    The agent operation attempts keep the agent's own bounded failure result -- its reason
    (which names the stable refusal code), its operation error code and its
    sanitized process-log tails -- which is the narrative a failed
    ``recipe.start`` never reached the log surface with.  An attempt whose lease
    lapsed left no result at all, so the Controller's own record of the wait and
    the clock that lapsed are projected in its place.  This is not a live
    stream, nothing is written here, and it never becomes an authority for
    anything.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Any | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))

    def list(
        self,
        node_id: str,
        *,
        since: datetime | None,
        lines: int,
        recipe: str | None,
        source: str | None,
        follow: bool,
    ) -> FleetLogResponse:
        entries: list[FleetLogEntry] = []
        retained = False
        if source in (None, "job", "runtime"):
            entries.extend(self._agent_failure_entries(node_id, since=since))
            retained = True
        entries.sort(key=lambda item: item.observed_at, reverse=True)
        return FleetLogResponse(
            node_id=node_id,
            since=since,
            lines=lines,
            entries=entries[:lines],
            # ``retained`` names whether a durable store was actually consulted
            # for this query.  A source with no producer reports False rather
            # than an empty retained store.
            retained=retained,
            # Both projected stores are retained evidence, not live streams.
            follow=False,
        )

    def _agent_failure_entries(
        self, node_id: str, *, since: datetime | None
    ) -> list[FleetLogEntry]:
        now = _aware(self._clock())
        cutoff = _aware(since) if since is not None else now - _AGENT_LOG_LOOKBACK
        with self._sessions() as session:
            rows = list(
                session.execute(
                    select(AgentOperation, AgentOperationAttempt)
                    .join(
                        AgentOperationAttempt,
                        AgentOperationAttempt.operation_id == AgentOperation.id,
                    )
                    .where(
                        AgentOperation.node_id == node_id,
                        failed_attempt_condition(AgentOperation, AgentOperationAttempt),
                        AgentOperation.updated_at >= cutoff,
                    )
                    .order_by(
                        AgentOperation.updated_at.desc(),
                        AgentOperation.id.desc(),
                        AgentOperationAttempt.attempt.desc(),
                    )
                    .limit(_AGENT_LOG_SCAN_LIMIT)
                )
            )
        entries: list[FleetLogEntry] = []
        for operation, attempt in rows:
            payload = (
                operation.payload if isinstance(operation.payload, Mapping) else {}
            )
            # Only the agent's own receipt may name an error code; a lease lapse
            # arrived with no receipt, so the fallback narrative is the
            # Controller's reason for the wait.
            receipt = attempt.result or payload.get("failure")
            result = receipt or {
                "reason": operation.status_reason or "Operation failed"
            }
            parked = _attempt_is_parked(operation, attempt)
            clock = _lease_clock(operation, attempt)
            observed_at = _aware(operation.updated_at)
            source_name = _agent_log_source(operation.kind)
            try:
                item = FailedAttempt(
                    id=operation.id,
                    attempt=attempt.attempt,
                    kind=operation.kind,
                    node_ids=[operation.node_id],
                    updated_at=observed_at.isoformat(),
                    source="agent",
                    progress=AttemptPhase.model_validate(attempt.progress),
                    result=OperationResultFacts.model_validate(result),
                )
                bundle = collect_failure(item, now=now)
            except Exception:  # noqa: BLE001 - one malformed row must not hide the rest
                headline, level = _ATTEMPT_OUTCOME.get(
                    agent_operation_states.attempt_outcome_key(attempt),
                    _ATTEMPT_OUTCOME["failed"],
                )
                fallback = redact_text(
                    f"{operation.kind} {headline}: "
                    f"{operation.status_reason or 'Operation failed'}"
                )[:4_096]
                if fallback:
                    entries.append(
                        FleetLogEntry(
                            observed_at=observed_at,
                            source=source_name,
                            level=level,
                            message=fallback,
                            evidence_id=operation.id,
                        )
                    )
                continue
            entries.extend(
                _failure_log_entries(
                    operation_id=operation.id,
                    kind=operation.kind,
                    source=source_name,
                    bundle=bundle,
                    observed_at=observed_at,
                    state=agent_operation_states.attempt_outcome_key(attempt),
                    clock=clock,
                    controller_reason=(operation.status_reason if parked else None),
                    has_receipt=receipt is not None,
                )
            )
            if len(entries) >= _AGENT_LOG_ENTRY_LIMIT:
                break
        return entries
