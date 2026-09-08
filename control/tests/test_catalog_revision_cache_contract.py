from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_revision_contract import (
    CatalogRevisionContractError,
    read_catalog_document,
    read_catalog_projection,
)
from vonk_control.model_cache import ModelCacheService, ModelCacheStorageError
from vonk_control.model_cache_contract import parse_model_cache_payload
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
        assert projection.artifact_count == len(raw["files"])
        stored.projected = {**stored.projected, "artifact_count": "malformed"}
        with pytest.raises(CatalogRevisionContractError):
            read_catalog_projection(stored)


def test_model_cache_operation_payload_round_trips_through_database_and_rejects_malformed_state(
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
        assert parsed.schema_version == 2
        assert parsed.source_policy == "nas-first"
        assert parsed.manifest.model_content_sha256 is not None
        valid_payload = dict(stored.payload)
        stored.payload = {"schema_version": 2, "source_policy": "nas-first"}
        session.commit()

    with pytest.raises(ModelCacheStorageError, match="payload is invalid"):
        cache.get_operation(operation.id)

    with sessions.begin() as session:
        stored = session.get(ModelCacheOperation, operation.id)
        assert stored is not None
        stored.payload = valid_payload
        stored.progress = {"schema_version": 2, "phase": "queued"}
    with pytest.raises(ModelCacheStorageError, match="progress is invalid"):
        cache.get_operation(operation.id)
    cache.close()
