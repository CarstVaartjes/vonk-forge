"""A Spark's installation directory leaves only on the Controller's verdict.

The Controller is the authority for which installations exist: of the
directories a Spark reports, it names those no installation record owns, and
the agent removes them after a day's grace.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import create_engine
from vonk_agent_protocol import InstallationState, LifecycleState, RunState
from vonk_control.models import ArtifactJob, Base, RecipeInstallation
from vonk_control.untracked_installations import untracked_installations, untracked_runs

from .test_artifact_jobs import running_artifact_service, submitted_artifact_job
from .test_catalog_revision_collection import NODE, Catalog


def test_only_installations_no_record_owns_are_untracked(tmp_path: Path) -> None:
    """Catches a verdict that names a tracked installation, or misses a leftover."""

    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")
    Base.metadata.create_all(engine)
    world = Catalog(engine)
    revision = world.revision("glm", 1)
    installed, _ = world.workload(revision, run="stopped")
    failed, _ = world.workload(revision, run="stopped", installation="failed")
    uninstalled, _ = world.workload(revision, run="stopped")
    with world.sessions.begin() as session:
        row = session.get(RecipeInstallation, uninstalled)
        assert row is not None
        row.state = InstallationState.UNINSTALLED
    forgotten = str(uuid.uuid4())

    with world.sessions() as session:
        verdict = untracked_installations(
            session, NODE, [installed, failed, uninstalled, forgotten]
        )
        assert untracked_installations(session, NODE, []) == []
        # Identity/placement changes must not preserve a copy on another node.
        assert untracked_installations(session, "replacement-spark", [installed]) == [
            installed
        ]

    # A record in any state but uninstalled keeps its directory.
    assert verdict == sorted([uninstalled, forgotten])


def test_only_run_scopes_no_run_or_job_owns_are_untracked(tmp_path: Path) -> None:
    """Catches a verdict that names a live run's or a job's scope, or misses a
    stopped or forgotten run's."""

    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")
    Base.metadata.create_all(engine)
    world = Catalog(engine)
    revision = world.revision("glm", 1)
    runs = {
        state: world.workload(revision, run=state)[1]
        for state in (
            RunState.RUNNING,
            RunState.STOPPING,
            RunState.FAILED,
            RunState.LOST,
            RunState.STOPPED,
        )
    }
    forgotten = str(uuid.uuid4())

    with world.sessions() as session:
        verdict = untracked_runs(session, NODE, [*runs.values(), forgotten])
        assert untracked_runs(session, NODE, []) == []
        assert untracked_runs(session, "spk_" + "f" * 32, runs.values()) == sorted(
            runs.values()
        )

    # Failed/stopped run history owns no scratch. Lost runs remain wanted;
    # the agent's live lifecycle is an independent veto until process exit.
    expired = [runs[state] for state in (RunState.STOPPED, RunState.FAILED)]
    assert verdict == sorted([*expired, forgotten])


def test_an_artifact_jobs_scope_is_tracked_while_its_job_exists(tmp_path: Path) -> None:
    sessions, _ops, _queue, service, run_id, node = running_artifact_service(tmp_path)
    job = submitted_artifact_job(service, run_id, request_suffix=410)
    forgotten = str(uuid.uuid4())

    with sessions() as session:
        assert untracked_runs(session, node, [job.id, run_id, forgotten]) == [forgotten]
        assert untracked_runs(session, "spk_" + "f" * 32, [job.id, run_id]) == sorted(
            [job.id, run_id]
        )


def test_terminal_job_history_does_not_own_spark_scratch(tmp_path: Path) -> None:
    """Catches keeping failed/completed job scratch forever because a row exists."""
    sessions, _ops, _queue, service, run_id, node = running_artifact_service(tmp_path)
    job = submitted_artifact_job(service, run_id, request_suffix=411)
    for state in (
        LifecycleState.SUCCEEDED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
    ):
        with sessions.begin() as session:
            row = session.get(ArtifactJob, job.id)
            assert row is not None
            row.state = state
        with sessions() as session:
            assert untracked_runs(session, node, [job.id, run_id]) == [job.id]
