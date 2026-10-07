"""Typed read projections of canonical recipe resource-planning inputs.

The containing recipe and model documents have other fields owned by their
canonical contracts. These projections consume their settings, topology and
model-file contracts without duplicating those nested definitions.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from vonk_forge_contracts.recipe import (
    RecipeEmbeddingSettings,
    RecipeGenerationSettings,
    RecipeJobSettings,
    RecipeModelSelection,
    RecipeParallelism,
)


class ResourceTopologyProjection(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    node_count: StrictInt = Field(ge=1)
    parallelism: RecipeParallelism


class ResourceRecipeProjection(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    settings: Annotated[
        RecipeGenerationSettings | RecipeEmbeddingSettings | RecipeJobSettings,
        Field(discriminator="kind"),
    ]
    topology: ResourceTopologyProjection
    models: list[RecipeModelSelection] = Field(default_factory=list)
