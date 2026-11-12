"""Which verified image archives a recipe can run, derived from its builds.

An image is its content, so nothing records which revision "may" use it. A
recipe document's revisions share the builds of the whole document: an editorial
successor with the same executable inputs runs its predecessor's build without
a row of its own (``RecipeBuildService.resolve`` finds it by content). The
images a revision can run are therefore the succeeded builds of its recipe
document, newest first, not the builds that happen to name its revision id.

A recipe of another document that pins the same image digest (a dual sharing its
single's prebuilt image) runs the same image: a succeeded build whose image
digest is the digest the revision's projection pins is returned too.

Readers use this for retention, presence and removal scope. Retention must keep
too much rather than too little, so it takes every build of the document;
presence readers ask only for builds of the same source bundle, which is the part
of the build input the catalog projection states without reading the bundle.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState

from .catalog_revision_contract import (
    CatalogRevisionContractError,
    read_catalog_projection,
)
from .content_identity import build_source_bundles
from .lifecycle.evidence import BookkeepingReason, retire_as_unknown
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
    readiness question). A revision whose projection states no bundle takes the
    bundles its own builds recorded; with neither it is unknown and has no
    image it is known to run. Without it every succeeded build of the recipe
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
    # Each projection is read once: it states the pin (the source bundle is a column).
    projections = {
        revision_id: _read_projection(revision)
        for revision_id, revision in revisions.items()
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
    # What each revision's own builds were built from: the evidence that names
    # a revision's source bundle when its projection does not.
    recorded: dict[str, set[str]] = {}
    for builds in by_document.values():
        for build_source, build_revision, _ in builds:
            recorded.setdefault(build_revision, set()).add(build_source)
    result: dict[str, tuple[RevisionImage, ...]] = {}
    for revision_id, revision in revisions.items():
        sources = (
            build_source_bundles(
                revision.source_bundle_sha256,
                recorded.get(revision_id, ()),
            )
            if same_source
            else None
        )
        if same_source and sources is None:
            # Unknown is not "different": no build is excluded for naming
            # another revision; this revision has no image it is known to run.
            retire_as_unknown(
                "revision-source-bundle",
                revision_id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "the projection states no source bundle and no build recorded one",
            )
            result[revision_id] = ()
            continue
        seen: set[str] = set()
        images: list[RevisionImage] = []
        for build_source, _, image in by_document.get(revision.document_id, ()):
            if sources is not None and build_source not in sources:
                continue
            if image.archive_sha256 in seen:
                continue
            seen.add(image.archive_sha256)
            images.append(image)
        result[revision_id] = tuple(images)
    pinned = {
        revision_id: digest
        for revision_id in revisions
        if (digest := _pinned_digest(projections[revision_id])) is not None
    }
    if pinned:
        # An image is its content: a succeeded build of any recipe whose image
        # is the digest this revision pins is an image this revision runs.
        # Both sides state that digest; the recipe that asked first is not it.
        shared: dict[str, list[RevisionImage]] = {}
        for build in session.scalars(
            select(RecipeBuild)
            .where(
                RecipeBuild.image_digest.in_(sorted(set(pinned.values()))),
                RecipeBuild.state == LifecycleState.SUCCEEDED.value,
                RecipeBuild.oci_layout_sha256.is_not(None),
                RecipeBuild.image_bytes.is_not(None),
            )
            .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
        ):
            assert build.image_digest is not None
            assert build.oci_layout_sha256 is not None
            assert build.image_bytes is not None
            shared.setdefault(build.image_digest, []).append(
                RevisionImage(
                    archive_sha256=build.oci_layout_sha256,
                    image_digest=build.image_digest,
                    image_bytes=build.image_bytes,
                    build_id=build.id,
                )
            )
        for revision_id, digest in pinned.items():
            known = {image.archive_sha256 for image in result[revision_id]}
            extra: list[RevisionImage] = []
            for image in shared.get(digest, ()):
                if image.archive_sha256 not in known:
                    known.add(image.archive_sha256)
                    extra.append(image)
            result[revision_id] += tuple(extra)
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


def _read_projection(revision: CatalogDocumentRevision) -> object | None:
    try:
        return read_catalog_projection(revision)
    except CatalogRevisionContractError:
        return None


def _stated(projected: object | None, name: str) -> str | None:
    candidate = getattr(projected, name, None)
    return candidate if isinstance(candidate, str) else None


def _pinned_digest(projected: object | None) -> str | None:
    image = getattr(projected, "prebuilt_image", None)
    return image.digest if image is not None else None


def revision_source_bundle(revision: CatalogDocumentRevision) -> str | None:
    """The source bundle digest the revision was imported with; None if unknown."""

    return revision.source_bundle_sha256
