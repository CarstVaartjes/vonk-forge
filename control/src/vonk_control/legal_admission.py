"""Informational license metadata for immutable model authorities.

Vonk Forge preserves territorial license declarations for operators to review,
but does not determine or enforce operator geography. Provider access controls
and all technical admission checks remain authoritative for execution.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session
from vonk_forge_contracts import ModelDisplay

from .models import CatalogDocument, CatalogDocumentRevision


@dataclass(frozen=True, slots=True)
class TerritorialAdmissionDecision:
    warning: tuple[str, str] | None


def stored_model_display(
    session: Session, revision: CatalogDocumentRevision | None
) -> ModelDisplay | None:
    """The published display of a Model revision's document, or None.

    The license is a display fact, not part of the hashed Model document. A
    display that is missing, or stored by an older contract and unreadable now,
    declares nothing (read, and noted once, by the one reader the library uses): the
    warning built from it is informational and never blocks anything.
    """

    if revision is None:
        return None
    row = session.get(CatalogDocument, revision.document_id)
    if row is None:
        return None
    return row.readable_model_display()


def territorial_admission(
    display: ModelDisplay | None,
    *,
    operation: str,
) -> TerritorialAdmissionDecision:
    """Expose territorial license facts without making them admission gates."""

    if operation not in {"install", "run"}:
        raise ValueError("territorial admission operation is invalid")
    restrictions = None if display is None else display.license.territorial_restrictions
    if restrictions is None:
        return TerritorialAdmissionDecision(None)
    denied = restrictions.denied_jurisdictions
    notice = restrictions.notice
    prefix = f"{operation}.license"
    return TerritorialAdmissionDecision(
        (
            f"{prefix}_territorial_restrictions_informational",
            (
                f"{notice} Territorial restrictions are informational; Vonk Forge "
                "does not determine or enforce operator geography. Declared denied "
                f"jurisdictions: {', '.join(denied)}."
            ),
        ),
    )
