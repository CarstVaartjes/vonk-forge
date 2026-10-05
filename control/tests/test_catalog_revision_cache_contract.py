from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_revision_contract import (
    CatalogRevisionContractError,
    ModelRevisionProjection,
    read_catalog_document,
    read_catalog_projection,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import (
    ModelCacheDownloadPayload,
    parse_model_cache_payload,
)
from vonk_control.models import CatalogDocumentRevision, ModelCacheOperation
from vonk_forge_contracts import ModelDefinition

from .test_model_cache_availability_regressions import _artifact, _database, _start


def test_catalog_revision_projection_is_persisted_and_read_as_canonical_model(
    tmp_path: Path,
) -> None:
    sessions = _database(tmp_path)
    raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    revision = CatalogEntityService(
        sessions, clock=lambda: datetime.now(UTC)
    ).create_draft(raw, actor="test")

    with sessions() as session:
        stored = session.get(CatalogDocumentRevision, revision.id)
        assert stored is not None
        assert isinstance(read_catalog_document(stored), ModelDefinition)
        projection = read_catalog_projection(stored)
        assert isinstance(projection, ModelRevisionProjection)
        assert projection.artifact_count == len(raw["files"])
        stored.projected = {**stored.projected, "artifact_count": "malformed"}
        with pytest.raises(CatalogRevisionContractError):
            read_catalog_projection(stored)


def test_model_cache_operation_payload_round_trips_through_database_and_reads_malformed_state_as_unknown(
    tmp_path: Path,
) -> None:
    sessions = _database(tmp_path)
    cache = ModelCacheService(
        sessions,
        tmp_path / "cache",
        reserve_bytes=0,
        fixture_sources=True,
    )
    artifact = _artifact("contract", b"contract payload")
    operation = _start(cache, [artifact], "00000000-0000-4000-8000-000000000901")

    with sessions() as session:
        stored = session.get(ModelCacheOperation, operation.id)
        assert stored is not None
        parsed = parse_model_cache_payload("download", stored.payload)
        assert isinstance(parsed, ModelCacheDownloadPayload)
        assert parsed.schema_version == 2
        assert parsed.source_policy == "nas-first"
        assert parsed.manifest.model_content_sha256 is not None
        valid_payload = dict(stored.payload)
        stored.payload = {"schema_version": 2, "source_policy": "nas-first"}
        session.commit()

    # A damaged document is unknown, not a failure to read: the row still renders
    # (from its own columns) and its other bookkeeping is untouched.
    unreadable = cache.get_operation(operation.id)
    assert unreadable.id == operation.id
    assert unreadable.artifact_set_sha256 == operation.artifact_set_sha256
    assert unreadable.blockers == ()

    with sessions.begin() as session:
        stored = session.get(ModelCacheOperation, operation.id)
        assert stored is not None
        stored.payload = valid_payload
        stored.progress = {"schema_version": 2, "phase": "queued"}
    # A damaged progress measurement restarts from zero; the next sample rebuilds it.
    restarted = cache.get_operation(operation.id)
    assert restarted.progress["phase"] == "queued"
    assert restarted.progress["downloaded_bytes"] == 0
    cache.close()
