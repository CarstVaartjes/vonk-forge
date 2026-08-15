from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_contract import (
    catalog_content_sha256,
    validate_catalog_document,
)
from vonk_control.catalog_service import CatalogService
from vonk_control.models import Base
from vonk_control.recipe_contract import recipe_content_sha256, validate_recipe

ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROOTS = (
    ROOT / "config/catalog/development",
    ROOT / "config/recipes/development",
)
ENTITY_PATHS = (
    ROOT / "config/model-groups/deepseek-flash.json",
    ROOT / "config/models/deepseek-v4-flash-0731.json",
    ROOT / "config/model-versions/deepseek-v4-flash-0731-ds4.json",
    ROOT / "config/model-versions/deepseek-v4-flash-0731-official.json",
    ROOT / "config/execution-harnesses/ds4.json",
    ROOT / "config/execution-harnesses/vllm.json",
    ROOT / "config/runtime-distributions/ds4-spark.json",
    ROOT / "config/runtime-distributions/anemll-vllm-mia.json",
    ROOT / "config/patch-bundles/mia-deepseek-v4-flash-0731.json",
)
RECIPE_PATHS = (
    ROOT / "config/recipes/deepseek-v4-flash-0731-ds4-single.json",
    ROOT / "config/recipes/deepseek-v4-flash-0731-mia-dual.json",
)


def _document(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _service(tmp_path: Path) -> CatalogService:
    engine = create_engine(f"sqlite:///{tmp_path / 'native-v1.sqlite'}")
    Base.metadata.create_all(engine)
    return CatalogService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: datetime(2026, 8, 15, tzinfo=UTC),
        cursors=TokenCodec(b"d" * 32).cursor_codec(),
    )


def _resolve_native_entities(service: CatalogService) -> None:
    for path in ENTITY_PATHS:
        document = _document(path)
        validate_catalog_document(document)
        draft = service.entities.create_draft(document, actor="admin")
        resolved = service.entities.resolve(draft.entity_id, actor="admin")
        assert resolved.content_sha256 == catalog_content_sha256(document)


def test_prototype_development_catalog_and_recipe_trees_are_absent() -> None:
    assert all(not root.exists() for root in LEGACY_ROOTS)


def test_native_deepseek_entities_resolve_in_exact_dependency_order(
    tmp_path: Path,
) -> None:
    _resolve_native_entities(_service(tmp_path))


@pytest.mark.parametrize("recipe_path", RECIPE_PATHS, ids=lambda path: path.stem)
def test_native_deepseek_recipes_resolve_through_the_catalog_service(
    tmp_path: Path, recipe_path: Path
) -> None:
    service = _service(tmp_path)
    _resolve_native_entities(service)
    recipe = _document(recipe_path)
    validate_recipe(recipe)
    assert service.resolve_recipe_revision(recipe, actor="admin") == (
        recipe_content_sha256(recipe)
    )
