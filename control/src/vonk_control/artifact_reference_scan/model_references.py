"""Artifact reference scan: model references."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    canonical_message,
)
from vonk_forge_contracts import RecipeDefinition

from .. import model_cache_states
from ..artifact_lifecycle import (
    ArtifactReferenceUnverified,
)
from ..catalog_revision_contract import read_catalog_document
from ..machine_states import DISTRIBUTION_HELD
from ..model_cache_contract import CacheManifest
from ..models import (
    ArtifactDistributionAssignment,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    FleetProfile,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeInstallation,
)
from .intents import _profile_plan, _run_switch_kinds, _run_switch_plan
from .model_sets import _protect_saved, _selector_revisions, saved_profile_selectors
from .types import (
    _ACTIVE_INSTALLATIONS,
    _ACTIVE_PROFILE_APPLICATIONS,
    _ACTIVE_RUN_SWITCH_JOBS,
    ArtifactReferenceFinding,
    _finding,
    _reason_projection,
)


def model_set_reference_findings(
    session: Session, set_digests: Iterable[str]
) -> dict[str, tuple[ArtifactReferenceFinding, ...]]:
    """Find typed saved-profile and active owners of exact model sets."""

    from ..model_cache.catalog_helpers import (
        _canonical_model_artifacts,
        _recipe_model_content_digests,
        _recipe_model_file_ids,
    )

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

    # Resolve relational owner pointers first; only content digests decide which
    # bytes a binding protects. Identical recipe content shares these bindings.
    recipe_content = {
        owner_id: digest
        for owner_id, digest in session.execute(
            select(
                CatalogDocumentRevision.id, CatalogDocumentRevision.content_digest
            ).where(CatalogDocumentRevision.kind == "recipe")
        )
    }
    bound_models: dict[str, set[str]] = {}
    for owner_id, model_digest in session.execute(
        select(
            CatalogRecipeModelReference.recipe_revision_id,
            CatalogRecipeModelReference.model_content_digest,
        )
    ):
        digest = recipe_content.get(owner_id)
        if digest is not None:
            bound_models.setdefault(digest, set()).add(model_digest)

    for profile in session.scalars(select(FleetProfile).order_by(FleetProfile.id)):
        selectors = saved_profile_selectors(profile)
        if selectors is None:
            _protect_saved(findings, profile, "model-set", selected)
            continue
        for selector in selectors:
            revisions = _selector_revisions(session, selector)
            if not revisions:
                _protect_saved(findings, profile, "model-set", selected)
                continue
            for record in revisions:
                model_digests = set(bound_models.get(record.content_digest, ()))
                required: set[str] = set()
                readable = True
                try:
                    recipe = TypeAdapter(RecipeDefinition).validate_json(
                        read_catalog_document(record).model_dump_json(), strict=True
                    )
                    model_digests.update(_recipe_model_content_digests(recipe))
                    for model_digest in model_digests:
                        model = session.scalar(
                            select(CatalogDocumentRevision)
                            .where(
                                CatalogDocumentRevision.kind == "model",
                                CatalogDocumentRevision.content_digest == model_digest,
                            )
                            .order_by(CatalogDocumentRevision.created_at.desc())
                            .limit(1)
                        )
                        if model is None:
                            readable = False
                            continue
                        file_ids = _recipe_model_file_ids(recipe, model_digest)
                        required.update(
                            artifact.sha256
                            for artifact in _canonical_model_artifacts(model)
                            if file_ids is None or artifact.id in file_ids
                        )
                except (TypeError, ValueError, RuntimeError):
                    readable = False
                protected: set[str] = set()
                for set_digest, row in sets.items():
                    if (
                        row.recipe_revision_sha256 == record.content_digest
                        or row.model_content_sha256 in model_digests
                    ):
                        protected.add(set_digest)
                        continue
                    try:
                        manifest = CacheManifest.model_validate_json(
                            canonical_message(row.manifest), strict=True
                        )
                    except (TypeError, ValueError):
                        protected.add(set_digest)
                        continue
                    if required and required <= {
                        artifact.sha256 for artifact in manifest.artifacts
                    }:
                        protected.add(set_digest)
                    elif not readable and (
                        not model_digests or row.model_content_sha256 is None
                    ):
                        # There is no exact independent scope for these bytes.
                        protected.add(set_digest)
                _protect_saved(findings, profile, "model-set", protected)

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
            plan = _run_switch_plan(operation.payload)
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

    for installation in session.scalars(
        select(RecipeInstallation)
        .where(RecipeInstallation.state.in_(_ACTIVE_INSTALLATIONS))
        .order_by(RecipeInstallation.id)
        .execution_options(yield_per=64)
    ):
        account(installation.plan)
        plan = _run_switch_plan({"plan": installation.plan})
        set_digest = plan.storage.artifact_set_sha256
        if set_digest in selected:
            findings[set_digest].add(
                _finding(
                    "model-set",
                    set_digest,
                    owner_kind="recipe-installation",
                    owner_id=installation.id,
                    state=installation.state,
                    classification="active-work",
                    detail="accepted installation uses this model set",
                    reason=f"installation {installation.id}",
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
