"""Storage sweep references and evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import InstallationState, canonical_message

from .. import model_cache_states
from ..fleet_profile_contract import FleetProfilePreview
from ..models import (
    AgentNode,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    InstallationNode,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
)
from .common import _ASSIGNMENTS, _DEAD_RUNS, _FINISHED_JOBS, ACTOR

if TYPE_CHECKING:
    from .evidence import _Evidence


def _installation_kept(
    session: Session, installation_id: str, evidence: _Evidence
) -> str | None:
    """Why an installation stays, or ``None`` when nothing uses it."""

    installation = session.get(RecipeInstallation, installation_id)
    if installation is None or installation.state != InstallationState.INSTALLED:
        return "not installed"
    revision = session.execute(
        select(
            CatalogDocumentRevision.document_id,
            CatalogDocumentRevision.publisher,
            CatalogDocumentRevision.slug,
        ).where(CatalogDocumentRevision.id == installation.recipe_revision_id)
    ).one_or_none()
    newest = evidence.newest.get(revision.document_id) if revision else None
    if revision is None or newest is None:
        return "recipe unavailable"
    nodes = set(
        session.scalars(
            select(InstallationNode.node_id).where(
                InstallationNode.installation_id == installation_id
            )
        )
    )
    if evidence.pointers is None or evidence.loaded_pointers is None:
        return "profiles unreadable"
    if newest[0] == installation.recipe_revision_id and _points_at(
        evidence.loaded_pointers, revision.publisher, revision.slug, nodes
    ):
        return "profile"
    runs = session.execute(
        select(RecipeRun.id, RecipeRun.state).where(
            RecipeRun.installation_id == installation_id
        )
    ).all()
    if any(state not in _DEAD_RUNS for _id, state in runs):
        return "running"
    if any(state == "failed" for _id, state in runs):
        # Uninstall refuses a run that was never stopped; nothing here stops one.
        return "run not stopped"
    if (
        installation_id in evidence.operations
        or any(run_id in evidence.operations for run_id, _state in runs)
        or not evidence.owned_nodes.isdisjoint(nodes)
        or any(
            node_id in scope for scope in evidence.active_scopes for node_id in nodes
        )
    ):
        return "live operation"
    if not evidence.application_references_readable:
        return "profile application unreadable"
    return None


def _points_at(
    pointers: Mapping[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]],
    publisher: str,
    slug: str,
    nodes: set[str],
) -> tuple[str, ...]:
    """The profiles in ``pointers`` that name this recipe on any of ``nodes``."""

    return tuple(
        name
        for name, sparks in pointers.get((publisher.casefold(), slug.casefold()), ())
        if nodes & sparks
    )


def _installation_pointing(
    session: Session,
    installation_id: str,
    evidence: _Evidence,
    pointers: Mapping[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]] | None,
) -> tuple[str, ...]:
    """The saved profiles that point at this installation (newest revision of
    their recipe, on its Sparks); empty when none or when unreadable."""

    installation = session.get(RecipeInstallation, installation_id)
    if installation is None or pointers is None:
        return ()
    revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
    newest = evidence.newest.get(revision.document_id) if revision else None
    if revision is None or newest is None or newest[3] != revision.content_digest:
        return ()
    nodes = set(
        session.scalars(
            select(InstallationNode.node_id).where(
                InstallationNode.installation_id == installation_id
            )
        )
    )
    return _points_at(pointers, revision.publisher, revision.slug, nodes)


def _installation_last_used(session: Session, installation_id: str) -> datetime:
    """When the installation, one of its Sparks or one of its runs last changed."""

    installation = session.get(RecipeInstallation, installation_id)
    assert installation is not None
    stamps = [_utc(installation.updated_at)]
    stamps.extend(
        _utc(updated)
        for updated in session.scalars(
            select(InstallationNode.updated_at).where(
                InstallationNode.installation_id == installation_id
            )
        )
    )
    stamps.extend(
        _utc(updated)
        for updated in session.scalars(
            select(RecipeRun.updated_at).where(
                RecipeRun.installation_id == installation_id
            )
        )
    )
    return max(stamps)


_UNFINISHED_LOADS = ("queued", "running")


def _applied_revision_ids(session: Session) -> frozenset[str] | None:
    """Read exact pending references; damage cannot prove any artifact unused."""

    found: set[str] = set()
    for application in session.scalars(
        select(FleetProfileApplication).where(
            FleetProfileApplication.state.in_(_UNFINISHED_LOADS)
        )
    ):
        try:
            plan = FleetProfilePreview.model_validate_json(
                json.dumps(application.plan), strict=True
            )
        except (TypeError, ValueError):
            return None
        if (
            plan.profile_id != application.profile_id
            or plan.profile_digest != application.profile_digest
            or plan.plan_digest != application.plan_digest
        ):
            return None
        found.update(item.recipe_revision_id for item in plan.resolved_assignments)
    return frozenset(found)


def _model_kept(session: Session, digest: str, evidence: _Evidence) -> str | None:
    """Why a model's cached files stay, or ``None`` when no profile needs them."""

    sets = session.execute(
        select(
            ModelCacheSet.artifact_set_sha256,
            ModelCacheSet.recipe_revision_sha256,
        ).where(ModelCacheSet.model_content_sha256 == digest)
    ).all()
    if digest in evidence.pointed_models or any(
        recipe in evidence.pointed_digests for _s, recipe in sets
    ):
        return "profile"
    names = {digest} | {set_digest for set_digest, _r in sets}
    names.update(
        session.scalars(
            select(ModelCacheSetArtifact.artifact_sha256).where(
                ModelCacheSetArtifact.artifact_set_sha256.in_(names)
            )
        )
    )
    if not evidence.live.isdisjoint(names):
        return "live operation"
    if not evidence.application_references_readable:
        return "profile application unreadable"
    return None


