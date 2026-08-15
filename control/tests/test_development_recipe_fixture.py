from __future__ import annotations

import json
from pathlib import Path

from vonk_control.catalog_contract import (
    catalog_content_sha256,
    validate_catalog_document,
)
from vonk_control.recipe_contract import validate_recipe

ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT = ROOT / "config/recipes/development"
RECIPES = (
    ROOT / "config/recipes/deepseek-v4-flash-0731-ds4-single.json",
    ROOT / "config/recipes/deepseek-v4-flash-0731-mia-dual.json",
)
KIND_ROOT = {
    "model-group": "model-groups",
    "model": "models",
    "model-version": "model-versions",
    "execution-harness": "execution-harnesses",
    "runtime-distribution": "runtime-distributions",
    "patch-bundle": "patch-bundles",
}


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve(reference: dict[str, object]) -> dict[str, object]:
    document = _load(
        ROOT
        / "config"
        / KIND_ROOT[str(reference["kind"])]
        / f"{reference['slug']}.json"
    )
    validate_catalog_document(document)
    assert catalog_content_sha256(document) == reference["content_sha256"]
    return document


def test_prototype_development_recipe_catalog_is_deleted() -> None:
    assert not DEVELOPMENT.exists()
    assert not any(
        fragment in path.as_posix()
        for path in ROOT.rglob("*")
        for fragment in ("ds4_smoke", "mia_dsv4_flash")
    )


def test_native_deepseek_recipes_and_all_references_are_strict_and_hash_locked() -> None:
    for recipe_path in RECIPES:
        recipe = _load(recipe_path)
        validate_recipe(recipe)
        version = _resolve(recipe["model"])
        model = _resolve(version["model"])
        _resolve(model["model_group"])
        _resolve(recipe["execution"]["harness"])
        _resolve(recipe["runtime"]["distribution"])
        if recipe["execution"]["patch_bundle"] is not None:
            _resolve(recipe["execution"]["patch_bundle"])


def test_native_recipes_have_no_startup_mutation_or_network_fetch_hooks() -> None:
    for recipe_path in RECIPES:
        recipe = _load(recipe_path)
        assert recipe["runtime"]["lifecycle"]["pre_start"] == []
        environment = {
            item["name"]: str(item["value"])
            for item in recipe["runtime"]["environment"]
        }
        assert environment["HF_HUB_OFFLINE"] == "1"
        assert not any("PATCH" in name or "DOWNLOAD" in name for name in environment)
