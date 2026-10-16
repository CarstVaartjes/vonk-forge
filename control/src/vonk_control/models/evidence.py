"""Models: repeated evidence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..model_primitives import Base


class EvidenceRepeat(Base):
    """The same typed evidence about one subject on one Spark, seen again.

    Runtime evidence for the operator, never a retry ledger: nothing reads it
    to decide work.  A success of the subject deletes its rows.
    """

    __tablename__ = "evidence_repeats"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject: Mapped[str] = mapped_column(String(128), primary_key=True)
    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    repeats: Mapped[int] = mapped_column(Integer, nullable=False)
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(256))
