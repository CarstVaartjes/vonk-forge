"""The stored image-availability identity reads its recipe with the tolerant reader."""

from __future__ import annotations

import json
from importlib import resources

from vonk_control.recipe_image_availability.reader_contract import (
    StoredAvailabilityIdentity,
)
from vonk_forge_contracts import read_recipe


def test_a_stored_recipe_with_a_retired_field_reads_and_damage_is_absent() -> None:
    # Wrong implementation: a strict TypeAdapter(RecipeDefinition) loses a recipe
    # an older contract wrote, so the job could not be recovered.
    recipe = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text()
    )
    recipe["identity"]["retired"] = "written by an older contract"
    stored = StoredAvailabilityIdentity.model_validate({"recipe": recipe})
    assert stored.recipe == read_recipe(recipe)
    damaged = StoredAvailabilityIdentity.model_validate({"recipe": {"kind": "recipe"}})
    assert damaged.recipe is None