def _image_kept(
    archive: str, evidence: _Evidence, modified: datetime | None
) -> str | None:
    """Why an image receipt stays, or ``None`` when no profile needs the image."""

    if archive in evidence.pointed_archives:
        return "profile"
    if modified is None:
        return "receipt unreadable"
    if archive in evidence.live:
        return "live operation"
    if not evidence.application_references_readable:
        return "profile application unreadable"
    return None


def _latest_snapshots(
    session: Session, node_id: str | None = None
) -> dict[str, tuple[int, int, datetime]]:
    """Each live Spark's latest reported (free, total, observed at) disk."""

    latest = select(
        NodeInventorySnapshot.node_id,
        func.max(NodeInventorySnapshot.observed_at).label("observed_at"),
    ).group_by(NodeInventorySnapshot.node_id)
    if node_id is not None:
        latest = latest.where(NodeInventorySnapshot.node_id == node_id)
    newest = latest.subquery()
    rows = session.execute(
        select(
            NodeInventorySnapshot.node_id,
            NodeInventorySnapshot.disk_free_bytes,
            NodeInventorySnapshot.disk_total_bytes,
            NodeInventorySnapshot.observed_at,
        )
        .join(
            newest,
            and_(
                NodeInventorySnapshot.node_id == newest.c.node_id,
                NodeInventorySnapshot.observed_at == newest.c.observed_at,
            ),
        )
        .join(AgentNode, AgentNode.node_id == NodeInventorySnapshot.node_id)
        .where(AgentNode.revoked_at.is_(None))
    )
    return {
        found: (free, total, _utc(observed)) for found, free, total, observed in rows
    }


def _spark_settling(session: Session, node_id: str, observed_at: datetime) -> bool:
    """A removal this collector queued is still running, or finished after the
    Spark last reported its free space, so that report does not show it yet."""

    for targets in session.scalars(
        select(Job.targets).where(
            Job.kind == "recipe.uninstall",
            Job.actor == ACTOR,
            or_(Job.state.not_in(_FINISHED_JOBS), Job.updated_at >= observed_at),
        )
    ):
        if isinstance(targets, list) and node_id in targets:
            return True
    return False


def _model_removal_in_flight(session: Session) -> bool:
    return (
        session.scalar(
            select(ModelCacheOperation.id)
            .where(
                ModelCacheOperation.kind == "remove",
                ModelCacheOperation.state.in_(model_cache_states.LIVE),
            )
            .limit(1)
        )
        is not None
    )


def _profile_pointers(
    session: Session, only_profile_id: str | None = None
) -> dict[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]] | None:
    """The profile name and Spark set each saved profile assigns to each recipe.

    ``None`` when a profile cannot be read as the current contract: nothing is
    then provably unpointed, so the caller keeps everything an assignment might
    name.  ``only_profile_id`` restricts it to one profile (the loaded one).
    """

    found: dict[tuple[str, str], list[tuple[str, frozenset[str]]]] = {}
    statement = select(FleetProfile.name, FleetProfile.assignments)
    if only_profile_id is not None:
        statement = statement.where(FleetProfile.id == only_profile_id)
    try:
        for name, assignments in session.execute(statement):
            for assignment in _ASSIGNMENTS.validate_json(
                canonical_message(assignments), strict=True
            ):
                publisher, separator, slug = assignment.recipe_selector.partition("/")
                if not separator:
                    return None
                found.setdefault((publisher.casefold(), slug.casefold()), []).append(
                    (name, frozenset(assignment.spark_ids))
                )
    except (TypeError, ValueError):
        return None
    return {key: tuple(value) for key, value in found.items()}


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def _code(error: Exception) -> str:
    return str(getattr(error, "code", type(error).__name__))


def _utc(value: datetime) -> datetime:
    # SQLite returns naive timestamps for timezone-aware columns.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
