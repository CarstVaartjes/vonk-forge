"""Storage sweep references and evidence."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import InstallationState, LifecycleState, WaitReason

from .. import model_cache_states
from ..content_identity import (
    RecipeExecution,
    catalog_reference_membership,
    runs_identically,
)
from ..fleet_profile_contract import FleetProfilePreview, NodeId
from ..models import (
    AgentNode,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    InstallationNode,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
)
from ..reference_facts import profiles_naming
from ..spark_disk import spark_free_bytes
from .common import _COLLECTABLE_STATES, _DEAD_RUNS, _FINISHED_JOBS, ACTOR

if TYPE_CHECKING:
    from .evidence import _CollectorEvidence, _Evidence


def _installation_kept(
    session: Session, installation_id: str, evidence: _Evidence
) -> str | None:
    """Why an installation stays, or ``None`` when nothing uses it."""

    installation = session.get(RecipeInstallation, installation_id)
    if installation is None or installation.state not in _COLLECTABLE_STATES:
        return "not installed"
    revision = session.execute(
        select(
            CatalogDocumentRevision.id,
            CatalogDocumentRevision.document_id,
            CatalogDocumentRevision.publisher,
            CatalogDocumentRevision.slug,
        ).where(CatalogDocumentRevision.id == installation.recipe_revision_id)
    ).one_or_none()
    # A profile follows the catalog head of its recipe. A revision
    # row that is gone, or a recipe with no active revision left, is history
    # nothing can point to (a retracted recipe has no head); it never keeps the installation's disk.
    newest = evidence.newest.get(revision.document_id) if revision else None
    nodes = set(
        session.scalars(
            select(InstallationNode.node_id).where(
                InstallationNode.installation_id == installation_id
            )
        )
    )
    if evidence.pointers is None or evidence.loaded_pointers is None:
        return "profiles unreadable"
    if (
        revision is not None
        and newest is not None
        and _points_at(
            evidence.loaded_pointers, revision.publisher, revision.slug, nodes
        )
        and _head_runs_it(
            session, revision.id, newest, nodes, is_head=revision.id == newest
        )
    ):
        return "profile"
    runs = session.execute(
        select(RecipeRun.id, RecipeRun.state).where(
            RecipeRun.installation_id == installation_id
        )
    ).all()
    if any(state not in _DEAD_RUNS for _id, state in runs):
        return "running"
    # A node is a placement/coordination scope, never an asset identity. Match
    # exact effects and content, including an install prepared before its
    # agent child or run exists. Do not match recipe/build provenance.
    names = {installation_id, installation.plan_digest}
    names.update(run_id for run_id, _state in runs)
    names.add(installation.image_digest.removeprefix("sha256:"))
    if installation.model_content_sha256 is not None:
        names.add(installation.model_content_sha256)
    try:
        plan = parse_stored_installation_plan(installation.plan, for_uninstall=True)
    except RecipeExecutionContractError:
        return WaitReason.OBSERVATION_UNAVAILABLE.value
    for compiled in plan.compiled_execution_plans.values():
        names.add(compiled.identity.model_artifact_set_sha256)
        names.add(compiled.runtime_image.oci_layout_sha256)
        names.add(compiled.runtime_image.image_digest.removeprefix("sha256:"))
        # A shared tokenizer/metadata file names only that object, not this
        # installation. The collector accounts for protected objects separately
        # and uninstalls with preserve_model_files=True.
        for artifact in compiled.artifacts:
            names.add(artifact.model.content_sha256)
    if not evidence.operations.isdisjoint(names):
        return "live operation"
    if not evidence.application_references_readable:
        return "profile application unreadable"
    return None


def _head_runs_it(
    session: Session,
    revision: str,
    head: str,
    nodes: set[str],
    *,
    is_head: bool,
) -> bool:
    """Whether an installation on ``nodes`` of ``revision`` is what its recipe's
    ``head`` revision runs there.

    An installation of the head is. One of an older revision that runs
    identically (a release that changed only metadata) is too, until an
    installed installation of the head covers its Sparks: a reload made it a
    spare twin, and it goes like any unused installation. Both documents are
    read only for such older revisions.
    """

    if is_head:
        return True
    executions = {
        row.id: RecipeExecution(
            row.execution_key,
            row.source_bundle_sha256,
            row.content_digest,
            row.document,
        )
        for row in session.execute(
            select(
                CatalogDocumentRevision.id,
                CatalogDocumentRevision.content_digest,
                CatalogDocumentRevision.execution_key,
                CatalogDocumentRevision.source_bundle_sha256,
                CatalogDocumentRevision.document,
            ).where(CatalogDocumentRevision.id.in_((revision, head)))
        )
    }
    installed, current = executions.get(revision), executions.get(head)
    if installed is None or current is None or not runs_identically(installed, current):
        return False
    covered = set(
        session.scalars(
            select(InstallationNode.node_id)
            .join(
                RecipeInstallation,
                RecipeInstallation.id == InstallationNode.installation_id,
            )
            .where(
                catalog_reference_membership(
                    RecipeInstallation.recipe_revision_id, (head,)
                ),
                RecipeInstallation.state == InstallationState.INSTALLED,
                InstallationNode.node_id.in_(sorted(nodes)),
            )
        )
    )
    return not nodes <= covered


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
    """The saved profiles that point at this installation (it runs what the head
    of their recipe runs, on its Sparks); empty when none or when unreadable."""

    installation = session.get(RecipeInstallation, installation_id)
    if installation is None or pointers is None:
        return ()
    revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
    newest = evidence.newest.get(revision.document_id) if revision else None
    if revision is None or newest is None:
        return ()
    nodes = set(
        session.scalars(
            select(InstallationNode.node_id).where(
                InstallationNode.installation_id == installation_id
            )
        )
    )
    pointing = _points_at(pointers, revision.publisher, revision.slug, nodes)
    if not pointing or not _head_runs_it(
        session, revision.id, newest, nodes, is_head=revision.id == newest
    ):
        return ()
    return pointing


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


def _model_kept(
    session: Session, digest: str, evidence: _CollectorEvidence
) -> str | None:
    """Why a model's cached files stay, or ``None`` when no profile needs them."""

    reference = evidence.reference
    if reference.pointers is None:
        return "profiles unreadable"
    sets = session.execute(
        select(
            ModelCacheSet.artifact_set_sha256,
            ModelCacheSet.recipe_revision_sha256,
        ).where(ModelCacheSet.model_content_sha256 == digest)
    ).all()
    if digest in reference.pointed_models or any(
        recipe in reference.pointed_digests for _s, recipe in sets
    ):
        return "profile"
    # Model and set digests only, as for an installation: a file shared with a
    # used successor does not keep a superseded revision. The removal scope's
    # delete_objects keeps the shared bytes and only unique bytes count as free.
    names = {digest} | {set_digest for set_digest, _r in sets}
    if not evidence.live.isdisjoint(names):
        return "live operation"
    if not reference.application_references_readable:
        return "profile application unreadable"
    return None


