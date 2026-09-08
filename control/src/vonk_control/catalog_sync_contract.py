"""Canonical durable catalog synchronization evidence shared with the API."""

from typing import Literal

from pydantic import ConfigDict, Field

from .library_contract import UuidId
from .strict_json import StrictJSONModel


class StrictModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)


_SEMVER = (
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class ManagedCatalogSyncProblem(StrictModel):
    recipe_uri: str | None = Field(default=None, max_length=256)
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=256)


class ManagedCatalogWithdrawnRecipe(StrictModel):
    recipe_id: UuidId
    recipe_uri: str | None = Field(default=None, max_length=256)
    release_version: str | None = Field(default=None, pattern=_SEMVER, max_length=64)


class ManagedCatalogStaleRecipe(StrictModel):
    recipe_id: UuidId
    current_revision_id: UuidId
    stale_installation_count: int = Field(ge=0)
    stale_run_count: int = Field(ge=0)


class ManagedCatalogSyncResult(StrictModel):
    schema_version: Literal[1]
    state: Literal["current", "partial", "failed"]
    imported_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    withdrawn_count: int = Field(ge=0)
    withdrawn_recipes: list[ManagedCatalogWithdrawnRecipe]
    stale_recipes: list[ManagedCatalogStaleRecipe]
    problems: list[ManagedCatalogSyncProblem]
