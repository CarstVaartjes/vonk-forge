"""The library projection carries the recipe's declared load peak and minimum swap."""

from __future__ import annotations

import json

from vonk_control.library_projection.service import LibraryProjection
from vonk_forge_contracts import RecipeDefinition, read_recipe

from tests.catalog_launch_fixtures import INPUTS

_GIB = 1 << 30


def _recipe() -> RecipeDefinition:
    path = min((INPUTS / "recipes").glob("*.json"))
    return read_recipe(json.loads(path.read_text(encoding="utf-8")))


def _with_memory(
    recipe: RecipeDefinition, *updates: dict[str, int | None]
) -> RecipeDefinition:
    template = recipe.topology.roles[0]
    roles = []
    for update in updates:
        memory = template.resources.memory.model_copy(update=update)
        resources = template.resources.model_copy(update={"memory": memory})
        roles.append(template.model_copy(update={"resources": resources}))
    topology = recipe.topology.model_copy(update={"roles": roles})
    return recipe.model_copy(update={"topology": topology})


def test_worker_declared_peaks_reach_the_projection() -> None:
    base = _recipe().topology.roles[0].resources.memory
    recipe = _with_memory(
        _recipe(),
        {"load_peak_bytes": None, "minimum_swap_bytes": 0},
        {
            "load_peak_bytes": base.peak_bytes + 8 * _GIB,
            "minimum_swap_bytes": 16 * _GIB,
        },
    )

    resources = LibraryProjection._recipe_resources(recipe)

    assert resources.memory_bytes == base.peak_bytes
    assert resources.load_peak_bytes == base.peak_bytes + 8 * _GIB
    assert resources.minimum_swap_bytes == 16 * _GIB


def test_recipe_declaring_neither_projects_none() -> None:
    recipe = _with_memory(_recipe(), {"load_peak_bytes": None, "minimum_swap_bytes": 0})

    resources = LibraryProjection._recipe_resources(recipe)

    assert resources.load_peak_bytes is None
    assert resources.minimum_swap_bytes is None
