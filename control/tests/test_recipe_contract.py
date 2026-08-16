from __future__ import annotations

import json
from pathlib import Path

import pytest
from vonk_control.catalog_contract import CatalogKind
from vonk_control.recipe_contract import (
    RecipeContractError,
    canonical_recipe,
    parse_recipe_json,
    recipe_content_sha256,
    recipe_references,
    recipe_topology,
    validate_recipe,
)

ROOT = Path(__file__).resolve().parents[2]
RECIPE_FIXTURE = ROOT / "control/tests/fixtures/global/recipe-v1-minimal.json"


def recipe_document() -> dict[str, object]:
    return json.loads(RECIPE_FIXTURE.read_text())


def test_recipe_has_one_topology_and_exact_bindings() -> None:
    document = parse_recipe_json(RECIPE_FIXTURE.read_bytes())

    validate_recipe(document)

    assert recipe_topology(document)["node_count"] == 1
    assert {item.kind for item in recipe_references(document)} == {
        CatalogKind.MODEL_VERSION,
        CatalogKind.EXECUTION_HARNESS,
        CatalogKind.RUNTIME_DISTRIBUTION,
    }


def test_recipe_patch_bundle_is_nullable_and_part_of_exact_references() -> None:
    document = recipe_document()
    document["execution"]["patch_bundle"] = {
        "kind": "patch-bundle",
        "publisher": "vonk-forge",
        "slug": "vllm-fix",
        "content_sha256": "e" * 64,
    }

    validate_recipe(document)

    references = recipe_references(document)
    assert [reference.kind for reference in references] == [
        CatalogKind.MODEL_VERSION,
        CatalogKind.EXECUTION_HARNESS,
        CatalogKind.RUNTIME_DISTRIBUTION,
        CatalogKind.PATCH_BUNDLE,
    ]


def test_recipe_supports_exact_auxiliary_model_versions() -> None:
    document = recipe_document()
    dependency = {
        "kind": "model-version",
        "publisher": "vonk-forge",
        "slug": "synthetic-auxiliary-fp16",
        "content_sha256": "f" * 64,
    }
    document["dependencies"] = [dependency]

    validate_recipe(document)

    dependencies = recipe_model_dependencies(document)
    assert len(dependencies) == 1
    assert dependencies[0].portable_identity == (
        "model-version",
        "vonk-forge",
        "synthetic-auxiliary-fp16",
        "f" * 64,
    )
    assert [reference.kind for reference in recipe_references(document)] == [
        CatalogKind.MODEL_VERSION,
        CatalogKind.EXECUTION_HARNESS,
        CatalogKind.RUNTIME_DISTRIBUTION,
        CatalogKind.MODEL_VERSION,
    ]


def test_recipe_rejects_the_primary_model_as_an_auxiliary_dependency() -> None:
    document = recipe_document()
    document["dependencies"] = [document["model"]]

    with pytest.raises(RecipeContractError, match="primary model version"):
        validate_recipe(document)


def test_recipe_keeps_patch_binding_distinct_from_auxiliary_models() -> None:
    document = recipe_document()
    document["execution"]["patch_bundle"] = {
        "kind": "patch-bundle",
        "publisher": "vonk-forge",
        "slug": "vllm-fix",
        "content_sha256": "e" * 64,
    }
    document["dependencies"] = [
        {
            "kind": "model-version",
            "publisher": "vonk-forge",
            "slug": "synthetic-auxiliary-fp16",
            "content_sha256": "f" * 64,
        }
    ]

    validate_recipe(document)

    assert [reference.kind for reference in recipe_references(document)] == [
        CatalogKind.MODEL_VERSION,
        CatalogKind.EXECUTION_HARNESS,
        CatalogKind.RUNTIME_DISTRIBUTION,
        CatalogKind.PATCH_BUNDLE,
        CatalogKind.MODEL_VERSION,
    ]


def test_job_interface_can_declare_a_read_only_input_contract() -> None:
    document = recipe_document()
    document["interfaces"] = [
        {
            "adapter": "image-job",
            "path": "/outputs",
            "input": {
                "path": "/inputs",
                "required": True,
                "media_types": ["image/png", "image/jpeg"],
                "max_bytes": 32 * 1024 * 1024,
            },
        }
    ]
    document["validation"]["validators"] = [
        {"interface": "image-job", "checks": ["artifact.mime.image-png"]}
    ]
    document["runtime"]["security"]["mounts"] = [
        {"source": "model", "target": "/models", "read_only": True},
        {"source": "inputs", "target": "/inputs", "read_only": True},
        {"source": "outputs", "target": "/outputs", "read_only": False},
    ]

    validate_recipe(document)


def test_recipe_rejects_an_input_contract_without_a_matching_mount() -> None:
    document = recipe_document()
    document["interfaces"] = [
        {
            "adapter": "image-job",
            "path": "/outputs",
            "input": {
                "path": "/inputs",
                "required": True,
                "media_types": ["image/png"],
                "max_bytes": 1024,
            },
        }
    ]
    document["validation"]["validators"] = [
        {"interface": "image-job", "checks": ["artifact.mime.image-png"]}
    ]

    with pytest.raises(RecipeContractError, match="read-only /inputs mount"):
        validate_recipe(document)


def test_recipe_rejects_filesystem_inputs_on_an_openai_interface() -> None:
    document = recipe_document()
    document["interfaces"][0]["input"] = {
        "path": "/inputs",
        "required": True,
        "media_types": ["image/png"],
        "max_bytes": 1024,
    }

    with pytest.raises(RecipeContractError, match="OpenAI interfaces"):
        validate_recipe(document)



def test_recipe_digest_changes_with_patch_identity() -> None:
    unpatched = recipe_document()
    patched = recipe_document()
    patched["execution"]["patch_bundle"] = {
        "kind": "patch-bundle",
        "publisher": "vonk-forge",
        "slug": "vllm-fix",
        "content_sha256": "e" * 64,
    }

    assert recipe_content_sha256(unpatched) != recipe_content_sha256(patched)


def test_unknown_root_shape_is_rejected() -> None:
    document = recipe_document()
    document["unexpected_root"] = []

    with pytest.raises(RecipeContractError, match="additionalProperties"):
        validate_recipe(document)


def test_recipe_parser_rejects_duplicate_keys_and_floats() -> None:
    with pytest.raises(RecipeContractError, match="duplicate object key"):
        parse_recipe_json(b'{"identity":{},"identity":{}}')
    with pytest.raises(RecipeContractError, match="floats are not permitted"):
        parse_recipe_json(b'{"value":1.5}')


def test_recipe_canonicalization_is_stable() -> None:
    document = {"z": 1, "a": [True, None]}

    assert canonical_recipe(document) == b'{"a":[true,null],"z":1}'
    assert recipe_content_sha256(document) == (
        "ca6da02fba3343778761e7785f2b55f7fb17b36ce16eee3492dc392fa7c9deaa"
    )


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("parameters", 0, "default"), "32768", "recipe.parameter_type"),
        (("parameters", 0, "minimum"), 131073, "recipe.parameter_bounds"),
        (("topology", "parallelism", "tensor"), 2, "recipe.topology_parallelism"),
        (("topology", "fabric", "connectivity"), "connected", "recipe.topology_fabric"),
    ],
)
def test_recipe_rejects_invalid_cross_field_values(
    path: tuple[str | int, ...], value: object, code: str
) -> None:
    document = recipe_document()
    target: object = document
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(RecipeContractError) as raised:
        validate_recipe(document)

    assert raised.value.code == code
