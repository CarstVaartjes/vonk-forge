"""The generic reconcile loop on top of the lifecycle core.

One loop replaces the per-kind ticks and sweeps: select the non-terminal rows
whose ``next_action_at`` is due, feed each one a ``Tick``, run the commands the
decision names through the kind's adapter, and feed the answers back until the
row has nothing more to do now.  A restart repeats a tick harmlessly because
``transition`` is idempotent for a tick that is not yet due and every command is
safe to repeat.

The loop is **disabled by default**.  Enabling it is a per-deployment decision
made when a kind has moved onto the core (the migration order is in the blocker
audit, section 5.6); until then :meth:`Reconciler.reconcile` does nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .adapter import KindAdapter
from .core import transition
from .types import (
    Command,
    Effect,
    Event,
    Execute,
    Lifecycle,
    Observe,
    Observed,
    RecordResidue,
    Stop,
    StopResult,
    Tick,
)

_LOGGER = logging.getLogger(__name__)
#: Commands followed by answers inside one pass over a row.  A row that is still
#: moving after this many steps is picked up again by its next ``next_action_at``.
MAX_STEPS_PER_ROW = 4


class LifecycleStore(Protocol):
    """Where lifecycle rows live; one writer per row.

    ``due`` returns the non-terminal rows that may need a decision now (a retry or
    observation whose ``next_action_at`` has come, a lapsed lease, a wait to
    re-evaluate).  ``save`` persists a decision under the row's lock and returns
    ``False`` when the stored row is no longer the ``before`` the decision was
    made from (a concurrent report, claim or cancel): the pass for that row ends
    and the next one reads it afresh.  ``record_residue`` writes the note the
    retention sweeper owns when a cancel gave up confirming a stop.
    """

    def due(self, now: datetime, limit: int) -> Sequence[Lifecycle]: ...

    def save(self, before: Lifecycle, after: Lifecycle) -> bool: ...

    def record_residue(self, row: Lifecycle, reason: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    examined: int = 0
    changed: int = 0
    commands: int = 0


@dataclass(frozen=True, slots=True)
class Settled:
    """What one row ended as after an event and the commands it caused."""

    row: Lifecycle
    changed: int = 0
    commands: int = 0
    stale: bool = False


def settle(
    row: Lifecycle,
    event: Event,
    adapter: KindAdapter,
    now: datetime,
    *,
    save: Callable[[Lifecycle, Lifecycle], bool],
    residue: Callable[[Lifecycle, str], None],
    max_steps: int = MAX_STEPS_PER_ROW,
) -> Settled:
    """Feed ``event`` to ``row``, run the commands it names, feed the answers back.

    Shared by the reconcile loop and by an owner that holds the row's lock and
    decides inline: both reach the same rows through the same function.
    """

    pending: Event | None = event
    changed = ran = 0
    for _ in range(max_steps):
        if pending is None:
            break
        decision = transition(row, pending, adapter, now)
        pending = None
        if decision.row != row:
            if not save(row, decision.row):
                return Settled(row, changed, ran, stale=True)
            changed += 1
        row = decision.row
        for command in decision.commands:
            ran += 1
            answer = _run(command, row, adapter, residue)
            if answer is not None:
                pending = answer
    return Settled(row, changed, ran)


def _run(
    command: Command,
    row: Lifecycle,
    adapter: KindAdapter,
    residue: Callable[[Lifecycle, str], None],
) -> Observed | None:
    """Run one command; an executor failure is an unknown, never a raise."""

    try:
        match command:
            case Observe():
                return adapter.observe(row)
            case Stop():
                confirmed = adapter.stop(row) is StopResult.CONFIRMED
                return Observed(Effect.STOPPED if confirmed else Effect.UNKNOWN)
            case Execute():
                adapter.execute(row, row.attempt + 1)
            case RecordResidue(reason=reason):
                residue(row, reason)
    except Exception as error:  # noqa: BLE001 - bookkeeping becomes unknown
        _LOGGER.warning(
            "lifecycle command %s failed for %s %s: %s",
            type(command).__name__,
            row.kind,
            row.id,
            error,
        )
        if isinstance(command, (Observe, Stop)):
            return Observed(Effect.UNKNOWN, "the inspection could not run")
    return None


class Reconciler:
    """``reconcile(limit)``: one pass over the due rows."""

    def __init__(
        self,
        store: LifecycleStore,
        adapters: Mapping[str, KindAdapter],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        enabled: bool = False,
    ) -> None:
        self._store = store
        self._adapters = dict(adapters)
        self._clock = clock
        self.enabled = enabled

    def reconcile(self, limit: int = 100) -> ReconcileReport:
        if not self.enabled:
            return ReconcileReport()
        now = self._clock()
        examined = changed = commands = 0
        for row in self._store.due(now, limit):
            adapter = self._adapters.get(row.kind)
            if adapter is None:
                continue  # a kind that has not moved onto the core
            examined += 1
            settled = settle(
                row,
                Tick(),
                adapter,
                now,
                save=self._store.save,
                residue=self._store.record_residue,
            )
            changed += settled.changed
            commands += settled.commands
        return ReconcileReport(examined, changed, commands)
