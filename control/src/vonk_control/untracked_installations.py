"""Which installation directories and run scopes on a Spark no record owns."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import InstallationState, RunState

from .models import ArtifactJob, RecipeInstallation, RecipeRun


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


def untracked_runs(session: Session, reported: Collection[str]) -> list[str]:
    """The reported run scope ids the Controller does not track.

    A scope (``runs/<id>``, ``run-metadata/<id>``) is a run's or an artifact
    job's. It is untracked when no run and no job has the id, or its run is
    stopped: a stopped run is never started again (a start plans a new run),
    and a Spark that still reports its process turns it lost again, which
    tracks it. Any other run, and any job, keeps the scope.
    """

    if not reported:
        return []
    ids = tuple(set(reported))
    tracked = set(
        session.scalars(
            select(RecipeRun.id).where(
                RecipeRun.id.in_(ids), RecipeRun.state != RunState.STOPPED
            )
        )
    ) | set(session.scalars(select(ArtifactJob.id).where(ArtifactJob.id.in_(ids))))
    return sorted(set(reported) - tracked)
