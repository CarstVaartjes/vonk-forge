"""A recipe revision's source bundle digest lives in its own column.

Revisions stored before the column existed carry the digest in ``projected``;
the startup fill moves it, so the column is the only copy.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import update
from vonk_control.catalog_revision_contract import read_recipe_projection
from vonk_control.models import CatalogDocumentRevision
from vonk_control.reference_facts import fill_revision_source_bundles

from .test_recipe_builds import setup


def test_an_old_revision_moves_its_digest_to_the_column(tmp_path: Path) -> None:
    sessions, _bundles, _now, _node, revision = setup(tmp_path)
    sha = str(revision.source_bundle_sha256)
    with sessions.begin() as session:
        old = session.get(CatalogDocumentRevision, revision.id)
        assert old is not None
        # The old shape: the digest in the projection, the column empty.
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision.id)
            .values(
                projected={**old.projected, "source_bundle_sha256": sha},
                source_bundle_sha256=None,
            )
        )

    with sessions.begin() as session:
        assert fill_revision_source_bundles(session.connection()) == 1
    with sessions.begin() as session:
        assert fill_revision_source_bundles(session.connection()) == 0
        healed = session.get(CatalogDocumentRevision, revision.id)
        assert healed is not None
        assert healed.source_bundle_sha256 == sha
        assert "source_bundle_sha256" not in healed.projected
        assert read_recipe_projection(healed).identity is not None


def test_a_revision_that_names_no_source_stays_unknown(tmp_path: Path) -> None:
    sessions, _bundles, _now, _node, revision = setup(tmp_path)
    with sessions.begin() as session:
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision.id)
            .values(source_bundle_sha256=None)
        )
    with sessions.begin() as session:
        assert fill_revision_source_bundles(session.connection()) == 0
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None and row.source_bundle_sha256 is None
