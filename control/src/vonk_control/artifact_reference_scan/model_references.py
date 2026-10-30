"""Artifact reference scan: model references."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    canonical_message,
)

from .. import model_cache_states
from ..artifact_lifecycle import (
    ArtifactReferenceUnverified,
)
from ..content_identity import installations_for_sets, profile_recipe_scope
from ..machine_states import DISTRIBUTION_HELD, RUN_LIVE
from ..model_cache_contract import CacheManifest
from ..models import (
    ArtifactDistributionAssignment,
    FleetProfile,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeInstallation,
    RecipeRun,
)
from .intents import (
    _installation_plan,
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


def model_set_reference_findings(
    session: Session, set_digests: Iterable[str]
) -> dict[str, tuple[ArtifactReferenceFinding, ...]]:
    """Find typed saved-profile and active owners of exact model sets."""

    from ..model_cache.lineage import set_bindings

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

    # A set serves every model content it was first requested for or later
    # verified (a refreshed model reuses the set of its first requester), so a
    # reference matches on that, never on the first requester alone.
    served: dict[str, frozenset[str] | None] = {}
    for set_digest, row in sets.items():
        try:
            manifest = CacheManifest.model_validate_json(
                canonical_message(row.manifest), strict=True
            )
            served[set_digest] = set_bindings(
                row.model_content_sha256, manifest.verified_models
            )
        except (TypeError, ValueError):
            served[set_digest] = None

    held_models = {model for found in served.values() for model in found or ()} | {
        row.model_content_sha256 for row in sets.values() if row.model_content_sha256
    }
    held_recipes = {
        row.recipe_revision_sha256
        for row in sets.values()
        if row.recipe_revision_sha256
    }

    # A profile names recipes; each active recipe revision is bound to the exact
    # model contents it uses by relational rows. Only the selectors that match
    # these sets, or cannot be proven, are read.
    profiles = list(session.scalars(select(FleetProfile).order_by(FleetProfile.id)))
    named = {profile.id: saved_profile_selectors(profile) for profile in profiles}
    scope = profile_recipe_scope(
        session,
        {
            _selector_name(selector)
            for chosen in named.values()
            for selector in chosen or ()
        },
        models=held_models,
        recipes=held_recipes,
    )
    for profile in profiles:
        selectors = named[profile.id]
        if selectors is None:
            _protect_saved(findings, profile, "model-set", selected)
            continue
        if selectors:
            # A manifest that cannot be read has no provable models.
            _protect_saved(
                findings,
                profile,
                "model-set",
                {digest for digest, found in served.items() if found is None},
            )
        for selector in selectors:
            pair = _selector_name(selector)
            if pair in scope.unknown:
                _protect_saved(findings, profile, "model-set", selected)
                continue
            for model, recipe in scope.matched.get(pair, ()):
                _protect_saved(
                    findings,
                    profile,
                    "model-set",
                    {
                        set_digest
                        for set_digest, row in sets.items()
                        if row.recipe_revision_sha256 == recipe
                        or model in (served[set_digest] or ())
                    },
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

    for use in installations_for_sets(
        session,
        models=held_models,
        recipes=held_recipes,
        set_digests=selected,
        active=or_(
            RecipeInstallation.state.in_(_ACTIVE_INSTALLATIONS),
            select(RecipeRun.id)
            .where(
                RecipeRun.installation_id == RecipeInstallation.id,
                RecipeRun.state.in_(RUN_LIVE),
            )
            .exists(),
        ),
    ):
        if use.attributable:
            # The installation's model, its recipe's bound models and the sets
            # its plan names say which sets it uses, without reading its plan.
            held = {
                set_digest
                for set_digest, cached in sets.items()
                if cached.recipe_revision_sha256 == use.recipe
                or use.used
                & (served[set_digest] or frozenset({cached.model_content_sha256}))
            } | (use.named_sets & selected)
            detail = "accepted installation uses this model set"
            label = f"installation {use.id}"
        else:
            # Its columns attribute nothing: read its own plan. Unreadable, it
            # keeps the sets of the model and recipe its row names, or every
            # selected set when it names none.
            installation = session.get(RecipeInstallation, use.id)
            assert installation is not None
            plan = _installation_plan(installation)
            if plan is not None:
                held = selected & {
                    compiled.identity.model_artifact_set_sha256
                    for compiled in plan.compiled_execution_plans.values()
                }
                detail = "accepted installation uses this model set"
                label = f"installation {use.id}"
            else:
                held = (
                    {
                        set_digest
                        for set_digest, cached in sets.items()
                        if cached.model_content_sha256 in {None, use.model}
                        or cached.recipe_revision_sha256 == use.recipe
                    }
                    if use.model is not None or use.recipe is not None
                    else selected
                )
                detail = "installation plan is unreadable; it keeps the sets it names"
                label = f"installation {use.id} (plan unreadable)"
        for set_digest in held:
            findings[set_digest].add(
                _finding(
                    "model-set",
                    set_digest,
                    owner_kind="recipe-installation",
                    owner_id=use.id,
                    state=use.state,
                    classification="active-work",
                    detail=detail,
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
