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
class RecipeLibraryRelease:
    version: str
    released_at: str


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
    release: RecipeLibraryRelease | None = None
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
    # Index documents skipped because they could not be read, each a
    # ``{"recipe_uri", "code", "detail"}`` sync problem.
    problems: tuple[dict[str, object], ...] = ()


__all__ = [
    "RecipeLibraryError",
    "RecipeLibraryItem",
    "RecipeLibraryRelease",
    "RecipeLibrarySnapshot",
    "RecipeLibrarySourceContext",
    "RecipeLibrarySourceFile",
]
