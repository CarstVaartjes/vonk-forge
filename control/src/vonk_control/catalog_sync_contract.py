"""Canonical durable catalog synchronization evidence shared with the API."""

import hashlib
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BeforeValidator, Field, field_validator
from vonk_agent_protocol import CatalogSyncState, machine_adopter
from vonk_agent_protocol.wire_model import WireEnum

from .library_contract import UuidId
from .strict_json import StrictModel

if TYPE_CHECKING:
    from .recipe_library_types import RecipeLibrarySnapshot


class CatalogSyncTrigger(WireEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class ManagedCatalogSyncRequest(StrictModel):
    """One request boundary for sync identity, intent and reviewed content."""

    request_key: str = Field(
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    trigger: Annotated[
        CatalogSyncTrigger, BeforeValidator(machine_adopter(CatalogSyncTrigger))
    ]
    actor: str = Field(min_length=1, max_length=200)
    reviewed_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("actor")
    @classmethod
    def actor_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sync actor must be nonempty")
        return value


def reviewed_catalog_content(snapshot: "RecipeLibrarySnapshot") -> str:
    """Bind content, accepting identical publication under another commit."""
    from .recipe_packages.contracts import _snapshot_content

    return hashlib.sha256(_snapshot_content(snapshot)).hexdigest()


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
