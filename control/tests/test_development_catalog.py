from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_contract import (
    CatalogKind,
    catalog_content_sha256,
    parse_catalog_reference,
    validate_catalog_document,
)
from vonk_control.catalog_service import CatalogService
from vonk_control.models import Base
from vonk_control.recipe_contract import (
    recipe_content_sha256,
    recipe_references,
    validate_recipe,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = ROOT / "config/catalog/development"
RECIPE_ROOT = ROOT / "config/recipes/development"

CATALOG_RECIPES = {
    "dev-http-smoke": (
        ROOT / "control/tests/fixtures/recipes/dev-http-smoke/recipe.json",
    ),
    "model-smoke": (
        RECIPE_ROOT / "model-smoke-single.json",
        RECIPE_ROOT / "model-smoke-pair.json",
    ),
    "mia-deepseek-v4-flash": (RECIPE_ROOT / "mia-deepseek-v4-flash.json",),
}


def _document(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _entity_references(document: Mapping[str, object]):
    kind = CatalogKind(document["kind"])
    if kind is CatalogKind.MODEL:
        fields = (("model_group", CatalogKind.MODEL_GROUP),)
    elif kind is CatalogKind.MODEL_VERSION:
        fields = (("model", CatalogKind.MODEL),)
    elif kind is CatalogKind.RUNTIME_DISTRIBUTION:
        fields = (("implements_harness", CatalogKind.EXECUTION_HARNESS),)
    elif kind is CatalogKind.PATCH_BUNDLE:
        fields = (("applies_to", CatalogKind.RUNTIME_DISTRIBUTION),)
    else:
        fields = ()
    return tuple(
        parse_catalog_reference(document[field], expected_kind=expected_kind)
        for field, expected_kind in fields
    )


def test_development_catalog_documents_resolve_in_checked_in_dependency_order() -> None:
    for catalog_name, recipe_paths in CATALOG_RECIPES.items():
        resolved: set[tuple[str, str, str, str]] = set()
        entity_paths = sorted((CATALOG_ROOT / catalog_name).glob("*.json"))
        assert len(entity_paths) == (
            6 if catalog_name == "mia-deepseek-v4-flash" else 5
        )

        for entity_path in entity_paths:
            document = _document(entity_path)
            validate_catalog_document(document)
            for reference in _entity_references(document):
                assert reference.portable_identity in resolved
            identity = document["identity"]
            assert isinstance(identity, Mapping)
            resolved.add(
                (
                    str(document["kind"]),
                    str(identity["publisher"]),
                    str(identity["slug"]),
                    catalog_content_sha256(document),
                )
            )

        for recipe_path in recipe_paths:
            recipe = _document(recipe_path)
            validate_recipe(recipe)
            assert {
                reference.portable_identity for reference in recipe_references(recipe)
            } <= resolved


def test_model_acceptance_recipes_have_phase_exact_native_topologies() -> None:
    single = _document(RECIPE_ROOT / "model-smoke-single.json")
    pair = _document(RECIPE_ROOT / "model-smoke-pair.json")

    assert single["identity"]["slug"] == "development-deepseek-smoke-single"
    assert single["topology"]["name"] == "solo"
    assert single["topology"]["node_count"] == 1
    assert sum(role["count"] for role in single["topology"]["roles"]) == 1
    assert pair["identity"]["slug"] == "development-deepseek-smoke-pair"
    assert pair["topology"]["name"] == "pair"
    assert pair["topology"]["node_count"] == 2
    assert sum(role["count"] for role in pair["topology"]["roles"]) == 2


@pytest.mark.parametrize("catalog_name", tuple(CATALOG_RECIPES))
def test_development_recipes_resolve_through_the_catalog_service(
    tmp_path: Path, catalog_name: str
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / f'{catalog_name}.sqlite'}")
    Base.metadata.create_all(engine)
    service = CatalogService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: datetime(2026, 8, 15, tzinfo=UTC),
        cursors=TokenCodec(b"d" * 32).cursor_codec(),
    )
    for entity_path in sorted((CATALOG_ROOT / catalog_name).glob("*.json")):
        draft = service.entities.create_draft(_document(entity_path), actor="admin")
        resolved = service.entities.resolve(draft.entity_id, actor="admin")
        assert resolved.content_sha256 == catalog_content_sha256(resolved.document)

    for recipe_path in CATALOG_RECIPES[catalog_name]:
        recipe = _document(recipe_path)
        assert service.resolve_recipe_revision(recipe, actor="admin") == (
            recipe_content_sha256(recipe)
        )
