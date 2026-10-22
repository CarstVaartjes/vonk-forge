"""Which installation directories and run scopes on a Spark no record owns."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationState,
    LifecycleState,
    LifecycleSubject,
    RunState,
    stored_words,
)

from .models import (
    ArtifactJob,
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)


def node_run_ids(node_id: str) -> Select[str]:
    """Run identities assigned here; observed state does not change membership."""
    return select(RunNode.run_id).where(RunNode.node_id == node_id)


def untracked_installations(
    session: Session, node_id: str, reported: Collection[str]
) -> list[str]:
    """The reported installation ids the Controller does not track.

    An id is untracked when no record has it, or its record is uninstalled: an
    uninstalled installation is never installed again, so a directory left
    under its id is a leftover. Any other record, in any state, on its assigned
    Spark only keeps the directory on that Spark; historical placement on a
    different identity is not a live owner here.
    """

    if not reported:
        return []
    tracked = set(
        session.scalars(
            select(RecipeInstallation.id).where(
                RecipeInstallation.id.in_(tuple(set(reported))),
                RecipeInstallation.state != InstallationState.UNINSTALLED,
                RecipeInstallation.id.in_(
                    select(InstallationNode.installation_id).where(
                        InstallationNode.node_id == node_id
                    )
                ),
            )
        )
    )
    return sorted(set(reported) - tracked)


def untracked_runs(
    session: Session, node_id: str, reported: Collection[str]
) -> list[str]:
    """The reported run scope ids the Controller does not track.

    A scope (``runs/<id>``, ``run-metadata/<id>``) is a run's or an artifact
    job's. It is untracked when no run and no job has the id, or its run is
    stopped or failed: a new start plans a new run,
    and a Spark that still reports its process turns it lost again, which
    tracks it. Only nonterminal jobs keep their scratch; published outputs live
    in the Controller artifact store. The agent retains local lifecycle claims
    until its runtime inspection proves the process gone. A run or job assigned
    elsewhere holds no scope on this Spark, including after identity changes.
    """

    if not reported:
        return []
    ids = tuple(set(reported))
    tracked = set(
        session.scalars(
            select(RecipeRun.id).where(
                RecipeRun.id.in_(ids),
                RecipeRun.id.in_(node_run_ids(node_id)),
                RecipeRun.state.not_in((RunState.STOPPED, RunState.FAILED)),
            )
        )
    ) | set(
        session.scalars(
            select(ArtifactJob.id).where(
                ArtifactJob.id.in_(ids),
                ArtifactJob.run_id.in_(node_run_ids(node_id)),
                or_(
                    ArtifactJob.state.is_(None),
                    ArtifactJob.state.not_in(
                        stored_words(
                            LifecycleSubject.ARTIFACT_JOB,
                            (
                                LifecycleState.SUCCEEDED,
                                LifecycleState.FAILED,
                                LifecycleState.CANCELLED,
                            ),
                        )
                    ),
                ),
            )
        )
    )
    return sorted(set(reported) - tracked)
