"""Value types of the shared lifecycle core.

These types hold no behavior and know no database: a :class:`Lifecycle` row is
the kind-independent projection of one lifecycle subject (an agent order, a
cache operation, a profile application), an :class:`Event` is something that
happened to it, and a :class:`Decision` is what the pure transition function
concluded.  Kind-specific payload stays in the owner's own tables and is reached
through its :class:`~vonk_control.lifecycle.adapter.KindAdapter`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class State(StrEnum):
    """The eight lifecycle states.

    ``waiting``, ``partial``, ``cancelling`` and ``expired`` of the legacy
    kinds map onto these: waiting and partial work is ``backoff`` or
    ``observing``, a cancelling row is a non-terminal row with
    ``cancel_requested_at`` set.
    """

    QUEUED = "queued"
    RUNNING = "running"
    OBSERVING = "observing"
    BACKOFF = "backoff"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_OPERATOR = "needs-operator"


TERMINAL_STATES = frozenset({State.SUCCEEDED, State.FAILED, State.CANCELLED})


class Effect(StrEnum):
    """What is known about the real-world effect of the work."""

    UNKNOWN = "unknown"
    NONE = "none"
    ISSUED = "issued"
    ESTABLISHED = "established"
    STOPPED = "stopped"


class Outcome(StrEnum):
    """What an executor reported.

    ``ok``, ``cancelled`` and a ``failed`` that is not ``retryable`` are
    *definite*: the executor says what happened, and the row ends there.
    ``uncertain`` (and a retryable failure) says the effect may or may not have
    happened, which rules 1 and 2 resolve.
    """

    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class StopResult(StrEnum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class Lifecycle:
    """One lifecycle subject, independent of its kind.

    ``next_action_at`` is the one retry, observation and cancel clock.
    ``cancel_requested_at`` is monotonic: once set it is never cleared.  The
    operator actions of a row are derived by its adapter and never stored.
    """

    id: str
    kind: str
    state: State = State.QUEUED
    attempt: int = 0
    fence: str | None = None
    lease_deadline: datetime | None = None
    next_action_at: datetime | None = None
    retry_count: int = 0
    observe_count: int = 0
    intent_ordinal: int | None = None
    cancel_requested_at: datetime | None = None
    cancel_request_key: str | None = None
    effect: Effect = Effect.NONE
    reason: str | None = None
    owner_id: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_requested_at is not None


# ------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class Submitted:
    """The subject was created."""


@dataclass(frozen=True, slots=True)
class Claimed:
    """An executor took the work: a new attempt under a new fence and lease."""

    attempt: int
    fence: str
    lease_deadline: datetime


@dataclass(frozen=True, slots=True)
class Heartbeat:
    fence: str | None
    lease_deadline: datetime


@dataclass(frozen=True, slots=True)
class Reported:
    """An executor's report.

    ``effect`` is the reporter's own claim about the effect when it has one
    (``none`` for "never started", ``stopped`` for a confirmed stop).
    ``retryable`` marks a failure the owner knows is transient.  A report whose
    ``fence`` is not the row's is stale and dropped (rule 7).
    """

    outcome: Outcome
    fence: str | None = None
    retryable: bool = False
    effect: Effect | None = None
    reason: str | None = None
    #: A dependency's ``Retry-After``: the retry may not start before it.
    retry_after: datetime | None = None


@dataclass(frozen=True, slots=True)
class LeaseLapsed:
    """The running attempt can no longer report."""

    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CancelRequested:
    """Cancel, or a newer intent superseding this one (rule 6)."""

    request_key: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Observed:
    """The result of ``adapter.observe`` or ``adapter.stop``."""

    effect: Effect
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class OperatorAction:
    """A person chose an advertised action (``resume``, ``retire``, ``stop``)."""

    name: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Tick:
    """The clock reached ``next_action_at``."""


Event = (
    Submitted
    | Claimed
    | Heartbeat
    | Reported
    | LeaseLapsed
    | CancelRequested
    | Observed
    | OperatorAction
    | Tick
)


# ----------------------------------------------------------------- commands


@dataclass(frozen=True, slots=True)
class Execute:
    """Issue the work (safe to repeat only when it is not irreversible)."""


@dataclass(frozen=True, slots=True)
class Observe:
    """Inspect the real effect, read-only; answer with :class:`Observed`."""


@dataclass(frozen=True, slots=True)
class Stop:
    """Idempotent stop or cleanup; answer with :class:`Observed`."""


@dataclass(frozen=True, slots=True)
class RecordResidue:
    """A cancel gave up confirming the stop: record what may remain.

    The retention sweeper and the advertised ``stop`` action own the residue; the
    row itself is already ``cancelled`` and never waits for an operator.
    """

    reason: str


Command = Execute | Observe | Stop | RecordResidue


@dataclass(frozen=True, slots=True)
class Decision:
    """The next row and the commands that follow from the event."""

    row: Lifecycle
    commands: tuple[Command, ...] = field(default_factory=tuple)
