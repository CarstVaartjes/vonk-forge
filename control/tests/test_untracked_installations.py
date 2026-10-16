"""A Spark's installation directory leaves only on the Controller's verdict.

The Controller is the authority for which installations exist: of the
directories a Spark reports, it names those no installation record owns, and
the agent removes them after a day's grace.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import create_engine
from vonk_agent_protocol import InstallationState
from vonk_control.models import Base, RecipeInstallation
from vonk_control.untracked_installations import untracked_installations

from .test_catalog_revision_collection import Catalog


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
            session, [installed, failed, uninstalled, forgotten]
        )
        assert untracked_installations(session, []) == []

    # A record in any state but uninstalled keeps its directory.
    assert verdict == sorted([uninstalled, forgotten])
