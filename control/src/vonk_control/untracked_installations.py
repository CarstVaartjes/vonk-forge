"""Which installation directories on a Spark no installation record owns."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import InstallationState

from .models import RecipeInstallation


def untracked_installations(session: Session, reported: Collection[str]) -> list[str]:
    """The reported installation ids the Controller does not track.

    An id is untracked when no record has it, or its record is uninstalled: an
    uninstalled installation is never installed again, so a directory left
    under its id is a leftover. Any other record, in any state and on any
    Spark, keeps the directory.
    """

    if not reported:
        return []
    tracked = set(
        session.scalars(
            select(RecipeInstallation.id).where(
                RecipeInstallation.id.in_(tuple(set(reported))),
                RecipeInstallation.state != InstallationState.UNINSTALLED,
            )
        )
    )
    return sorted(set(reported) - tracked)
