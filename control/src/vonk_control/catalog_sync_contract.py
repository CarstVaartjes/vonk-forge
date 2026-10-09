"""Canonical durable catalog synchronization evidence shared with the API."""

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field
from vonk_agent_protocol import CatalogSyncState, machine_adopter

from .library_contract import UuidId
from .strict_json import StrictModel

SEMVER_PATTERN = (
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
    release_version: str | None = Field(
        default=None, pattern=SEMVER_PATTERN, max_length=64
    )


class ManagedCatalogStaleRecipe(StrictModel):
    recipe_id: UuidId
    current_revision_id: UuidId
    stale_installation_count: int = Field(ge=0)
    stale_run_count: int = Field(ge=0)


_SyncResultState = Annotated[
    Literal[
        CatalogSyncState.CURRENT,
        CatalogSyncState.PARTIAL,
        CatalogSyncState.FAILED,
    ],
    BeforeValidator(machine_adopter(CatalogSyncState)),
]


class ManagedCatalogSyncResult(StrictModel):
    reviewed_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal[1]
    state: _SyncResultState
    imported_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    withdrawn_count: int = Field(ge=0)
    withdrawn_recipes: list[ManagedCatalogWithdrawnRecipe]
    stale_recipes: list[ManagedCatalogStaleRecipe]
    problems: list[ManagedCatalogSyncProblem]
