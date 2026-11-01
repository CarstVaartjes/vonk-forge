"""Give hand-seeded active recipe revisions the catalog head production always has.

Production only activates a recipe revision through the catalog services, which
move the head with it. Tests that insert active revisions directly would
otherwise have no current revision, since current is defined by the head. The
seeded head follows the newest seeded revision; a head a test states itself wins.
"""

from __future__ import annotations

import uuid

from sqlalchemy import event, select
from sqlalchemy.orm import Session
from vonk_control.models import CatalogDocumentHead, CatalogDocumentRevision

_Key = tuple[str, str]
# Seeded head id -> revision_number of the revision it points at. Ids are
# unique per test database, so nothing carries between tests.
_SEEDED: dict[str, int] = {}


def install_seeded_heads() -> None:
    event.listen(Session, "before_flush", _note_seeded)
    event.listen(Session, "after_flush_postexec", _add_heads)


def _head_for(session: Session, key: _Key) -> CatalogDocumentHead | None:
    with session.no_autoflush:
        return session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.kind == "recipe",
                CatalogDocumentHead.publisher == key[0],
                CatalogDocumentHead.slug == key[1],
            )
        )


def _note_seeded(session: Session, _context: object, _instances: object) -> None:
    for head in list(session.new):
        if not (isinstance(head, CatalogDocumentHead) and head.kind == "recipe"):
            continue
        if head.id in _SEEDED:
            continue
        key = (head.publisher, head.slug)
        seeded = next(
            (
                other
                for other in session.new
                if isinstance(other, CatalogDocumentHead)
                and other.id in _SEEDED
                and (other.publisher, other.slug) == key
            ),
            None,
        ) or _head_for(session, key)
        if seeded is not None and seeded.id in _SEEDED:
            # The test states its own head after the seed made one: its
            # statement wins, on the head row that already exists.
            del _SEEDED[seeded.id]
            seeded.active_revision_id = head.active_revision_id
            seeded.candidate_revision_id = head.candidate_revision_id
            seeded.generation = head.generation or 0
            session.expunge(head)
    pending: dict[_Key, CatalogDocumentRevision] = session.info.setdefault(
        "seeded_revisions", {}
    )
    stated = {
        (head.publisher, head.slug)
        for head in session.new
        if isinstance(head, CatalogDocumentHead) and head.kind == "recipe"
    }
    for row in session.new:
        key = (
            (row.publisher, row.slug)
            if isinstance(row, CatalogDocumentRevision)
            else None
        )
        if (
            isinstance(row, CatalogDocumentRevision)
            and key is not None
            and row.kind == "recipe"
            and row.state == "active"
            and key not in stated
        ):
            row.id = row.id or str(uuid.uuid4())
            best = pending.get(key)
            if best is None or row.revision_number > best.revision_number:
                pending[key] = row


def _add_heads(session: Session, _context: object) -> None:
    pending: dict[_Key, CatalogDocumentRevision] = session.info.pop(
        "seeded_revisions", {}
    )
    for key, row in pending.items():
        head = _head_for(session, key)
        if head is None:
            head = CatalogDocumentHead(
                id=str(uuid.uuid4()),
                kind="recipe",
                publisher=key[0],
                slug=key[1],
                active_revision_id=row.id,
                generation=0,
            )
            session.add(head)
            _SEEDED[head.id] = row.revision_number
        elif head.id in _SEEDED and row.revision_number > _SEEDED[head.id]:
            head.active_revision_id = row.id
            _SEEDED[head.id] = row.revision_number
