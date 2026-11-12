"""A recipe revision's source bundle digest lives in its own column.

Revisions stored before the column existed carry the digest in ``projected``;
the startup fill moves it, so the column is the only copy.
"""

from __future__ import annotations

import io
from pathlib import Path

from sqlalchemy import update
from vonk_control.catalog_revision_contract import (
    read_recipe_projection,
    write_catalog_projection,
)
from vonk_control.models import CatalogDocumentRevision
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.reference_facts import fill_revision_source_bundles

from .test_recipe_builds import setup


def test_an_old_revision_moves_its_digest_to_the_column(tmp_path: Path) -> None:
    sessions, bundles, _now, _node, revision = setup(tmp_path)
    sha = str(revision.source_bundle_sha256)
    archive = bundles.get(sha).archive
    bundles.put(sha, io.BytesIO(archive)).path.unlink()
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

    # The healed digest is the one the worker restores a missing bundle by.
    service = RecipeBuildService(
        sessions, bundles=bundles, source_rederiver=lambda *_args: archive
    )
    service._source_check_at = 0.0
    assert service.reconcile_source_bundles()
    assert bundles.verified_manifest(sha).sha256 == sha


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


def test_a_stored_projection_never_holds_the_digest(tmp_path: Path) -> None:
    """The response view may carry the digest; what is stored does not."""
    sessions, _bundles, _now, _node, revision = setup(tmp_path)
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        viewed = read_recipe_projection(row).model_copy(
            update={"source_bundle_sha256": row.source_bundle_sha256}
        )
    assert "source_bundle_sha256" not in write_catalog_projection(viewed)