def _image_kept(
    archive: str, evidence: _CollectorEvidence, modified: datetime | None
) -> str | None:
    """Why an image receipt stays, or ``None`` when no profile needs the image."""

    reference = evidence.reference
    if reference.pointers is None:
        return "profiles unreadable"
    if archive in evidence.pointed_archives:
        return "profile"
    if modified is None:
        return "receipt unreadable"
    if archive in evidence.live:
        return "live operation"
    if not reference.application_references_readable:
        return "profile application unreadable"
    return None


def _latest_snapshots(
    session: Session, node_id: str | None = None
) -> dict[str, tuple[int, int, datetime]]:
    """Each live Spark's latest reported (free, total, received at) disk. Free
    counts the unused model files its agent removes itself before a claim
    writes: only a need beyond them uninstalls anything."""

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
            NodeInventorySnapshot.unused_model_bytes,
            NodeInventorySnapshot.disk_total_bytes,
            NodeInventorySnapshot.received_at,
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
        found: (spark_free_bytes(free, unused), total, _utc(received))
        for found, free, unused, total, received in rows
    }


def _spark_settling(session: Session, node_id: str, received_at: datetime) -> bool:
    """A removal (or the stop of a failed run before one) this collector queued
    is still running, or finished after the Spark last reported its free space,
    so that report does not show it yet."""

    for targets in session.scalars(
        select(Job.targets).where(
            Job.kind.in_(
                (
                    WireAgentOperation.RECIPE_UNINSTALL.value,
                    WireAgentOperation.RECIPE_STOP.value,
                )
            ),
            Job.actor == ACTOR,
            or_(Job.state.not_in(_FINISHED_JOBS), Job.updated_at >= received_at),
        )
    ):
        if isinstance(targets, list) and node_id in targets:
            return True
    return False


