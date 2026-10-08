"""Durable Run/Switch journal observation and append-only repair evidence."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from .lifecycle.evidence import Residue
from .model_primitives import Base, _lower_hex
from .run_switch_identity_contract import RunSwitchCancellation
from .run_switch_journal_contract import (
    RunSwitchJournalRepairEndEvidence,
    RunSwitchJournalRepairEvidence,
    RunSwitchJournalRepairPendingState,
)
from .stored_json import ContractJSON


class RunSwitchJournalRepairPending(Base):
    """Bounded observation intent; deleted after repair or non-blocking end."""

    __tablename__ = "run_switch_journal_repair_pending"
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    # The immutable deadline survives damage to the mutable observation projection.
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cancellation: Mapped[RunSwitchCancellation | Residue | None] = mapped_column(
        ContractJSON[RunSwitchCancellation](
            "run_switch_journal_repair_pending", "cancellation"
        ),
        nullable=True,
    )
    progress: Mapped[RunSwitchJournalRepairPendingState | Residue] = mapped_column(
        ContractJSON[RunSwitchJournalRepairPendingState](
            "run_switch_journal_repair_pending", "progress"
        ),
        nullable=False,
    )


class RunSwitchJournalRepair(Base):
    """Append-only repair evidence retained with the owning Job's history."""

    __tablename__ = "run_switch_journal_repairs"
    __table_args__ = (
        UniqueConstraint("job_id", "original_digest", "record_kind"),
        CheckConstraint(
            "record_kind IN ('repair','end')", name="ck_run_switch_journal_record_kind"
        ),
        CheckConstraint(
            _lower_hex("original_digest", 64),
            name="ck_run_switch_journal_repair_digest",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Derived evidence index: an end never overwrites an earlier repair witness.
    record_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="repair"
    )
    evidence: Mapped[
        RunSwitchJournalRepairEvidence | RunSwitchJournalRepairEndEvidence | Residue
    ] = mapped_column(
        ContractJSON[
            RunSwitchJournalRepairEvidence | RunSwitchJournalRepairEndEvidence
        ]("run_switch_journal_repairs", "evidence"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


@event.listens_for(RunSwitchJournalRepair, "before_update")
def _retain_run_switch_journal_repair(_mapper, _connection, _target) -> None:
    raise ValueError("Run/Switch journal repair evidence is append-only")
