"""The one edge that ends an unfinished installation.

An installation in ``planned`` or ``installing`` is in flight only while a live
operation names it. Whichever path ends that operation (an agent result, the
operator's retirement, a supersession or a cancel before any claim), the
installation leaves the in-flight states through this edge to ``partial``:
the Controller cannot prove nothing reached the Spark, so the existing
retention sweep removes whatever it left, and the next attempt may adopt it.
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


def end_unfinished_installation(
    installation: RecipeInstallation, now: datetime
) -> bool:
    """Move a locked in-flight installation to ``partial``; False if it is not one."""

    if installation.state not in IN_FLIGHT:
        return False
    installation.state = InstallationState.PARTIAL
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
            RecipeInstallation.state == InstallationState.INSTALLING,
            RecipeInstallation.updated_at == seen,
        )
        .values(state=InstallationState.PARTIAL, updated_at=now)
    )
    return cast(CursorResult[Any], result).rowcount == 1


__all__ = ["IN_FLIGHT", "end_orphaned_installation", "end_unfinished_installation"]
