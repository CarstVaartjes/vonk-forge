"""Informational license metadata for immutable model authorities.

Vonk Forge preserves territorial license declarations for operators to review,
but does not determine or enforce operator geography. Provider access controls
and all technical admission checks remain authoritative for execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerritorialAdmissionDecision:
    warning: tuple[str, str] | None


def territorial_admission(
    model_version: Mapping[str, object] | None,
    *,
    operation: str,
) -> TerritorialAdmissionDecision:
    """Expose territorial license facts without making them admission gates."""

    if operation not in {"install", "run"}:
        raise ValueError("territorial admission operation is invalid")
    license_document = (
        model_version.get("license") if isinstance(model_version, Mapping) else None
    )
    restrictions = (
        license_document.get("territorial_restrictions")
        if isinstance(license_document, Mapping)
        else None
    )
    if restrictions is None:
        return TerritorialAdmissionDecision(None)
    if not isinstance(restrictions, Mapping):
        return TerritorialAdmissionDecision(None)
    denied = restrictions.get("denied_jurisdictions")
    notice = restrictions.get("notice")
    if (
        not isinstance(denied, list)
        or not denied
        or not all(isinstance(value, str) for value in denied)
        or not isinstance(notice, str)
        or not notice
    ):
        return TerritorialAdmissionDecision(None)
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
