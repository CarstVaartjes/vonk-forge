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
from vonk_control.source_policy import dockerfile_base_images

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


def test_native_recipe_builds_declare_the_exact_offline_base_image_supply() -> None:
    expected = {
        "ds4": (
            "nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04@sha256:5c36750138dc1447a17dafbb397674f167d3b44ce18d9160d769df114577b35d",
            "nvcr.io/nvidia/cuda:13.0.1-runtime-ubuntu24.04@sha256:36050649ad1acc5d3de2c26620191c25850fb12a5771b6c22996033003d952e4",
        ),
        "mia-vllm": (
            "ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8",
        ),
    }
    for adapter, references in expected.items():
        payload = (ROOT / "adapters/deepseek" / adapter / "Dockerfile").read_bytes()
        authorities = dockerfile_base_images(payload)
        assert tuple(item["reference"] for item in authorities) == references
        assert tuple(item["manifest_digest"] for item in authorities) == tuple(
            reference.rsplit("@", 1)[1] for reference in references
        )


def test_synthetic_development_recipe_compiles_through_the_native_runtime_path() -> None:
    fixture = ROOT / "control/tests/fixtures/recipes/dev-http-smoke"
    recipe = _load(fixture / "recipe.json")
    base_images = dockerfile_base_images((fixture / "context/Dockerfile").read_bytes())
    expected_base_image = (
        "docker.io/library/python:3.12.11-slim-bookworm@"
        "sha256:9bb659dc6d5218917236f3711e866a5634bb4c2f208de9d4533aa4863f57c1d3"
    )
    assert tuple(image["reference"] for image in base_images) == (expected_base_image,)
    assert recipe["build"]["resources"]["download_bytes"] == 256 * 1024 * 1024
    entities = {
        document["kind"]: document
        for path in sorted((fixture / "entities").glob("*.json"))
        if (document := _load(path))["kind"]
        in {"model-version", "execution-harness", "runtime-distribution"}
    }
    assert entities["runtime-distribution"]["image"] == expected_base_image
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
