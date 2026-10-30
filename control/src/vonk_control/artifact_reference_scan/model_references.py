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
from ..content_identity import recipe_model_digests
from ..machine_states import DISTRIBUTION_HELD, RUN_LIVE
from ..models import (
    ArtifactDistributionAssignment,
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
    _installation_plan,
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


def model_set_reference_findings(
    session: Session, set_digests: Iterable[str]
) -> dict[str, tuple[ArtifactReferenceFinding, ...]]:
    """Find typed saved-profile and active owners of exact model sets."""

    from ..model_cache.errors import ModelCacheError
    from ..model_cache.resolution import resolve_recipe_set

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

    # A set is held when it is the set a protected recipe revision resolves to,
    # as a download resolves it (identical files resolve to the same set). A
    # revision that cannot be resolved holds the sets bound to its models.
    present = set(
        session.scalars(
            select(ModelCacheSetArtifact.artifact_set_sha256)
            .where(ModelCacheSetArtifact.artifact_set_sha256.in_(sorted(selected)))
            .distinct()
        )
    )
    for set_digest in selected - present:
        label = f"model set {set_digest} has no membership"
        findings[set_digest].add(
            _finding(
                "model-set",
                set_digest,
                owner_kind="model-cache-set",
                owner_id=set_digest,
                state="unverified",
                classification="active-work",
                detail="the set's membership is unavailable, so it is kept",
                reason=label,
            )
        )

    profiles = list(session.scalars(select(FleetProfile).order_by(FleetProfile.id)))
    named = {profile.id: saved_profile_selectors(profile) for profile in profiles}
    resolved = _selectors_revisions(
        session, {selector for chosen in named.values() for selector in chosen or ()}
    )
    running = list(
        session.scalars(
            select(RecipeInstallation)
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
    revision_ids = {revision.id for found in resolved.values() for revision in found}
    bound = recipe_model_digests(
        session, revision_ids | {row.recipe_revision_id for row in running}
    )

    def bound_sets(models: Iterable[str]) -> set[str]:
        """The selected sets first cached for these models (no parsing)."""
        wanted = set(models)
        return {
            digest for digest, row in sets.items() if row.model_content_sha256 in wanted
        }

    resolves: dict[str, str | None] = {}

    def resolved_set(ident: str) -> str | None:
        if ident not in resolves:
            try:
                resolves[ident] = resolve_recipe_set(session, ident).digest
            except (ModelCacheError, TypeError, ValueError):
                resolves[ident] = None
        return resolves[ident]

    for profile in profiles:
        selectors = named[profile.id]
        if selectors is None:
            _protect_saved(findings, profile, "model-set", selected)
            continue
        for selector in selectors:
            found = resolved[selector]
            held = set() if found else set(selected)
            for revision in found:
                digest = resolved_set(revision.id)
                held |= (
                    {digest} & selected
                    if digest is not None
                    else bound_sets(bound.get(revision.id, ()))
                )
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
        plan = _installation_plan(row)
        if plan is not None:
            held = selected & {
                compiled.identity.model_artifact_set_sha256
                for compiled in plan.compiled_execution_plans.values()
            }
        else:
            # Its plan cannot be read: it keeps the sets of the models its rows name.
            held = bound_sets(
                bound.get(row.recipe_revision_id, frozenset())
                | ({row.model_content_sha256} if row.model_content_sha256 else set())
            )
        label = f"installation {row.id}"
        for set_digest in held:
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
