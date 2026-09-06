from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_repository import CatalogRepository, sensitive_document_path
from vonk_control.models import Base

from tests.recipe_library_source import recipe_library_root

RECIPE_LIBRARY_ROOT = recipe_library_root()


def test_repository_allocates_canonical_revision_numbers() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        service = CatalogEntityService(
            session,
            clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        )
        index = json.loads(
            (RECIPE_LIBRARY_ROOT / "catalog-index.json").read_text(encoding="utf-8")
        )
        document = index["catalog_entities"][0]["document"]
        draft = service.create_draft(document, actor="test")
        service.resolve(draft.id, actor="test")
        repository = CatalogRepository()
        assert repository.next_revision_number(session, draft.document_id) == 2
        assert repository.active_revision(session, draft.document_id) is not None


def test_repository_redacts_sensitive_source_and_reports_only_paths() -> None:
    repository = CatalogRepository()
    source = {"token": "secret", "nested": [{"password": "hidden", "ok": 1}]}
    assert repository.redact_source(source) == {
        "token": "[REDACTED]",
        "nested": [{"password": "[REDACTED]", "ok": 1}],
    }
    assert sensitive_document_path(source) == "$.token"
