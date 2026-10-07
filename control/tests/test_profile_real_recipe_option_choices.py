"""Stale saved option choices never block a profile, on the real recipe library.

Recipe refreshes add, rename and drop options (a two-Spark GLM recipe gained a
``drafter`` value, others dropped per-request options), while a saved profile
re-submits whatever it stored on every edit.
"""

from __future__ import annotations

import json

import pytest
from vonk_control.fleet_profiles import _effective_option_choices
from vonk_forge_contracts import RecipeDefinition

from cluster_profiles.control_client import (
    ControlClientError,
    validate_control_document,
)

from .recipe_library_source import recipe_library_root

ROOT = recipe_library_root()


def _recipes() -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "recipes").glob("*.json"))
    ]


def test_every_library_recipe_replaces_stale_choices_with_its_defaults() -> None:
    recipes = _recipes()
    assert recipes
    optioned = 0
    for document in recipes:
        recipe = RecipeDefinition.model_validate(document)
        declared = {option.name: option for option in recipe.options}
        defaults, notes = _effective_option_choices(recipe, {})
        assert notes == []
        assert set(defaults) == set(declared)

        stale = {name: "value-the-recipe-never-offered" for name in declared}
        stale["option-the-recipe-never-declared"] = "x"
        effective, notes = _effective_option_choices(recipe, stale)

        # Everything stale is replaced by the default, each replacement named.
        assert effective == defaults
        assert len(notes) == len(stale)

        # A choice the recipe still offers survives next to a stale one.
        for name, option in declared.items():
            offered = [choice.value for choice in option.choices]
            kept = offered[-1]
            effective, _notes = _effective_option_choices(recipe, {**stale, name: kept})
            assert effective[name] == kept
            optioned += 1
    assert optioned


# The three recipes whose profile edit failed in the hardware sweep. Each
# declares a reviewed service alias with capitals, which the sweep passes as
# ``--as``; the endpoint-alias contract is lowercase.
_SWEEP_RECIPES = (
    "glm-5-3-flash-exl3-dflash2-tensorfold-mia-dual",
    "qwen3-8-27b-nvfp4-dflash2-eugr-vllm-dual",
    "qwen3-coder-next-fp8-eugr-vllm-dual",
)


@pytest.mark.parametrize("slug", _SWEEP_RECIPES)
def test_the_reviewed_service_alias_is_refused_naming_its_field(slug: str) -> None:
    definition = json.loads(
        (ROOT / "qualification" / "recipes" / f"{slug}.json").read_text("utf-8")
    )
    # The library has since made its reviewed aliases valid; the sweep's
    # historical failure is the capitalized spelling, so refuse that form.
    alias = definition["service_recipes"]["alias"].upper()
    document = {
        "name": "sweep",
        "expected_revision": 3,
        "assignments": [
            {
                "recipe_selector": f"vonk-forge/{slug}",
                "spark_ids": ["spk_" + "a" * 32, "spk_" + "b" * 32],
                "assignment_name": alias,
            }
        ],
    }

    with pytest.raises(ControlClientError) as refused:
        validate_control_document("FleetProfileInput", document)

    assert "assignments[0].assignment_name" in str(refused.value)
    assert "pattern" in str(refused.value)
    assert alias not in str(refused.value)
    # The same document with the alias in the contract's lowercase form passes.
    document["assignments"][0]["assignment_name"] = alias.lower().replace(".", "-")
    validate_control_document("FleetProfileInput", document)
