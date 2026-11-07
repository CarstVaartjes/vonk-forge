"""Shared snapshot types for the canonical recipe package reader."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from vonk_forge_contracts import ModelDisplay, RecipeApi, RecipeDisplay
from vonk_forge_contracts.model import ModelIdentity
from vonk_forge_contracts.qualification_status import PublishedQualificationStatusView

if TYPE_CHECKING:
    from .catalog_revision_contract import PrebuiltImage
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
    title: str
    description: str
    tags: tuple[str, ...]
    display: RecipeDisplay
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
    title: str
    description: str
    tags: tuple[str, ...]
    display: RecipeDisplay
    #: The published recipe document, carried as published: its digest is over
    #: exactly these JSON values and is verified where it is imported.
    document: dict[str, object]
    prebuilt_image: PrebuiltImage | None
    publication_commit: str


@dataclass(frozen=True, slots=True)
class CatalogModelEntry:
    """One index Model: the signed document and the display that travels with it.

    The display is optional: a display this Controller cannot read leaves the
    Model (the execution document) importable, and is read again on a later sync.
    """

    document: dict
    display: ModelDisplay | None
    #: From the validated document, so retraction never depends on the display.
    identity: ModelIdentity


@dataclass(frozen=True, slots=True)
class RecipeLibrarySnapshot:
    commit: str
    items: tuple[RecipeLibraryItem, ...]
    repository: str = "CarstVaartjes/vonk-forge-recipes"
    catalog_entities: tuple[CatalogModelEntry, ...] = ()
    # Index documents skipped because they could not be read, each a sync problem.
    problems: tuple[ManagedCatalogSyncProblem, ...] = ()
    # Displays this Controller could not read: the document still applies. A
    # note is reported but is not a skipped document, does not make the snapshot
    # incomplete and does not pause retraction. A display already stored stays
    # shown until a readable one arrives.
    display_notes: tuple[ManagedCatalogSyncProblem, ...] = ()
    # The library release version (its contract version) and when its
    # recipes last changed.
    version: str | None = None
    updated_at: datetime | None = None


__all__ = [
    "CatalogModelEntry",
    "RecipeLibraryError",
    "RecipeLibraryItem",
    "RecipeLibraryRelease",
    "RecipeLibrarySnapshot",
    "RecipeLibrarySourceContext",
    "RecipeLibrarySourceFile",
    "RecipePackageEntry",
]
