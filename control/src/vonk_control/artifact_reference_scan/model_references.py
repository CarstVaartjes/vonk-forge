"""Artifact reference scan: model references."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    canonical_message,
)
from vonk_agent_protocol.agent_words import RunSwitchReasonScopeName
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from .. import model_cache_states
from ..artifact_lifecycle import (
    ArtifactReferenceUnverified,
)
from ..catalog_revision_contract import read_catalog_document
from ..content_identity import recipe_model_digests
from ..machine_states import DISTRIBUTION_HELD, RUN_LIVE
from ..models import (
    ArtifactDistributionAssignment,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
    RecipeRun,
)
from .intents import (
    _profile_plan,
    _run_switch_kinds,
    _run_switch_plan,
)
from .model_sets import _protect_saved, _selectors_revisions, saved_profile_selectors
from .types import (
    _ACTIVE_INSTALLATIONS,
    _ACTIVE_PROFILE_APPLICATIONS,
    _ACTIVE_RUN_SWITCH_JOBS,
    ArtifactReferenceFinding,
    _finding,
    _reason_projection,
)


def _model_documents(session: Session, roots: set[str]) -> dict[str, ModelDefinition]:
    """The models and everything they depend on, read once each, level by level.

    A model that is missing or whose document cannot be read is absent, which
    callers treat as unknown, never as declaring nothing.
    """

    found: dict[str, ModelDefinition] = {}
    asked: set[str] = set()
    pending = set(roots)
    while pending:
        asked |= pending
        level: dict[str, ModelDefinition] = {}
        for revision in session.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == RunSwitchReasonScopeName.MODEL,
                CatalogDocumentRevision.content_digest.in_(sorted(pending)),
            )
        ):
            try:
                model = read_catalog_document(revision)
            except (TypeError, ValueError):
                continue
            if isinstance(model, ModelDefinition):
                level[revision.content_digest] = model
        found.update(level)
        pending = {
            dependency.content_sha256
            for model in level.values()
            for dependency in model.dependencies
        } - asked
    return found


def _needs(
    revision: CatalogDocumentRevision,
    used: frozenset[str],
    models: dict[str, ModelDefinition],
) -> list[frozenset[str]] | None:
    """The files a set must contain to serve each model a recipe revision uses.

    A model needs the files its recipe selects (all of them when the recipe does
    not select it) plus the needs of its dependencies. A recipe or model that
    cannot be read, or a selection naming none of the model's files, is unknown.
    """

    try:
        recipe = read_catalog_document(revision)
    except (TypeError, ValueError):
        return None
    if not isinstance(recipe, RecipeDefinition):
        return None
    chosen: dict[str, set[str]] = {}
    for selection in recipe.models:
        chosen.setdefault(selection.model.content_sha256, set()).update(
            file.file_id for file in selection.files
        )
    needs: dict[str, frozenset[str]] = {}

    def need(digest: str, path: frozenset[str]) -> frozenset[str] | None:
        if digest in needs:
            return needs[digest]
        model = models.get(digest)
        if model is None or digest in path:
            return None
        ids = chosen.get(digest)
        files = {file.sha256 for file in model.files if ids is None or file.id in ids}
        for dependency in model.dependencies:
            inner = need(dependency.content_sha256, path | {digest})
            if inner is None:
                return None
            files |= inner
        if not files:
            return None
        needs[digest] = frozenset(files)
        return needs[digest]

    for root in sorted(used):
        if need(root, frozenset()) is None:
            return None
    return list(needs.values())


def model_set_reference_findings(
    session: Session, set_digests: Iterable[str]
) -> dict[str, tuple[ArtifactReferenceFinding, ...]]:
    """Find typed saved-profile and active owners of exact model sets."""

    selected = set(set_digests)
    if not selected:
        return {}
    sets = {
        row.artifact_set_sha256: row
        for row in session.scalars(
            select(ModelCacheSet)
            .where(ModelCacheSet.artifact_set_sha256.in_(selected))
            .order_by(ModelCacheSet.artifact_set_sha256)
        )
    }
    if set(sets) != selected:
        raise ArtifactReferenceUnverified(
            ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
            "model-set owners could not be read; removal was deferred",
            retryable=True,
        )
    findings: dict[str, set[ArtifactReferenceFinding]] = {
        digest: set() for digest in selected
    }

    # A set is held when it serves a protected model: it contains every file the
    # model needs. Shared small files are kept by the object scope, never by
    # holding unrelated sets.
    members: dict[str, set[str]] = {digest: set() for digest in selected}
    for set_digest, object_digest in session.execute(
        select(
            ModelCacheSetArtifact.artifact_set_sha256,
            ModelCacheSetArtifact.artifact_sha256,
        ).where(ModelCacheSetArtifact.artifact_set_sha256.in_(sorted(selected)))
    ):
        members[set_digest].add(object_digest)
    if not all(members.values()):
        raise ArtifactReferenceUnverified(
            ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
            "model-set membership is unavailable; removal was deferred",
            retryable=True,
        )

    profiles = list(session.scalars(select(FleetProfile).order_by(FleetProfile.id)))
    named = {profile.id: saved_profile_selectors(profile) for profile in profiles}
    resolved = _selectors_revisions(
        session, {selector for chosen in named.values() for selector in chosen or ()}
    )
    running = list(
        session.execute(
            select(
                RecipeInstallation.id,
                RecipeInstallation.state,
                RecipeInstallation.model_content_sha256,
                RecipeInstallation.recipe_revision_id,
            )
            .where(
                or_(
                    RecipeInstallation.state.in_(_ACTIVE_INSTALLATIONS),
                    select(RecipeRun.id)
                    .where(
                        RecipeRun.installation_id == RecipeInstallation.id,
                        RecipeRun.state.in_(RUN_LIVE),
                    )
                    .exists(),
                )
            )
            .order_by(RecipeInstallation.id)
        )
    )
    recipes = {
        revision.id: revision for found in resolved.values() for revision in found
    }
    installed = {row.recipe_revision_id for row in running} - recipes.keys()
    if installed:
        recipes.update(
            {
                revision.id: revision
                for revision in session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.id.in_(sorted(installed))
                    )
                )
            }
        )
    # A recipe uses the models its binding rows name; an installation also the
    # model its row records.
    bound = recipe_model_digests(session, recipes.keys())
    uses = {
        row.id: bound.get(row.recipe_revision_id, frozenset())
        | ({row.model_content_sha256} if row.model_content_sha256 else frozenset())
        for row in running
    }
    documents = _model_documents(
        session,
        {model for chosen in bound.values() for model in chosen}
        | {model for chosen in uses.values() for model in chosen},
    )
    needs: dict[tuple[str, frozenset[str]], list[frozenset[str]] | None] = {}

    def held_by(revision_id: str, used: frozenset[str]) -> set[str]:
        """The selected sets serving the models used; unknown holds them all."""
        revision = recipes.get(revision_id)
        key = (revision_id, used)
        if key not in needs:
            needs[key] = None if revision is None else _needs(revision, used, documents)
        wanted = needs[key]
        if wanted is None:
            return set(selected)
        return {
            digest
            for digest, have in members.items()
            if any(need <= have for need in wanted)
        }

    for profile in profiles:
        selectors = named[profile.id]
        if selectors is None:
            _protect_saved(findings, profile, "model-set", selected)
            continue
        for selector in selectors:
            found = resolved[selector]
            # No revision, or a revision bound to no model, is unknown.
            held = set() if found else set(selected)
            for revision in found:
                used = bound.get(revision.id, frozenset())
                held |= held_by(revision.id, used) if used else set(selected)
            _protect_saved(findings, profile, "model-set", held)

    def account(value: object) -> None:
        try:
            canonical_message(value)
        except (TypeError, ValueError) as error:
            raise ArtifactReferenceUnverified(
                ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
                "accepted reference JSON is malformed; removal was deferred",
                retryable=True,
            ) from error

    for application in session.scalars(
        select(FleetProfileApplication)
        .where(FleetProfileApplication.state.in_(_ACTIVE_PROFILE_APPLICATIONS))
        .order_by(FleetProfileApplication.id)
        .execution_options(yield_per=64)
    ):
        account(application.plan)
        plan = _profile_plan(application.plan)
        for preparation in plan.preparation_decisions:
            identity = preparation.model
            if identity.artifact_set_sha256 in selected:
                findings[identity.artifact_set_sha256].add(
                    _finding(
                        "model-set",
                        identity.artifact_set_sha256,
                        owner_kind="fleet-profile-application",
                        owner_id=application.id,
                        state=application.state,
                        classification="active-work",
                        detail="accepted profile application prepares this model set",
                        reason=f"profile application {application.id}",
                    )
                )

    run_switch_kinds = _run_switch_kinds()
    if run_switch_kinds:
        for operation in session.scalars(
            select(Job)
            .where(
                Job.kind.in_(run_switch_kinds),
                Job.state.in_(_ACTIVE_RUN_SWITCH_JOBS),
            )
            .order_by(Job.id)
            .execution_options(yield_per=64)
        ):
            account(operation.payload)
            plan = _run_switch_plan(operation)
            set_digest = plan.storage.artifact_set_sha256
            if set_digest in selected:
                findings[set_digest].add(
                    _finding(
                        "model-set",
                        set_digest,
                        owner_kind="run-switch-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="accepted Run/Switch operation uses this model set",
                        reason=f"run/switch operation {operation.id}",
                    )
                )

    for row in running:
        label = f"installation {row.id}"
        for set_digest in held_by(row.recipe_revision_id, uses[row.id]):
            findings[set_digest].add(
                _finding(
                    "model-set",
                    set_digest,
                    owner_kind="recipe-installation",
                    owner_id=row.id,
                    state=row.state,
                    classification="active-work",
                    detail="accepted installation uses this model set",
                    reason=label,
                )
            )

    for distribution in session.scalars(
        select(ArtifactDistributionAssignment)
        # Expiry does not prove that a serving worker has stopped. Explicit
        # revocation is the existing durable fence for this reference owner.
        .where(ArtifactDistributionAssignment.state.in_(DISTRIBUTION_HELD))
        .order_by(ArtifactDistributionAssignment.id)
        .execution_options(yield_per=64)
    ):
        account(distribution.objects)
        if distribution.model_artifact_set_sha256 in selected:
            findings[distribution.model_artifact_set_sha256].add(
                _finding(
                    "model-set",
                    distribution.model_artifact_set_sha256,
                    owner_kind="artifact-distribution-assignment",
                    owner_id=distribution.id,
                    state=distribution.state,
                    classification="active-work",
                    detail="accepted distribution assignment retains the model set",
                    reason=f"active distribution assignment {distribution.id}",
                )
            )

    for operation in session.scalars(
        select(ModelCacheOperation)
        .where(
            ModelCacheOperation.kind.in_(("download", "repair")),
            ModelCacheOperation.state.in_(model_cache_states.LIVE),
        )
        .order_by(ModelCacheOperation.id)
    ):
        if operation.artifact_set_sha256 in selected:
            account(operation.payload)
            findings[operation.artifact_set_sha256].add(
                _finding(
                    "model-set",
                    operation.artifact_set_sha256,
                    owner_kind="model-cache-operation",
                    owner_id=operation.id,
                    state=operation.state,
                    classification="active-work",
                    detail="active model-cache download or repair owns this set",
                    reason=f"model-cache operation {operation.id}",
                )
            )

    return {
        digest: tuple(
            sorted(
                value,
                key=lambda item: (
                    item.classification,
                    item.owner_kind,
                    item.owner_id,
                    item.state,
                    item.detail,
                    item.reason,
                ),
            )
        )
        for digest, value in findings.items()
    }


def model_set_reference_reasons(
    session: Session, set_digests: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """Project typed model-set findings into existing presentation reasons."""

    return _reason_projection(model_set_reference_findings(session, set_digests))
