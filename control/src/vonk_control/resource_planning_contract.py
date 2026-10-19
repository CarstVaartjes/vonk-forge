"""Typed read projections of canonical recipe resource-planning inputs.

The containing recipe and model documents have other fields owned by their
canonical contracts. These projections consume their settings, topology and
model-file contracts without duplicating those nested definitions.
"""

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from vonk_forge_contracts.recipe import (
    RecipeModelSelection,
    RecipeParallelism,
    RecipeSettings,
)


class ResourceTopologyProjection(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    node_count: StrictInt = Field(ge=1)
    parallelism: RecipeParallelism


class ResourceRecipeProjection(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    settings: RecipeSettings
    topology: ResourceTopologyProjection
    models: list[RecipeModelSelection] = Field(default_factory=list)
