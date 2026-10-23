"""Insert natural identities once; replay committed rows without read locks."""

from __future__ import annotations

from sqlalchemy import inspect, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from vonk_agent_protocol import UnknownOutcomeError, WaitReason

from .model_primitives import Base
from .stored_json import validate_row_write


def insert_identity[Row: Base](
    session: Session, candidate: Row, *, key: tuple[str, ...]
) -> tuple[Row, bool]:
    """Return the inserted row or the winner of the exact unique key.

    A separate SELECT after DO NOTHING sees the winner under READ COMMITTED,
    including when it committed while INSERT arbitrated the unique index.
    Arbitrate all unique indexes together: targeting one index can deadlock
    against another unique index for the same identity. Read back only the
    requested natural key; a different unique collision or concurrent deletion
    requires a fresh plan. Other integrity constraints still fail normally.
    Callers decide content conflicts; this primitive never retries or overwrites.
    """
    validate_row_write(candidate)
    model = type(candidate)
    state = inspect(candidate).dict
    values = {
        column.key: state[column.key]
        for column in inspect(model).columns
        if column.key in state
    }
    inserted = session.scalar(
        insert(model).values(**values).on_conflict_do_nothing().returning(model)
    )
    if inserted is not None:
        return inserted, True
    existing = session.scalars(
        select(model)
        .where(*(getattr(model, name) == getattr(candidate, name) for name in key))
        .execution_options(populate_existing=True)
    ).one_or_none()
    if existing is None:
        raise UnknownOutcomeError(
            f"Unique identity changed while creating {model.__tablename__}; re-plan.",
            reason=WaitReason.SCOPE_CHANGED,
        )
    return existing, False
