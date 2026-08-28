from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.library_service import LibraryRecipeConflict, LibraryRecipeService
from vonk_control.models import Base

ROOT = Path(__file__).resolve().parents[2]


def _document() -> dict[str, object]:
    return json.loads(
        (ROOT / "control/tests/fixtures/global/recipe-v1-minimal.json").read_text()
    )


def _service() -> LibraryRecipeService:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return LibraryRecipeService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: datetime(2026, 8, 18, tzinfo=UTC),
    )


def _actor() -> dict[str, object]:
    return {"subject": "operator", "capabilities": ["library:write"]}


def test_save_edit_and_get_preserve_exact_digest_and_immutable_history() -> None:
    service = _service()
    first = service.save(slug="custom-recipe", content=_document(), actor=_actor())
    changed = _document()
    changed["metadata"]["title"] = "Changed"
    second = service.edit(first.recipe_id, changed, _actor())

    assert first.revision_number == 1
    assert second.revision_number == 2
    assert first.content_digest != second.content_digest
    assert service.get(first.recipe_id, 1).content["metadata"]["title"] != "Changed"
    assert [item.revision_number for item in service.list()] == [1, 2]


def test_duplicate_slug_and_content_are_rejected() -> None:
    service = _service()
    first = service.save(slug="custom-recipe", content=_document(), actor=_actor())
    with pytest.raises(LibraryRecipeConflict, match="slug"):
        service.save(slug="custom-recipe", content=_document(), actor=_actor())
    with pytest.raises(LibraryRecipeConflict, match="unchanged"):
        service.edit(first.recipe_id, _document(), _actor())


def test_write_capability_is_required_and_remove_only_removes_recipe() -> None:
    service = _service()
    with pytest.raises(PermissionError):
        service.save(
            slug="custom-recipe", content=_document(), actor={"subject": "viewer"}
        )
    saved = service.save(slug="custom-recipe", content=_document(), actor=_actor())
    service.remove(saved.recipe_id, _actor())
    with pytest.raises(KeyError):
        service.get(saved.recipe_id)
