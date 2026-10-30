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
from vonk_forge_contracts import ModelDefinition

from .. import model_cache_states
from ..artifact_lifecycle import (
    ArtifactReferenceUnverified,
)
from ..catalog_revision_contract import read_catalog_document
from ..content_identity import catalog_selector_membership, recipe_model_digests
from ..machine_states import DISTRIBUTION_HELD, RUN_LIVE
from ..models import (
    ArtifactDistributionAssignment,
    CatalogDocumentHead,
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
from .model_sets import _protect_saved, _selector_name, saved_profile_selectors
from .types import (
    _ACTIVE_INSTALLATIONS,
    _ACTIVE_PROFILE_APPLICATIONS,
    _ACTIVE_RUN_SWITCH_JOBS,
    ArtifactReferenceFinding,
    _finding,
    _reason_projection,
)


def _declared_files(session: Session, models: set[str]) -> dict[str, frozenset[str]]:
    """The file digests each model content declares, read once per model.

    A model that is missing or whose document cannot be read is absent, which
    callers treat as unknown, never as declaring nothing.
    """

    found: dict[str, frozenset[str]] = {}
    damaged: set[str] = set()
    for revision in session.scalars(
        select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.kind == RunSwitchReasonScopeName.MODEL,
            CatalogDocumentRevision.content_digest.in_(sorted(models)),
        )
    ):
        digest = revision.content_digest
        try:
            model = read_catalog_document(revision)
        except (TypeError, ValueError):
            damaged.add(digest)
            continue
        if not isinstance(model, ModelDefinition):
            damaged.add(digest)
            continue
        found[digest] = found.get(digest, frozenset()) | {
            file.sha256 for file in model.files
        }
    return {digest: files for digest, files in found.items() if digest not in damaged}


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

    # A set is held when it contains a file that a protected model declares.
    # Files are content: a refreshed model that reuses the bytes of an earlier
    # one is found by its files, never by who first requested the set.
    members: dict[str, set[str]] = {digest: set() for digest in selected}
    for set_digest, object_digest in session.execute(
        select(
            ModelCacheSetArtifact.artifact_set_sha256,
            ModelCacheSetArtifact.artifact_sha256,
        ).where(ModelCacheSetArtifact.artifact_set_sha256.in_(sorted(selected)))
    ):
        members[set_digest].add(object_digest)

    profiles = list(session.scalars(select(FleetProfile).order_by(FleetProfile.id)))
    named = {profile.id: saved_profile_selectors(profile) for profile in profiles}
    pairs = sorted(
        {
            _selector_name(selector)
            for chosen in named.values()
            for selector in chosen or ()
        }
    )
    heads = (
        {
            (publisher, slug): revision_id
            for publisher, slug, revision_id in session.execute(
                select(
                    CatalogDocumentHead.publisher,
                    CatalogDocumentHead.slug,
                    CatalogDocumentHead.active_revision_id,
                ).where(
                    CatalogDocumentHead.kind == RunSwitchReasonScopeName.RECIPE,
                    catalog_selector_membership(
                        CatalogDocumentHead.publisher,
                        CatalogDocumentHead.slug,
                        pairs,
                    ),
                )
            )
        }
        if pairs
        else {}
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
    # The models a recipe revision uses are its relational binding rows.
    recipe_ids = {rid for rid in heads.values() if rid} | {
        row.recipe_revision_id for row in running
    }
    bound = recipe_model_digests(session, recipe_ids)
    models = {model for chosen in bound.values() for model in chosen} | {
        row.model_content_sha256 for row in running if row.model_content_sha256
    }
    declared = _declared_files(session, models)

    def held_by(used: Iterable[str] | None) -> set[str]:
        """The selected sets holding a file of ``used``; unknown holds them all."""
        if used is None:
            return set(selected)
        held: set[str] = set()
        for model in used:
            files = declared.get(model)
            if files is None:
                return set(selected)
            held.update(digest for digest, have in members.items() if have & files)
        return held

    for profile in profiles:
        selectors = named[profile.id]
        if selectors is None:
            _protect_saved(findings, profile, "model-set", selected)
            continue
        for selector in selectors:
            # No active revision, or none bound to a model, is unknown.
            _protect_saved(
                findings,
                profile,
                "model-set",
                held_by(bound.get(heads.get(_selector_name(selector)) or "")),
            )

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
        # A row that records no model is unknown and keeps every selected set.
        used = (
            None
            if row.model_content_sha256 is None
            else {row.model_content_sha256}
            | bound.get(row.recipe_revision_id, frozenset())
        )
        label = f"installation {row.id}"
        for set_digest in held_by(used):
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
