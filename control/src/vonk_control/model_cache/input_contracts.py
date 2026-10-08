"""Typed artifact locators at catalog and fixture ingress."""

from __future__ import annotations

from pydantic import Field, model_validator

from ..model_cache_contract import CacheManifestArtifactPart
from ..strict_json import StrictModel


class CatalogArtifactPart(StrictModel):
    path: str
    sha256: str
    download_bytes: int = Field(gt=0)


class CatalogArtifact(StrictModel):
    id: str
    path: str
    kind: str
    repository: str
    revision: str
    sha256: str
    download_bytes: int = Field(ge=0)
    roles: list[str]
    release_id: int | None = None
    asset_id: int | None = None
    parts: list[CatalogArtifactPart] | None = None


class FixtureArtifact(StrictModel):
    """Explicit test-only ingress, never admitted by the production constructor."""

    id: str
    path: str
    kind: str
    sha256: str
    download_bytes: int = Field(ge=0)
    roles: list[str]
    source: str | None = None
    repository: str | None = None
    revision: str | None = None
    model_content_sha256: str | None = None
    parts: list[CacheManifestArtifactPart] | None = None

    @model_validator(mode="after")
    def source_is_present(self) -> FixtureArtifact:
        if self.source is None and not (
            self.kind in {"http.file", "file"} and self.repository is not None
        ):
            raise ValueError("cache artifact source is missing")
        return self
