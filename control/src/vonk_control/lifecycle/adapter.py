"""The per-kind adapter protocol of the lifecycle core.

An adapter is the only place that knows a kind's tables, its executor and its
operator surfaces.  The core never branches on ``kind``.  ``irreversible`` and
``actions`` are called by the pure transition function, so they must be
read-only and deterministic for a given store state; ``execute``, ``observe`` and
``stop`` are called by the reconciler for the commands a decision names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .types import Lifecycle, Observed, StopResult


@dataclass(frozen=True, slots=True)
class Dispatch:
    """Whether ``execute`` handed the work to an executor.

    A polled kind (the Spark claims its own work) returns ``issued=False``: the
    claim arrives later as a ``Claimed`` event.
    """

    issued: bool = False
    reason: str | None = None


@runtime_checkable
class KindAdapter[RowT](Protocol):
    """Everything kind-specific, in one place."""

    kind: str

    def adopt(self, stored: RowT) -> Lifecycle:
        """Map a legacy row onto the core, defaulting what it lacks (pure).

        A legacy ``running`` row with a live lease stays ``running``; one with an
        expired lease becomes ``observing``.  A legacy ``waiting-for-operator``
        row is returned as ``needs-operator`` and re-evaluated by rules 1 to 3 on
        its first tick.  Nothing is rewritten until the row's next transition.
        """
        ...

    def irreversible(self, row: Lifecycle) -> bool:
        """Whether repeating ``execute`` could repeat a visible effect."""
        ...

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        """Issue the effect; safe to call again only if not irreversible."""
        ...

    def observe(self, row: Lifecycle) -> Observed:
        """Read-only inspection: established, none or unknown, with evidence."""
        ...

    def stop(self, row: Lifecycle) -> StopResult:
        """Idempotent stop or cleanup: confirmed or unconfirmed."""
        ...

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        """The real operator actions for this row, from the one function the
        action endpoints also call (so the UI and the mutation cannot disagree)."""
        ...

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        """Composite subjects only: their state is ``aggregate(children)``."""
        ...
