"""Give hand-seeded active recipe revisions the catalog head production always has.

Production only activates a recipe revision through the catalog services, which
move the head with it. Tests that insert active revisions directly would
otherwise have no current revision, since current is defined by the head. The
seeded head follows the newest seeded revision; a head a test states itself wins.
Only a recipe revision inserted directly as active is seeded: the catalog
services insert candidates and promote them, so they are never affected.
"""

from __future__ import annotations

import uuid
from weakref import WeakKeyDictionary

from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session
from vonk_control.models import CatalogDocumentHead, CatalogDocumentRevision

_Key = tuple[str, str]
# Weak: a test's entries go with its engine.
_BY_ENGINE: WeakKeyDictionary[Engine, dict[str, int]] = WeakKeyDictionary()


def _seeded(session: Session) -> dict[str, int]:
    """Seeded head id -> revision_number it points at, kept on the test engine."""
    bind = session.get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    return _BY_ENGINE.setdefault(engine, {})


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
    ids = _seeded(session)
    for head in list(session.new):
        if not (isinstance(head, CatalogDocumentHead) and head.kind == "recipe"):
            continue
        if head.id in ids:
            continue
        key = (head.publisher, head.slug)
        standing = next(
            (
                other
                for other in session.new
                if isinstance(other, CatalogDocumentHead)
                and other.id in ids
                and (other.publisher, other.slug) == key
            ),
            None,
        ) or _head_for(session, key)
        if standing is not None and standing.id in ids:
            # The test states its own head after the seed made one: its
            # statement wins, on the head row that already exists.
            del ids[standing.id]
            standing.active_revision_id = head.active_revision_id
            standing.candidate_revision_id = head.candidate_revision_id
            standing.generation = head.generation or 0
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
    ids = _seeded(session)
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
            ids[head.id] = row.revision_number
        elif head.id in ids and row.revision_number > ids[head.id]:
            head.active_revision_id = row.id
            ids[head.id] = row.revision_number
