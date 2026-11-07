"""The one edge that ends an unfinished installation.

An installation in ``planned`` or ``installing`` is waiting on its owning
operation. Ending that operation without finishing the install leaves it
through this edge, ``IN_FLIGHT`` -> ``ENDED_AS``, written one way
(``end_unfinished_installation``) or as a compare-and-set
(``end_orphaned_installation``); both read the same definition. The Controller
cannot prove nothing reached the Spark, so the retention sweep removes whatever
it left, and the next attempt may adopt it.

Callers: the operator's retirement (it locks the row), and the residue sweep
for an ``installing`` row no live operation names (supersession or a cancel
before any claim). A ``planned`` row with no owner is
released by the residue sweep's never-installed path instead. The agent-result
projections (``terminal_projection``, and the cancel receipt in
``result_consumption``, which also ends an installed row's cancelled uninstall
or reconcile as ``partial``) write other sources and stay separate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.orm import Session
from vonk_agent_protocol import InstallationState

from .models import RecipeInstallation

#: The states in which an installation is waiting on its owning operation.
IN_FLIGHT: tuple[InstallationState, ...] = (
    InstallationState.PLANNED,
    InstallationState.INSTALLING,
)


#: Where an unfinished installation goes: possibly written, so removable.
ENDED_AS = InstallationState.PARTIAL


def end_unfinished_installation(
    installation: RecipeInstallation, now: datetime
) -> bool:
    """Move a locked in-flight installation to ``partial``; False if it is not one."""

    if installation.state not in IN_FLIGHT:
        return False
    installation.state = ENDED_AS
    installation.updated_at = now
    return True


def end_orphaned_installation(
    session: Session, installation_id: str, seen: datetime, now: datetime
) -> bool:
    """The same edge as a compare-and-set, for a row no operation names.

    ``seen`` is the ``updated_at`` the caller read while no live operation
    named the row. A re-launch rewrites it in the transaction that queues its
    order, so a concurrent one makes this miss instead of overwriting it.
    """

    result = session.execute(
        update(RecipeInstallation)
        .where(
            RecipeInstallation.id == installation_id,
            RecipeInstallation.state.in_(IN_FLIGHT),
            RecipeInstallation.updated_at == seen,
        )
        .values(state=ENDED_AS, updated_at=now)
    )
    return cast(CursorResult[Any], result).rowcount == 1


__all__ = [
    "ENDED_AS",
    "IN_FLIGHT",
    "end_orphaned_installation",
    "end_unfinished_installation",
]
