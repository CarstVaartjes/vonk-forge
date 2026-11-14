"""Contracts 5.0 removed ``identity.family`` from every Model document.

A 4.x Controller stored the 4.9 release's rows (Model documents carrying the
family, recipes pinning those digests). The first 5.0 sync over them adds one
head revision per Model and recipe with the same execution, so nothing runs
differently: the head shows the 5.0 release's own published view (whose status
the recipes side proved equal to the 4.9 one, in R1's index diff), the measured
peaks carry to the new recipe digest, the artifact key (and with it every cached
file) stays, and no workload is told to reload. The old rows still read.
"""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from vonk_control.catalog_entities import CatalogEntityService, _digest
from vonk_control.catalog_entities import _execution_projection as execution_of
from vonk_control.catalog_revision_contract import (
    ModelRevisionProjection,
    RecipeRevisionProjection,
    read_catalog_document,
    read_catalog_projection,
)
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncRequest,
)
from vonk_control.models import CatalogDocumentRevision, MeasuredRolePeak
from vonk_control.observed_run_memory import observed_role_peaks
from vonk_control.recipe_update_notice import (
    newest_active_revisions,
    recipe_update_notice,
)
from vonk_forge_contracts import document_sha256, read_model, read_recipe

from tests.signed_recipe_release import signed_recipe_releases

from .test_catalog_sync import _fixture, _sync
from .test_model_facts_split import NOW, _artifact_key, _store

pytestmark = pytest.mark.usefixtures(signed_recipe_releases.__name__)

FAMILY = {"publisher": "vonk-forge", "slug": "retired-family"}


def _with_family(model: dict[str, Any]) -> dict[str, Any]:
    """The Model document as the 4.9 release published it."""
    legacy = copy.deepcopy(model)
    legacy["identity"]["family"] = dict(FAMILY)
    return legacy


