"""One current fault per worker coordinator; never operation history."""

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from vonk_control.failures.coordinator_contract import CoordinatorFailureEvidence
from vonk_control.lifecycle.evidence import Residue
from vonk_control.model_primitives import Base
from vonk_control.models.waits import wait_due_column, wait_evidence_column
from vonk_control.stored.json_values import ContractJSON
from vonk_control.waits.contract import WaitEvidence


class CoordinatorFailure(Base):
    __tablename__ = "coordinator_failures"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    failure: Mapped[CoordinatorFailureEvidence | Residue] = mapped_column(
        ContractJSON[CoordinatorFailureEvidence](__tablename__, "failure"),
        nullable=False,
    )
    wait_evidence: Mapped[WaitEvidence | Residue | None] = wait_evidence_column(
        __tablename__
    )
    next_action_at: Mapped[datetime | None] = wait_due_column("next_action_at")
