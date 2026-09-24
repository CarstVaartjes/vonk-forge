"""The current persisted intent for one recipe cache removal request."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from .model_cache_contract import UUID_PATTERN
from .strict_json import StrictJSONModel

RECIPE_CACHE_REMOVE_KIND = "recipe.cache.remove.v2"
RecipeCacheRemovalKind = Literal["recipe.cache.remove.v2"]
RequestKey = Annotated[str, Field(pattern=UUID_PATTERN)]
Selector = Annotated[str, Field(min_length=1, max_length=256)]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]


class RecipeCacheRemovalIntent(StrictJSONModel):
    """Exact accepted removal request stored on its existing Job owner."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2]
    kind: RecipeCacheRemovalKind
    action: Literal["remove"]
    selector: Selector
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    request_key: RequestKey
    recipe_revision_id: Identifier
    with_model: bool
    removal_fence: RequestKey


__all__ = [
    "RECIPE_CACHE_REMOVE_KIND",
    "RecipeCacheRemovalIntent",
    "RecipeCacheRemovalKind",
]
