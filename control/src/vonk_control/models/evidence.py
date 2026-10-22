"""Database ordering of operation-owned repeated evidence, independent of clocks."""

from sqlalchemy import Sequence

from ..model_primitives import Base

REPEATED_EVIDENCE_ORDINAL = Sequence(
    "repeated_evidence_ordinal", metadata=Base.metadata
)
