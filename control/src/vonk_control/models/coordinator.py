"""One current fault per worker coordinator; never operation history."""

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from ..coordinator_contract import CoordinatorFailureEvidence
from ..model_primitives import Base
from ..stored_json import ContractJSON
from .waits import Residue, WaitEvidence, wait_due_column, wait_evidence_column


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
