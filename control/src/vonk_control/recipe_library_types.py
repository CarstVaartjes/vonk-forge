"""Shared snapshot types for the canonical recipe package reader."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from vonk_forge_contracts import (
    CatalogModelEntry,
    CatalogVocabulary,
    PrebuiltImage,
    RecipeApi,
    RecipeDisplay,
)
from vonk_forge_contracts.qualification_status import PublishedQualificationStatusView

if TYPE_CHECKING:
    from .catalog_revision_contract import PublishedDocument
    from .catalog_sync_contract import ManagedCatalogSyncProblem
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
    # ``None`` when the index's display was unreadable: titled by identity.
    display: RecipeDisplay | None
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
    # The signed index's CI-built runtime image for this exact revision.
    prebuilt_image: PrebuiltImage | None = None
    # The hardware qualification the signed index derived for this revision.
    qualification: PublishedQualificationStatusView | None = None
    # The gateway API facts the signed index carries beside this revision, read
    # once at ingress; none when absent or unreadable (the route then publishes
    # no model_info).
    api: RecipeApi | None = None


@dataclass(frozen=True, slots=True)
class RecipePackageEntry:
    """One recipe's package as the signed index declares it."""

    publisher: str
    slug: str
    source_path: str
    recipe_content_sha256: str
    package_sha256: str
    size: int
    location: str
    publication_commit: str


@dataclass(frozen=True, slots=True)
class CatalogModel:
    """One index Model as the contract's catalog reader read it.

    ``entry.display`` is ``None`` when this Controller cannot read it: the Model
    stays importable and the stored display stays until a readable one arrives.
    """

    entry: CatalogModelEntry
    #: The document exactly as published: the bytes ``entry.content_sha256``
    #: names, which the reader verified and the import stores unchanged.
    published: PublishedDocument


@dataclass(frozen=True, slots=True)
class RecipeLibrarySnapshot:
    commit: str
    items: tuple[RecipeLibraryItem, ...]
    repository: str = "CarstVaartjes/vonk-forge-recipes"
    catalog_entities: tuple[CatalogModel, ...] = ()
    # Index entries the catalog reader skipped, each a sync problem.
    problems: tuple[ManagedCatalogSyncProblem, ...] = ()
    # Blocks the catalog reader dropped or this Controller withholds (a word a
    # newer minor added): the entry still applies. A note is reported but is
    # not a skipped entry, does not make the snapshot incomplete and does not
    # pause retraction. A display already stored stays shown until a readable
    # one arrives.
    display_notes: tuple[ManagedCatalogSyncProblem, ...] = ()
    # The library release version (its contract version) and when its
    # recipes last changed.
    version: str | None = None
    updated_at: datetime | None = None
    # The words the release publishes for capabilities and statuses.
    vocabulary: CatalogVocabulary | None = None


__all__ = [
    "CatalogModel",
    "RecipeLibraryError",
    "RecipeLibraryItem",
    "RecipeLibraryRelease",
    "RecipeLibrarySnapshot",
    "RecipeLibrarySourceContext",
    "RecipeLibrarySourceFile",
    "RecipePackageEntry",
]