def _model_removal_in_flight(session: Session) -> bool:
    """Whether a model removal is deleting bytes now.

    Only a running removal is settling.  One parked in backoff waits on a
    dependency and frees nothing; a queued one has not started, and the next
    pass sees it running if it does.  Counting those would let a removal that
    waits for ever switch off eviction on space pressure.
    """

    return (
        session.scalar(
            select(ModelCacheOperation.id)
            .where(
                ModelCacheOperation.kind == "remove",
                ModelCacheOperation.state.in_(
                    model_cache_states.words(LifecycleState.RUNNING)
                ),
            )
            .limit(1)
        )
        is not None
    )


def _profile_pointers(
    session: Session,
    only_profile_id: str | None = None,
    *,
    recipes: Collection[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]]:
    """The profile name and Spark set each saved profile assigns to each recipe.

    Which profiles name which recipes is the stored projection; only the Spark
    sets are read from those profiles' own assignments. ``only_profile_id``
    restricts it to one profile (the loaded one), ``recipes`` to these recipes.
    """

    named = profiles_naming(session, recipes, profile_id=only_profile_id)
    found: dict[tuple[str, str], list[tuple[str, frozenset[str]]]] = {}
    for profile_id, name, assignments in session.execute(
        select(FleetProfile.id, FleetProfile.name, FleetProfile.assignments)
        .where(FleetProfile.id.in_(sorted(named)))
        .order_by(FleetProfile.id)
    ):
        wanted = named[profile_id]
        for recipe, nodes in _assignment_sparks(session, assignments, wanted):
            found.setdefault(recipe, []).append((name, nodes))
    return {key: tuple(value) for key, value in found.items()}


def _assignment_sparks(
    session: Session, assignments: object, recipes: frozenset[tuple[str, str]]
) -> list[tuple[tuple[str, str], frozenset[str]]]:
    """Each assignment of ``recipes`` with its Sparks.

    A Spark set that cannot be read retains that recipe on every enrolled
    Spark, never another recipe.
    """

    by_recipe: dict[tuple[str, str], list[object]] = {}
    for assignment in assignments if isinstance(assignments, list) else ():
        if isinstance(assignment, Mapping):
            publisher, _, slug = str(assignment.get("recipe_selector", "")).partition(
                "/"
            )
            by_recipe.setdefault((publisher, slug), []).append(
                assignment.get("spark_ids")
            )
    found: list[tuple[tuple[str, str], frozenset[str]]] = []
    for recipe in sorted(recipes):
        for value in by_recipe.get(recipe) or [None]:
            try:
                nodes = TypeAdapter(list[NodeId]).validate_python(value, strict=True)
            except (TypeError, ValueError):
                nodes = list(session.scalars(select(AgentNode.node_id)))
            found.append((recipe, frozenset(nodes)))
    return found


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
