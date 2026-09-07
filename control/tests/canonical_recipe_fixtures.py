"""Canonical producer inputs and the smallest current internal test seams."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from vonk_control.recipe_builds import _source_policy_document
from vonk_forge_contracts import ModelDefinition, RecipeDefinition


def _example(name: str) -> dict[str, Any]:
    return json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", name)
        .read_text(encoding="utf-8")
    )


def canonical_model() -> ModelDefinition:
    return ModelDefinition.model_validate(_example("model-definition.json"))


def canonical_recipe() -> RecipeDefinition:
    return RecipeDefinition.model_validate(_example("recipe-image.json"))


def canonical_job_recipe() -> RecipeDefinition:
    return RecipeDefinition.model_validate(_example("recipe-job.json"))


def source_policy_recipe() -> dict[str, Any]:
    """Adapt the canonical source-build document at the policy parser seam."""

    canonical = _example("recipe-source-build.json")
    return _source_policy_document(
        canonical, canonical["execution"]["build"], "c" * 64
    )


def topology_document() -> dict[str, Any]:
    """Return a complete canonical recipe document for topology tests."""

    return canonical_recipe().model_dump(mode="json")
