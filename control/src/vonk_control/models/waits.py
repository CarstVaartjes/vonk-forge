"""Typed columns and the common interface of existing durable wait owners."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column

from vonk_control.lifecycle.evidence import Residue
from vonk_control.stored.json_values import ContractJSON
from vonk_control.waits.contract import WaitEvidence


class ScheduledWaitOwner(Protocol):
    wait_evidence: Mapped[WaitEvidence | Residue | None]
    next_action_at: Mapped[datetime | None]


def wait_evidence_column(table: str) -> MappedColumn[WaitEvidence | Residue | None]:
    return mapped_column(
        ContractJSON[WaitEvidence](table, "wait_evidence"), nullable=True
    )


def wait_due_column(name: str) -> MappedColumn[datetime | None]:
    # Existing SQL names retain their values through startup reconciliation.
    return mapped_column(name, DateTime(timezone=True), index=True)


__all__ = [
    "ScheduledWaitOwner",
    "wait_due_column",
    "wait_evidence_column",
]
