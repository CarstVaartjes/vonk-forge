"""Selection of current catalog heads without retiring accepted revisions."""

from sqlalchemy import Exists, select

from .models import CatalogDocumentHead, CatalogDocumentRevision


def active_head_revision() -> Exists:
    """Match the authoritative head for this revision's full document identity."""
    return select(CatalogDocumentHead.id).where(
        CatalogDocumentHead.active_revision_id == CatalogDocumentRevision.id,
        CatalogDocumentHead.kind == CatalogDocumentRevision.kind,
        CatalogDocumentHead.publisher == CatalogDocumentRevision.publisher,
        CatalogDocumentHead.slug == CatalogDocumentRevision.slug,
    ).exists()
