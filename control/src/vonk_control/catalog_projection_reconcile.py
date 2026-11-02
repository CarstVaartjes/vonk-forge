"""Persist the current projection of every stored catalog revision, once per start.

A revision written under an earlier contract keeps a projection in its old
shape. Reads already repair it (``read_catalog_projection``), so nothing waits
for this pass: it only stores what a read would derive, so later reads do not
repeat the repair. It is independent of the catalog sync and never gates serving.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .catalog_revision_contract import (
    read_catalog_document,
    rederive_projection,
    write_catalog_projection,
)
from .models import CatalogDocumentRevision

_LOGGER = logging.getLogger(__name__)
_BATCH = 100


def _reconcile_batch(session: Session, after: str) -> tuple[str | None, int]:
    """Re-derive one id range; return its last id (None when empty) and the writes."""
    rows = session.scalars(
        select(CatalogDocumentRevision)
        .where(CatalogDocumentRevision.id > after)
        .order_by(CatalogDocumentRevision.id)
        .limit(_BATCH)
        # A row a sync holds is current by that sync's own write.
        .with_for_update(skip_locked=True)
    ).all()
    written = 0
    for row in rows:
        try:
            projected = write_catalog_projection(
                rederive_projection(row.projected, read_catalog_document(row)),
                kind=row.kind,
            )
            if canonical_message(projected) == canonical_message(row.projected):
                continue
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == row.id)
                .values(projected=projected)
            )
            written += 1
        except Exception:
            _LOGGER.warning(
                "catalog projection of revision %s left as stored",
                row.id,
                exc_info=True,
            )
    return (rows[-1].id if rows else None), written


def reconcile_catalog_projections(sessions: sessionmaker[Session]) -> int:
    """Store the current projection of every revision; return the rows rewritten."""
    with sessions() as session:
        count = session.scalar(select(func.count(CatalogDocumentRevision.id))) or 0
    after, total = "", 0
    # One extra pass for the empty batch that ends the walk; rows added after the
    # count are reconciled by the next start.
    for _batch in range(count // _BATCH + 2):
        with sessions.begin() as session:
            last, written = _reconcile_batch(session, after)
        if last is None:
            break
        after, total = last, total + written
    return total


async def keep_catalog_projections(sessions: sessionmaker[Session]) -> None:
    """One background pass per Controller start; a failed pass is retried by the next start."""
    try:
        total = await asyncio.to_thread(reconcile_catalog_projections, sessions)
    except Exception:
        _LOGGER.exception("catalog projection reconcile deferred")
        return
    if total:
        _LOGGER.info("Re-derived %d catalog revision projections", total)