def test_a_5_0_sync_over_stored_4_9_rows_moves_no_status_carries_peaks_and_offers_no_update(
    tmp_path: Path,
) -> None:
    sessions, service, reader, item = _fixture(tmp_path)
    entry = reader.snapshot.catalog_entities[0]
    new_model = read_model(entry.published)
    legacy_model = _with_family(entry.published)
    assert read_model(legacy_model) == new_model  # the same execution document
    model_key = _artifact_key(new_model)

    legacy_recipe: dict[str, Any] = copy.deepcopy(item.document)
    pins = [
        selection["model"]
        for selection in legacy_recipe["models"]
        if selection["model"]["slug"] == entry.entry.document.identity.slug
    ]
    assert pins
    for pin in pins:
        pin["content_sha256"] = document_sha256(legacy_model)
    legacy_digest = document_sha256(legacy_recipe)
    assert legacy_digest != item.content_sha256
    # The keys the 4.x Controller derived: the family never reached them.
    model_keys = {
        document_sha256(other.published): _artifact_key(read_model(other.published))
        for other in reader.snapshot.catalog_entities
    }
    model_keys[document_sha256(legacy_model)] = model_key
    artifact_key = _digest(
        {
            "models": [
                {
                    "selection_id": selection["id"],
                    "artifact_key": model_keys[selection["model"]["content_sha256"]],
                }
                for selection in legacy_recipe["models"]
            ]
        }
    )
    execution_key = _digest(
        {
            "execution": execution_of(read_recipe(legacy_recipe)),
            "artifact_key": artifact_key,
        }
    )
    # The 4.9 release published the same status for the old digest, its proof
    # naming the old digest; the 5.0 release names the new one.
    published = item.qualification
    assert published is not None
    legacy_view = published.model_dump(mode="json")
    for side in ("last_pass", "last_failure"):
        if legacy_view.get(side):
            legacy_view[side]["identity"]["content_sha256"] = legacy_digest
    legacy_projection = {"qualification": legacy_view}

    with sessions.begin() as session:
        legacy_model_id = _store(
            session,
            legacy_model,
            projected={},
            artifact_key=model_key,
        )
        legacy_recipe_id = _store(
            session,
            legacy_recipe,
            projected=legacy_projection,
            artifact_key=artifact_key,
            execution_key=execution_key,
            source_bundle_sha256=item.source_bundle_sha256,
        )
        session.add_all(
            [
                MeasuredRolePeak(
                    content_sha256=legacy_digest, role="entrypoint", peak_bytes=700
                ),
                MeasuredRolePeak(
                    content_sha256=legacy_digest, role="worker", peak_bytes=900
                ),
            ]
        )

    _sync(sessions, service, reader).sync(
        ManagedCatalogSyncRequest(
            request_key=str(uuid.uuid4()),
            trigger=CatalogSyncTrigger.MANUAL,
            actor="test",
        )
    )

    with sessions() as session:
        legacy_row = session.get(CatalogDocumentRevision, legacy_recipe_id)
        assert legacy_row is not None and legacy_row.state == "active"
        heads = newest_active_revisions(session, [legacy_row.document_id])
        head = heads[legacy_row.document_id]
        assert head.id != legacy_recipe_id
        assert head.content_digest == item.content_sha256
        # Same execution: the catalog keys did not move.
        assert (head.execution_key, head.source_bundle_sha256) == (
            execution_key,
            item.source_bundle_sha256,
        )
        # No "Update available": reloading would run the same thing.
        assert recipe_update_notice("Recipe", legacy_row, head) is None
        # The head shows the new release's own view, neither the stored old
        # one carried over nor a local reset, and the status is the old one.
        before = read_catalog_projection(legacy_row)
        after = read_catalog_projection(head)
        assert isinstance(before, RecipeRevisionProjection)
        assert isinstance(after, RecipeRevisionProjection)
        assert before.qualification is not None
        assert after.qualification == published
        assert after.qualification.status == before.qualification.status
        # Measured wins: the new digest starts with what the old one measured.
        assert observed_role_peaks(session, item.content_sha256) == {
            "entrypoint": 700,
            "worker": 900,
        }
        # No re-download: the new Model head has the old artifact key.
        legacy_model_row = session.get(CatalogDocumentRevision, legacy_model_id)
        assert legacy_model_row is not None
        new_model_rows = [
            row
            for row in session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.document_id == legacy_model_row.document_id,
                    CatalogDocumentRevision.id != legacy_model_id,
                )
            )
        ]
        assert [row.artifact_key for row in new_model_rows] == [model_key]
        assert new_model_rows[0].content_digest == document_sha256(entry.published)


def test_a_stored_4_9_model_row_still_reads_and_its_projection_drops_the_family(
    tmp_path: Path,
) -> None:
    sessions, _service, reader, _item = _fixture(tmp_path)
    document = reader.snapshot.catalog_entities[0].published
    model = read_model(document)
    legacy = _with_family(document)
    projected = {
        "identity": legacy["identity"],
        "format": legacy["format"],
        "artifact_count": len(model.files),
        "download_bytes": model.download_bytes,
        "installed_bytes": model.installed_bytes,
    }
    with sessions.begin() as session:
        revision_id = _store(
            session, legacy, projected=projected, artifact_key="1" * 64
        )
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, revision_id)
        assert row is not None
        # The document reads (a retired field is ignored, never a failure) ...
        assert read_catalog_document(row) == model
        # ... and the stored projection is re-derived from it at read time.
        with pytest.raises(ValueError):
            ModelRevisionProjection.model_validate(row.projected)
        repaired = read_catalog_projection(row)
        assert isinstance(repaired, ModelRevisionProjection)
        assert repaired.identity == model.identity
    # The sync's pass persists the repair once; the row keeps its document.
    CatalogEntityService(sessions, clock=lambda: NOW).refresh_model_projections()
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, revision_id)
        assert row is not None
        assert "family" not in row.projected["identity"]
        assert row.document == legacy and row.artifact_key == "1" * 64
