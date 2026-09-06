"""Shared snapshot types for the canonical recipe package reader."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .recipe_packages import RecipePackageHandle


class RecipeLibraryError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail[:256]
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class RecipeLibrarySourceFile:
    path: str
    blob_sha: str
    size: int


@dataclass(frozen=True, slots=True)
class RecipeLibrarySourceContext:
    path: str
    content_sha256: str
    expected_bytes: int
    files: tuple[RecipeLibrarySourceFile, ...]


@dataclass(frozen=True, slots=True)
class RecipeLibraryChange:
    kind: str
    summary: str
    details: str | None = None
    references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecipeLibraryRelease:
    version: str
    released_at: str
    content_sha256: str
    upgrade_effect: str
    changes: tuple[RecipeLibraryChange, ...]


@dataclass(frozen=True, slots=True)
class RecipeLibraryItem:
    library_commit: str
    source_path: str
    publisher: str
    slug: str
    title: str
    description: str
    tags: tuple[str, ...]
    content_sha256: str
    uri: str
    document: dict[str, object]
    release_history: tuple[RecipeLibraryRelease, ...] = ()
    dependencies: tuple[dict[str, object], ...] = ()
    source_context: RecipeLibrarySourceContext | None = None
    source_bundle: bytes | None = None
    package_handle: RecipePackageHandle | None = None
    package_sha256: str | None = None
    source_bundle_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RecipeLibrarySnapshot:
    commit: str
    items: tuple[RecipeLibraryItem, ...]
    repository: str = "CarstVaartjes/vonk-forge-recipes"
    catalog_entities: tuple[dict[str, object], ...] = ()


__all__ = [
    "RecipeLibraryChange",
    "RecipeLibraryError",
    "RecipeLibraryItem",
    "RecipeLibraryRelease",
    "RecipeLibrarySnapshot",
    "RecipeLibrarySourceContext",
    "RecipeLibrarySourceFile",
]
