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
released by the residue sweep's never-installed path instead. The cancel receipt
(``end_cancelled_operation``) and the Spark-lost-image demotion
(``demote_installed_rank``) are the other edges; the terminal projection of an
agent result writes its own source and stays separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.orm import Session
from vonk_agent_protocol import InstallationNodeState, InstallationState

from .machine_states import rank_installed
from .models import InstallationNode, RecipeInstallation

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


def end_cancelled_operation(
    rank: InstallationNode | None,
    installation: RecipeInstallation | None,
    now: datetime,
) -> None:
    """A cancelled install, uninstall or reconcile ended: its locked rank is
    ``failed`` and its installation ``partial``, so whatever it left is removable
    and the next attempt may adopt it. A row that is gone is simply skipped."""

    if rank is not None:
        rank.state = InstallationNodeState.FAILED
        rank.updated_at = now
    if installation is not None:
        installation.state = ENDED_AS
        installation.updated_at = now


@dataclass(frozen=True)
class RankRead:
    """An installed rank as read: the version ``demote_installed_rank`` fences on."""

    rank_id: str
    installation_id: str
    updated_at: datetime


def demote_installed_rank(
    session: Session, seen: RankRead, *, before: datetime, now: datetime
) -> bool:
    """The edge ``installed`` rank -> ``planned`` (installation -> ``partial``),
    as a compare-and-set on the rank's ``updated_at`` as ``seen``.

    The Spark lost what the rank needs, so the next plan installs it afresh. A
    transition that landed after the read changes ``updated_at``, so this
    misses (False) instead of overwriting it; the caller re-reads and re-decides.
    The installation moves only while no newer transition (``before``) has
    touched it, and a miss there leaves it to that transition: the rank is
    already planned, which every installation state tolerates.
    """

    moved = session.execute(
        update(InstallationNode)
        .where(
            InstallationNode.id == seen.rank_id,
            rank_installed(InstallationNode.state),
            InstallationNode.updated_at == seen.updated_at,
        )
        .values(state=InstallationNodeState.PLANNED, installed_bytes=0, updated_at=now)
    )
    if cast(CursorResult[Any], moved).rowcount != 1:
        return False
    session.execute(
        update(RecipeInstallation)
        .where(
            RecipeInstallation.id == seen.installation_id,
            RecipeInstallation.state.in_(
                (InstallationState.INSTALLED, InstallationState.PARTIAL)
            ),
            RecipeInstallation.updated_at <= before,
        )
        .values(state=InstallationState.PARTIAL, updated_at=now)
    )
    return True


__all__ = [
    "ENDED_AS",
    "IN_FLIGHT",
    "RankRead",
    "demote_installed_rank",
    "end_cancelled_operation",
    "end_orphaned_installation",
    "end_unfinished_installation",
]
