"""Display of a fixture document, as the signed index would publish it."""

from __future__ import annotations

from collections.abc import Mapping

from vonk_control.recipe_library_types import CatalogModelEntry
from vonk_forge_contracts import (
    ModelDisplay,
    RecipeDisplay,
    model_display_of,
    published_json,
    read_model,
    read_recipe,
    recipe_display_of,
)


def display_of_recipe(document: Mapping[str, object]) -> RecipeDisplay:
    return recipe_display_of(read_recipe(document))


def display_of_model(document: Mapping[str, object]) -> ModelDisplay:
    return model_display_of(read_model(document))


def model_entry(document: Mapping[str, object]) -> CatalogModelEntry:
    return CatalogModelEntry(dict(document), display_of_model(document))


def publish_displays(index: dict) -> dict:
    """Give each entry of an older fixture index the display a release carries."""
    for entry in index["catalog_entities"]:
        entry["display"] = published_json(display_of_model(entry["document"]))
    for entry in index["recipes"]:
        entry["display"] = published_json(display_of_recipe(entry["document"]))
    return index
