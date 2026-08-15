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


def test_prototype_multi_profile_shape_is_rejected() -> None:
    document = recipe_document()
    document["deployment_profiles"] = []

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
