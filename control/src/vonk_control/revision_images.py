"""Which verified image archives a recipe can run, derived from its builds.

An image is its content, so nothing records which revision "may" use it. A
recipe document's revisions share the builds of the whole document: an editorial
successor with the same executable inputs runs its predecessor's build without
a row of its own (``RecipeBuildService.resolve`` finds it by content). The
images a revision can run are therefore the succeeded builds of its recipe
document, newest first, not the builds that happen to name its revision id.

Readers use this for retention, presence and removal scope. Retention must keep
too much rather than too little, so it takes every build of the document;
presence readers ask only for builds of the same source bundle, which is the part
of the build input the catalog projection states without reading the bundle.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog_revision_contract import (
    CatalogRevisionContractError,
    read_catalog_projection,
)
from .models import CatalogDocumentRevision, RecipeBuild


@dataclass(frozen=True, slots=True)
class RevisionImage:
    """One verified archive a recipe's build produced."""

    archive_sha256: str
    image_digest: str
    image_bytes: int
    build_id: str


def revision_images(
    session: Session,
    revision_ids: Collection[str],
    *,
    same_source: bool = False,
) -> dict[str, tuple[RevisionImage, ...]]:
    """Images each revision can run, newest build first. Unknown revisions are absent.

    ``same_source`` keeps only builds of the revision's own source bundle (a
    readiness question; a revision whose source bundle cannot be read has only
    the builds that name it). Without it every succeeded build of the recipe
    document is returned (a retention question: unknown means keep).
    """

    if not revision_ids:
        return {}
    revisions = {
        row.id: row
        for row in session.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.id.in_(sorted(revision_ids)),
                CatalogDocumentRevision.kind == "recipe",
            )
        )
    }
    documents = {row.document_id for row in revisions.values()}
    if not documents:
        return {}
    by_document: dict[str, list[tuple[str, str, RevisionImage]]] = {}
    for build, document_id in session.execute(
        select(RecipeBuild, CatalogDocumentRevision.document_id)
        .join(
            CatalogDocumentRevision,
            CatalogDocumentRevision.id == RecipeBuild.recipe_revision_id,
        )
        .where(
            CatalogDocumentRevision.document_id.in_(sorted(documents)),
            RecipeBuild.state == "succeeded",
            RecipeBuild.image_digest.is_not(None),
            RecipeBuild.oci_layout_sha256.is_not(None),
            RecipeBuild.image_bytes.is_not(None),
        )
        .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
    ):
        assert build.image_digest is not None
        assert build.oci_layout_sha256 is not None
        assert build.image_bytes is not None
        by_document.setdefault(document_id, []).append(
            (
                build.source_bundle_sha256,
                build.recipe_revision_id,
                RevisionImage(
                    archive_sha256=build.oci_layout_sha256,
                    image_digest=build.image_digest,
                    image_bytes=build.image_bytes,
                    build_id=build.id,
                ),
            )
        )
    result: dict[str, tuple[RevisionImage, ...]] = {}
    for revision_id, revision in revisions.items():
        source = _source_bundle(revision) if same_source else None
        seen: set[str] = set()
        images: list[RevisionImage] = []
        for build_source, build_revision, image in by_document.get(
            revision.document_id, ()
        ):
            if same_source and (
                build_source != source
                if source is not None
                else build_revision != revision_id
            ):
                continue
            if image.archive_sha256 in seen:
                continue
            seen.add(image.archive_sha256)
            images.append(image)
        result[revision_id] = tuple(images)
    return result


def revision_archives(
    session: Session, revision_ids: Collection[str], *, same_source: bool = False
) -> frozenset[str]:
    """Every archive any of the revisions can run."""

    return frozenset(
        image.archive_sha256
        for images in revision_images(
            session, revision_ids, same_source=same_source
        ).values()
        for image in images
    )


def revisions_running_archives(
    session: Session, archives: Collection[str]
) -> Mapping[str, frozenset[str]]:
    """For each archive, every revision of a recipe document that built it."""

    if not archives:
        return {}
    documents: dict[str, set[str]] = {}
    for archive, document_id in session.execute(
        select(RecipeBuild.oci_layout_sha256, CatalogDocumentRevision.document_id)
        .join(
            CatalogDocumentRevision,
            CatalogDocumentRevision.id == RecipeBuild.recipe_revision_id,
        )
        .where(RecipeBuild.oci_layout_sha256.in_(sorted(archives)))
    ):
        if archive is not None:
            documents.setdefault(archive, set()).add(document_id)
    wanted = {document for values in documents.values() for document in values}
    revisions: dict[str, set[str]] = {}
    if wanted:
        for revision_id, document_id in session.execute(
            select(
                CatalogDocumentRevision.id, CatalogDocumentRevision.document_id
            ).where(CatalogDocumentRevision.document_id.in_(sorted(wanted)))
        ):
            revisions.setdefault(document_id, set()).add(revision_id)
    return {
        archive: frozenset(
            revision_id
            for document_id in documents.get(archive, ())
            for revision_id in revisions.get(document_id, ())
        )
        for archive in archives
    }


def _source_bundle(revision: CatalogDocumentRevision) -> str | None:
    try:
        projected = read_catalog_projection(revision)
    except CatalogRevisionContractError:
        return None
    candidate = getattr(projected, "source_bundle_sha256", None)
    return candidate if isinstance(candidate, str) else None
