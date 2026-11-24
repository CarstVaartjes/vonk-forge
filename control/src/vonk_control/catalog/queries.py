"""Selection of current catalog heads without retiring accepted revisions."""

from sqlalchemy import Exists, select
from sqlalchemy.orm import defer

from vonk_control.models.catalog import CatalogDocumentHead, CatalogDocumentRevision


def active_head_revision() -> Exists:
    """Match the authoritative head for this revision's full document identity."""
    return (
        select(CatalogDocumentHead.id)
        .where(
            CatalogDocumentHead.active_revision_id == CatalogDocumentRevision.id,
            CatalogDocumentHead.kind == CatalogDocumentRevision.kind,
            CatalogDocumentHead.publisher == CatalogDocumentRevision.publisher,
            CatalogDocumentHead.slug == CatalogDocumentRevision.slug,
        )
        .exists()
    )


# Row lookups select revisions without their document, which loads (on access)
# only where it is read: decoding it also snapshots it for the immutability guard.
WITHOUT_DOCUMENT = defer(CatalogDocumentRevision.document)
