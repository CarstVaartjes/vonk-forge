"""Repeated identical evidence is a platform fault: shown, never acted on.

A deterministic failure ends its own work with its evidence.  When the same
evidence comes back about the same subject on the same Spark with no success
between (every Stop failing with one code, a runtime grant refused on every
ask, a profile retry declined the same way on every try), retrying is not
what is wrong: the platform is, and an operator has to look.

:func:`note_repeat` records one more occurrence at the caller's own commit
point and :func:`note_success` clears the subject.  Neither returns anything,
so no caller can branch on the count or turn it into a cap: the work goes on
exactly as it would without them.  Whether a repetition is an alarm is decided
when it is read (like ``progress_stall``): the one failure limit of
occurrences, the last of them within the profile stall window, so evidence
that stops coming back ages out without anyone acknowledging it.

Every write and read is fail-open: an absent or unreadable table records and
shows nothing, and never fails the caller's work.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import EvidenceRepeat
from .recovery_policy import RECOVERY
from .settings import PROFILE_STALL_WINDOW_SECONDS

_LOGGER = logging.getLogger(__name__)
_SHOWN_FOR = timedelta(seconds=PROFILE_STALL_WINDOW_SECONDS)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def note_repeat(
    session: Session,
    subject: str,
    node_id: str,
    code: str,
    *,
    now: datetime,
    detail: str | None = None,
) -> None:
    """Count one more occurrence of ``code`` about ``subject`` on ``node_id``."""

    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        return
    at = _utc(now)
    statement = insert(EvidenceRepeat).values(
        node_id=node_id,
        subject=subject[:128],
        code=str(code)[:128],
        repeats=1,
        first_at=at,
        last_at=at,
        detail=None if detail is None else detail[:256],
    )
    table = EvidenceRepeat.__table__
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.node_id, table.c.subject, table.c.code],
        set_={
            "repeats": table.c.repeats + 1,
            "last_at": statement.excluded.last_at,
            "detail": statement.excluded.detail,
        },
    ).returning(table.c.repeats)
    # The caller's own pending writes flush (and fail) outside the savepoint.
    session.flush()
    try:
        with session.begin_nested():
            repeats = int(session.execute(statement).scalar_one())
    except SQLAlchemyError as error:
        _LOGGER.warning("repeated evidence was not recorded: %s", type(error).__name__)
        return
    if RECOVERY.exhausted(repeats) and not RECOVERY.exhausted(repeats - 1):
        _LOGGER.warning(
            "evidence repeating: %s x%d on %s on %s", code, repeats, subject, node_id
        )


def note_success(session: Session, subject: str, node_id: str) -> None:
    """The subject succeeded on ``node_id``: its repetition is over."""

    session.flush()
    try:
        with session.begin_nested():
            session.execute(
                delete(EvidenceRepeat).where(
                    EvidenceRepeat.node_id == node_id,
                    EvidenceRepeat.subject == subject[:128],
                )
            )
    except SQLAlchemyError as error:
        _LOGGER.warning("repeated evidence was not cleared: %s", type(error).__name__)


def repeating_evidence(
    session: Session, node_ids: Sequence[str], now: datetime
) -> dict[str, tuple[str, ...]]:
    """Each Spark's repeating evidence, in words, decided at read time."""

    try:
        rows = session.scalars(
            select(EvidenceRepeat)
            .where(
                EvidenceRepeat.node_id.in_(node_ids),
                EvidenceRepeat.last_at >= _utc(now) - _SHOWN_FOR,
            )
            .order_by(
                EvidenceRepeat.node_id, EvidenceRepeat.subject, EvidenceRepeat.code
            )
        ).all()
    except SQLAlchemyError:
        return {}
    shown: dict[str, list[str]] = {}
    for row in rows:
        if not RECOVERY.exhausted(row.repeats):
            continue
        detail = (
            f"{row.code} x{row.repeats} on {row.subject} since "
            f"{_utc(row.first_at):%Y-%m-%dT%H:%MZ}, last {_utc(row.last_at):%Y-%m-%dT%H:%MZ}"
            + ("" if row.detail is None else f": {row.detail}")
        )
        shown.setdefault(row.node_id, []).append(detail[:256])
    return {node_id: tuple(details) for node_id, details in shown.items()}
