"""One owner for "a newer revision of this recipe exists" (never restarts anything).

Profile load and run always resolve the newest active revision. A workload that
started earlier may still run an older one; this module names that difference
once so the fleet, Spark, profile, CLI and web all say the same thing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Annotated

from pydantic import ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ProjectionCode
from vonk_forge_contracts import read_recipe

from .models import CatalogDocumentRevision
from .strict_json import StrictJSONModel

RECIPE_UPDATE_AVAILABLE = ProjectionCode.RECIPE_UPDATE_AVAILABLE
_Text = Annotated[str, StringConstraints(min_length=1, max_length=64)]
_Detail = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class RecipeUpdateNotice(StrictJSONModel):
    """A running workload uses an older revision than the newest active one."""

    model_config = ConfigDict(extra="forbid", strict=True)

    code: Annotated[str, StringConstraints(pattern=r"^recipe\.update_available$")] = (
        RECIPE_UPDATE_AVAILABLE
    )
    severity: Annotated[str, StringConstraints(pattern=r"^info$")] = "info"
    running_revision_id: _Text
    newest_revision_id: _Text
    running_version: _Text | None = None
    newest_version: _Text | None = None
    running_released_at: _Text | None = None
    newest_released_at: _Text | None = None
    detail: _Detail


def newest_active_revisions(
    session: Session, document_ids: Collection[str]
) -> dict[str, CatalogDocumentRevision]:
    """The newest readable active revision per recipe id.

    This is the same choice profile load and run make, so "newest" here means
    exactly what a reload would start.
    """

    newest: dict[str, CatalogDocumentRevision] = {}
    if not document_ids:
        return newest
    rows = session.scalars(
        select(CatalogDocumentRevision)
        .where(
            CatalogDocumentRevision.kind == "recipe",
            CatalogDocumentRevision.state == "active",
            CatalogDocumentRevision.document_id.in_(sorted(document_ids)),
        )
        .order_by(
            CatalogDocumentRevision.document_id,
            CatalogDocumentRevision.revision_number.desc(),
        )
    )
    for row in rows:
        if row.document_id in newest:
            continue
        try:
            read_recipe(row.document)
        except (TypeError, ValueError):
            continue
        newest[row.document_id] = row
    return newest


def _release(document: Mapping[str, object]) -> tuple[str | None, str | None]:
    release = document.get("release")
    if not isinstance(release, Mapping):
        return None, None
    version, released = release.get("version"), release.get("released_at")
    return (
        version if isinstance(version, str) and version else None,
        released if isinstance(released, str) and released else None,
    )


def recipe_update_notice(
    title: str,
    running: CatalogDocumentRevision,
    newest: CatalogDocumentRevision | None,
) -> RecipeUpdateNotice | None:
    """The notice when `running` is not the newest active revision, else None."""

    if newest is None or newest.id == running.id:
        return None
    if newest.revision_number <= running.revision_number:
        return None
    running_version, running_date = _release(running.document)
    newest_version, newest_date = _release(newest.document)
    running_label = running_version or f"revision {running.revision_number}"
    newest_label = newest_version or f"revision {newest.revision_number}"
    newest_when = f" ({newest_date})" if newest_date else ""
    shown = title if len(title) <= 80 else f"{title[:79]}…"
    return RecipeUpdateNotice(
        running_revision_id=running.id,
        newest_revision_id=newest.id,
        running_version=running_version,
        newest_version=newest_version,
        running_released_at=running_date,
        newest_released_at=newest_date,
        detail=(
            f"Update available: running {shown} {running_label}, newest "
            f"{newest_label}{newest_when}. Reload to apply."
        ),
    )
