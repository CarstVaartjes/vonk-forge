"""Contracts 4.0 moved a Model's declared facts into its display.

The first sync of a 4.0 library over rows a 3.x Controller stored adds one head
revision per Model and recipe, keeps every old row byte for byte, downloads
nothing again (the artifact key is unchanged) and carries the measured memory
peaks to the re-keyed recipe digest instead of forgetting them.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from vonk_control.catalog_entities import (
    CatalogCode,
    CatalogEntityService,
    CatalogValidationError,
    _digest,
    _execution_projection,
)
from vonk_control.catalog_revision_contract import (
    ModelRevisionProjection,
    read_catalog_projection,
)
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncRequest,
)
from vonk_control.models import (
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    MeasuredRolePeak,
    RecipeRun,
)
from vonk_control.observed_run_memory import observed_role_peaks
from vonk_control.recipe_library_types import RecipeLibrarySnapshot
from vonk_forge_contracts import (
    ModelDefinition,
    document_sha256,
    read_model,
    read_recipe,
)
from vonk_forge_contracts.qualification_status import (
    PublishedQualificationStatusView,
    QualificationQualityNote,
)

from tests.recipe_library_source import recipe_library_root
from tests.signed_recipe_release import signed_recipe_releases

from .display_fixtures import create_draft
from .test_catalog_sync import _fixture, _sync

pytestmark = pytest.mark.usefixtures(signed_recipe_releases.__name__)

NOW = datetime(2026, 9, 5, tzinfo=UTC)
LEGACY_LICENSE = {
    "spdx": "Apache-2.0",
    "url": "https://www.apache.org/licenses/LICENSE-2.0",
    "attribution": [],
}


def _artifact_key(model: ModelDefinition) -> str:
    projection = _execution_projection(model)
    assert isinstance(projection, dict)
    return str(projection["artifact_key"])


def _legacy_model(model: dict) -> dict:
    """The Model document a 3.x library published: the facts inside it."""
    return {
        **copy.deepcopy(model),
        "capabilities": ["text-generation"],
        "modalities": ["text"],
        "license": LEGACY_LICENSE,
    }


def _store(
    session,
    document: dict,
    *,
    projected: dict,
    artifact_key: str,
    execution_key: str = "2" * 64,
) -> str:
    """Insert an active revision exactly as a 3.x Controller wrote it."""
    root = CatalogDocument(
        kind=document["kind"],
        publisher=document["identity"]["publisher"],
        slug=document["identity"]["slug"],
        title="Stored title",
        created_by="admin",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(root)
    session.flush()
    revision = CatalogDocumentRevision(
        document_id=root.id,
        kind=root.kind,
        publisher=root.publisher,
        slug=root.slug,
        revision_number=1,
        schema_version=2,
        state="active",
        document={},
        content_digest=document_sha256(document),
        artifact_key=artifact_key,
        execution_key=execution_key,
        projected={},
        created_by="admin",
        created_at=NOW,
    )
    session.add(revision)
    session.flush()
    session.execute(
        update(CatalogDocumentRevision)
        .where(CatalogDocumentRevision.id == revision.id)
        .values(document=document, projected=projected)
    )
    session.add(
        CatalogDocumentHead(
            kind=root.kind,
            publisher=root.publisher,
            slug=root.slug,
            active_revision_id=revision.id,
        )
    )
    return revision.id


def _legacy_projection(model: ModelDefinition, legacy: dict) -> dict:
    return {
        "identity": legacy["identity"],
        "format": legacy["format"],
        "capabilities": legacy["capabilities"],
        "modalities": legacy["modalities"],
        "artifact_count": len(model.files),
        "download_bytes": model.download_bytes,
        "installed_bytes": model.installed_bytes,
    }


@pytest.mark.parametrize(
    "variant", ["pins-only", "recipe-also-changed", "model-bytes-changed"]
)
def test_a_4_0_sync_over_stored_3x_rows_adds_heads_keeps_keys_and_carries_peaks(
    tmp_path: Path, variant: str
) -> None:
    carried = variant == "pins-only"
    sessions, service, reader, item = _fixture(tmp_path)
    entry = reader.snapshot.catalog_entities[0]
    new_model = read_model(entry.document)
    legacy_model = _legacy_model(entry.document)
    model_key = _artifact_key(new_model)
    assert read_model(legacy_model) == new_model  # same execution document

    legacy_recipe: dict[str, Any] = copy.deepcopy(item.document)
    pins = [
        selection["model"]
        for selection in legacy_recipe["models"]
        if selection["model"]["slug"] == entry.document["identity"]["slug"]
    ]
    assert pins
    for pin in pins:
        pin["content_sha256"] = document_sha256(legacy_model)
    if variant == "recipe-also-changed":
        role = legacy_recipe["topology"]["roles"][0]
        role["resources"]["memory"]["peak_bytes"] += 1
    legacy_recipe_digest = document_sha256(legacy_recipe)
    # The keys a 3.x Controller derived when it resolved the legacy recipe.
    model_keys = {
        document_sha256(other.document): _artifact_key(read_model(other.document))
        for other in reader.snapshot.catalog_entities
    }
    model_keys[document_sha256(legacy_model)] = (
        "9" * 64 if variant == "model-bytes-changed" else model_key
    )
    recipe_artifact_key = _digest(
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
    recipe_execution_key = _digest(
        {
            "execution": _execution_projection(read_recipe(legacy_recipe)),
            "artifact_key": recipe_artifact_key,
        }
    )
    assert legacy_recipe_digest != item.content_sha256

    with sessions.begin() as session:
        legacy_model_id = _store(
            session,
            legacy_model,
            projected=_legacy_projection(new_model, legacy_model),
            artifact_key="9" * 64
            if variant == "model-bytes-changed"
            else str(model_key),
        )
        legacy_recipe_id = _store(
            session,
            legacy_recipe,
            projected={},
            artifact_key=recipe_artifact_key,
            execution_key=recipe_execution_key,
        )
        session.add_all(
            [
                MeasuredRolePeak(
                    content_sha256=legacy_recipe_digest,
                    role="entrypoint",
                    peak_bytes=700,
                ),
                MeasuredRolePeak(
                    content_sha256=legacy_recipe_digest, role="worker", peak_bytes=900
                ),
                # Already measured on the new digest: the higher value stays.
                MeasuredRolePeak(
                    content_sha256=item.content_sha256,
                    role="entrypoint",
                    peak_bytes=800,
                ),
            ]
        )
        before = {
            row.id: (
                row.document,
                row.content_digest,
                row.artifact_key,
                row.execution_key,
                row.state,
            )
            for row in session.scalars(select(CatalogDocumentRevision))
        }
        runs_before = session.scalars(select(RecipeRun)).all()

    request = ManagedCatalogSyncRequest(
        request_key=str(uuid.uuid4()),
        trigger=CatalogSyncTrigger.MANUAL,
        actor="test",
    )
    _sync(sessions, service, reader).sync(request)

    with sessions() as session:
        rows = session.scalars(select(CatalogDocumentRevision)).all()
        by_id = {row.id: row for row in rows}
        # The stored 3.x rows are intact: nothing rewrote, rebuilt or re-keyed them.
        for revision_id, kept in before.items():
            row = by_id[revision_id]
            assert (
                row.document,
                row.content_digest,
                row.artifact_key,
                row.execution_key,
                row.state,
            ) == kept
        # One new head revision per Model and recipe, on the same document root.
        new_model_rows = [
            row
            for row in rows
            if row.kind == "model"
            and row.id != legacy_model_id
            and row.document_id == by_id[legacy_model_id].document_id
        ]
        new_recipe_rows = [
            row
            for row in rows
            if row.kind == "recipe"
            and row.id != legacy_recipe_id
            and row.document_id == by_id[legacy_recipe_id].document_id
        ]
        assert len(new_model_rows) == len(new_recipe_rows) == 1
        # No re-download: the artifact key does not move with the facts.
        assert new_model_rows[0].artifact_key == str(model_key)  # no re-download
        assert new_model_rows[0].content_digest == document_sha256(entry.document)
        assert new_recipe_rows[0].content_digest == item.content_sha256
        for kind, new in (("model", new_model_rows[0]), ("recipe", new_recipe_rows[0])):
            head = session.scalar(
                select(CatalogDocumentHead.active_revision_id).where(
                    CatalogDocumentHead.kind == new.kind,
                    CatalogDocumentHead.publisher == new.publisher,
                    CatalogDocumentHead.slug == new.slug,
                )
            )
            assert head == new.id, kind
        # Measured wins: the re-keyed recipe starts with what was measured,
        # when the execution is the same (pins only, same model bytes).
        assert observed_role_peaks(session, item.content_sha256) == (
            {"entrypoint": 800, "worker": 900} if carried else {"entrypoint": 800}
        )
        assert observed_role_peaks(session, legacy_recipe_digest) == {
            "entrypoint": 700,
            "worker": 900,
        }
        # Running workloads are not touched by the sync.
        assert session.scalars(select(RecipeRun)).all() == runs_before


def test_a_stored_3x_model_projection_is_rederived_without_the_moved_facts(
    tmp_path: Path,
) -> None:
    sessions, _service, reader, _item = _fixture(tmp_path)
    document = reader.snapshot.catalog_entities[0].document
    new_model = read_model(document)
    legacy = _legacy_model(document)
    with sessions.begin() as session:
        revision_id = _store(
            session,
            legacy,
            projected=_legacy_projection(new_model, legacy),
            artifact_key="1" * 64,
        )
    # Read time already repairs it (the projection forbids the retired fields) ...
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, revision_id)
        assert row is not None
        with pytest.raises(ValueError):
            ModelRevisionProjection.model_validate(row.projected)
        repaired = read_catalog_projection(row)
        assert isinstance(repaired, ModelRevisionProjection)
        assert "capabilities" not in repaired.model_dump()
    # ... and the sync's pass persists the repair once, then finds nothing to do.
    CatalogEntityService(sessions, clock=lambda: NOW).refresh_model_projections()
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, revision_id)
        assert row is not None
        stored = dict(row.projected)
        assert "capabilities" not in stored and "modalities" not in stored
        assert stored["artifact_count"] == len(new_model.files)
        assert row.document == legacy and row.artifact_key == "1" * 64
    CatalogEntityService(sessions, clock=lambda: NOW).refresh_model_projections()
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, revision_id)
        assert row is not None and dict(row.projected) == stored


def test_a_write_refuses_a_model_that_still_carries_its_facts(tmp_path: Path) -> None:
    sessions, _service, reader, _item = _fixture(tmp_path)
    document = reader.snapshot.catalog_entities[0].document
    entities = CatalogEntityService(sessions, clock=lambda: NOW)
    for field in ("capabilities", "modalities", "license"):
        with pytest.raises(CatalogValidationError) as refused:
            create_draft(
                entities,
                {**copy.deepcopy(document), field: _legacy_model(document)[field]},
                actor="test",
            )
        assert refused.value.code == CatalogCode.DOCUMENT_INVALID


def test_a_retained_older_release_changes_and_withdraws_nothing(tmp_path: Path) -> None:
    """The window where this Controller runs but its major is not published yet.

    The reader hands back the verified older release as an empty snapshot; the
    catalog the Controller stores (what its routes and workloads run on) stays
    active and visible, and nothing is withdrawn.
    """
    sessions, service, reader, _item = _fixture(tmp_path)
    request = ManagedCatalogSyncRequest(
        request_key=str(uuid.uuid4()),
        trigger=CatalogSyncTrigger.AUTOMATIC,
        actor="test",
    )
    _sync(sessions, service, reader).sync(request)
    with sessions() as session:
        before = {
            row.id: (row.state, row.content_digest, row.projected)
            for row in session.scalars(select(CatalogDocumentRevision))
        }
        heads = session.scalars(select(CatalogDocumentHead.active_revision_id)).all()
    assert before and all(state == "active" for state, *_ in before.values())

    reader.snapshot = RecipeLibrarySnapshot(
        commit="a" * 40, items=(), version="3.2.0", updated_at=NOW
    )
    result = _sync(sessions, service, reader).sync(
        request.model_copy(update={"request_key": str(uuid.uuid4())})
    )
    assert result.state == "current"
    assert result.withdrawn_count == 0 and not result.problems
    with sessions() as session:
        assert {
            row.id: (row.state, row.content_digest, row.projected)
            for row in session.scalars(select(CatalogDocumentRevision))
        } == before
        assert (
            session.scalars(select(CatalogDocumentHead.active_revision_id)).all()
            == heads
        )


def test_the_published_status_of_a_carried_proof_is_readable() -> None:
    """The generated contract knows the quality note the split's twins carry."""
    index = json.loads((recipe_library_root() / "catalog-index.json").read_text())
    carried = [
        entry["qualification"]
        for entry in index["recipes"]
        if QualificationQualityNote.CARRIED_MODEL_FACTS
        in json.dumps(entry.get("qualification"))
    ]
    assert carried, "the 4.0 index carries proofs moved by the Model facts split"
    for status in carried:
        PublishedQualificationStatusView.model_validate(status)
