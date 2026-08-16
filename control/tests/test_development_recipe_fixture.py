from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from vonk_control.catalog_contract import (
    catalog_content_sha256,
    validate_catalog_document,
)
from vonk_control.recipe_contract import validate_recipe
from vonk_control.recipe_runtime_specs import compile_runtime_spec

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


def test_synthetic_development_recipe_compiles_through_the_native_runtime_path() -> None:
    fixture = ROOT / "control/tests/fixtures/recipes/dev-http-smoke"
    recipe = _load(fixture / "recipe.json")
    entities = {
        document["kind"]: document
        for path in sorted((fixture / "entities").glob("*.json"))
        if (document := _load(path))["kind"]
        in {"model-version", "execution-harness", "runtime-distribution"}
    }
    resolved = {
        "model_version": SimpleNamespace(
            content_sha256=recipe["model"]["content_sha256"]
        ),
        "harness": SimpleNamespace(
            document=entities["execution-harness"],
            content_sha256=recipe["execution"]["harness"]["content_sha256"],
        ),
        "runtime_distribution": SimpleNamespace(
            document=entities["runtime-distribution"],
            content_sha256=recipe["runtime"]["distribution"]["content_sha256"],
        ),
        "patch_bundle": None,
    }

    spec = compile_runtime_spec(
        recipe,
        resolved_entities=resolved,
        parameters={},
        role="entrypoint",
        rank=0,
        recipe_build_id="00000000-0000-4000-8000-000000000001",
        image_digest="sha256:" + "d" * 64,
    )

    assert spec["runtime"]["entrypoint"] == [
        "/opt/vonk/bin/vllm",
        "serve",
        "/models",
        "--max-model-len",
        "1",
        "--tensor-parallel-size",
        "1",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert spec["security"]["devices"] == ["nvidia.com/gpu=all"]
    assert spec["security"]["mounts"] == [
        {"source": "model", "target": "/models", "read_only": True},
        {"source": "outputs", "target": "/outputs", "read_only": False},
    ]
