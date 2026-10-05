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

    ``due`` returns non-terminal rows with ``next_action_at <= now`` ordered by
    ``next_action_at`` and locks them for the caller (``FOR UPDATE SKIP LOCKED``
    in SQL), ``save`` persists the decided row, and ``record_residue`` writes the
    note the retention sweeper owns when a cancel gave up confirming a stop.
    """

    def due(self, now: datetime, limit: int) -> Sequence[Lifecycle]: ...

    def save(self, row: Lifecycle) -> None: ...

    def record_residue(self, row: Lifecycle, reason: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    examined: int = 0
    changed: int = 0
    commands: int = 0


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
            did_change, ran = self._settle(row, adapter, now)
            changed += did_change
            commands += ran
        return ReconcileReport(examined, changed, commands)

    def _settle(
        self, row: Lifecycle, adapter: KindAdapter, now: datetime
    ) -> tuple[int, int]:
        event: Event | None = Tick()
        changed = ran = 0
        for _ in range(MAX_STEPS_PER_ROW):
            if event is None:
                break
            decision = transition(row, event, adapter, now)
            if decision.row != row:
                self._store.save(decision.row)
                changed += 1
            row = decision.row
            event = None
            for command in decision.commands:
                ran += 1
                answer = self._run(command, row, adapter)
                if answer is not None:
                    event = answer
        return changed, ran

    def _run(
        self, command: Command, row: Lifecycle, adapter: KindAdapter
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
                    self._store.record_residue(row, reason)
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
