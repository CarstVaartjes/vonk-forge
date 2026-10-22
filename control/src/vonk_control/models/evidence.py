"""Database ordering of operation-owned repeated evidence, independent of clocks."""

from sqlalchemy import Sequence

from ..model_primitives import Base

REPEATED_EVIDENCE_SEQUENCE = Sequence(
    "repeated_evidence_sequence", metadata=Base.metadata
)
