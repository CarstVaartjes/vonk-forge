"""Bounded evidence blobs and durable incremental collection cursors."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class FailureEvidenceRecord(Base):
    __tablename__ = "failure_evidence_records"
    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class FailureEvidenceCursor(Base):
    __tablename__ = "failure_evidence_cursors"
    family: Mapped[str] = mapped_column(String(32), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
