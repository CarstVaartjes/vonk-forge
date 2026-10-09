"""Artifact reference scan: image references."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    WaitReason,
    canonical_message,
)

from ..artifact_lifecycle import (
    ArtifactReferenceUnverified,
)
from ..machine_states import DISTRIBUTION_HELD
from ..models import (
    ArtifactDistributionAssignment,
    FleetProfile,
    FleetProfileApplication,
    Job,
    RecipeInstallation,
    RecipeRun,
)
from ..revision_images import revision_archives, revisions_running_archives
from ..runtime_image_preparation.contracts import read_runtime_image_reference_intent
from .intents import (
    _profile_plan,
    _run_switch_kinds,
    _run_switch_plan,
    _run_switch_runtime_image_intent,
)
from .model_sets import _protect_saved, _selector_revisions, saved_profile_selectors
from .types import (
    _ACTIVE_ARTIFACT_JOBS,
    _ACTIVE_INSTALLATIONS,
    _ACTIVE_PROFILE_APPLICATIONS,
    _ACTIVE_RUN_SWITCH_JOBS,
    _ACTIVE_RUNS,
    ArtifactReferenceFinding,
    _finding,
    _reason_projection,
)


def runtime_image_reference_findings(
    session: Session, archive_digests: Iterable[str]
) -> dict[str, tuple[ArtifactReferenceFinding, ...]]:
    """Find typed saved-profile and active owners of exact runtime-image bytes."""

    selected = set(archive_digests)
    findings: dict[str, set[ArtifactReferenceFinding]] = {
        digest: set() for digest in selected
    }
    if not selected:
        return {}

    for profile in session.scalars(select(FleetProfile).order_by(FleetProfile.id)):
        selectors = saved_profile_selectors(profile)
        if selectors is None:
            _protect_saved(findings, profile, "runtime-image", selected)
            continue
        for selector in selectors:
            revisions = _selector_revisions(session, selector)
            if not revisions:
                _protect_saved(findings, profile, "runtime-image", selected)
                continue
            archives = revision_archives(
                session, [revision.id for revision in revisions]
            )
            _protect_saved(findings, profile, "runtime-image", selected & archives)

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
        for preparation in _profile_plan(application.plan).preparation_decisions:
            archive = preparation.runtime_image.oci_layout_sha256
            if archive in selected:
                findings[archive].add(
                    _finding(
                        "runtime-image",
                        archive,
                        owner_kind="fleet-profile-application",
                        owner_id=application.id,
                        state=application.state,
                        classification="active-work",
                        detail="accepted profile application prepares this runtime image",
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
            account(operation.result)
            plan = _run_switch_plan(operation.payload)
            archive = plan.runtime_storage.oci_layout_sha256
            if archive in selected:
                findings[archive].add(
                    _finding(
                        "runtime-image",
                        archive,
                        owner_kind="run-switch-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="accepted Run/Switch operation uses this runtime image",
                        reason=f"run/switch operation {operation.id}",
                    )
                )
            intent = _run_switch_runtime_image_intent(operation, plan)
            if intent is not None and intent.archive_sha256 in selected:
                findings[intent.archive_sha256].add(
                    _finding(
                        "runtime-image",
                        intent.archive_sha256,
                        owner_kind="run-switch-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="accepted Run/Switch operation is preparing this runtime image",
                        reason=(
                            f"run/switch operation {operation.id} "
                            "runtime image preparation"
                        ),
                    )
                )

    for installation in session.scalars(
        select(RecipeInstallation)
        .where(RecipeInstallation.state.in_(_ACTIVE_INSTALLATIONS))
        .order_by(RecipeInstallation.id)
        .execution_options(yield_per=64)
    ):
        account(installation.plan)
        archive = _run_switch_plan(
            {"plan": installation.plan}
        ).runtime_storage.oci_layout_sha256
        if archive in selected:
            findings[archive].add(
                _finding(
                    "runtime-image",
                    archive,
                    owner_kind="recipe-installation",
                    owner_id=installation.id,
                    state=installation.state,
                    classification="active-work",
                    detail="accepted installation uses this runtime image",
                    reason=f"installation {installation.id}",
                )
            )

    for distribution in session.scalars(
        select(ArtifactDistributionAssignment)
        # Expiry is only a serving deadline; it does not fence a stale worker.
        .where(ArtifactDistributionAssignment.state.in_(DISTRIBUTION_HELD))
        .order_by(ArtifactDistributionAssignment.id)
        .execution_options(yield_per=64)
    ):
        if distribution.oci_archive_sha256 in selected:
            findings[distribution.oci_archive_sha256].add(
                _finding(
                    "runtime-image",
                    distribution.oci_archive_sha256,
                    owner_kind="artifact-distribution-assignment",
                    owner_id=distribution.id,
                    state=distribution.state,
                    classification="active-work",
                    detail="accepted distribution assignment retains the runtime image",
                    reason=f"active distribution assignment {distribution.id}",
                )
            )

    for run in session.scalars(
        select(RecipeRun)
        .where(RecipeRun.state.in_(_ACTIVE_RUNS))
        .order_by(RecipeRun.id)
        .execution_options(yield_per=64)
    ):
        installation = session.get(RecipeInstallation, run.installation_id)
        if installation is None:
            raise ArtifactReferenceUnverified(
                ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
                "active run installation could not be read; removal was deferred",
                retryable=True,
            )
        account(installation.plan)
        archive = _run_switch_plan(
            {"plan": installation.plan}
        ).runtime_storage.oci_layout_sha256
        if archive in selected:
            findings[archive].add(
                _finding(
                    "runtime-image",
                    archive,
                    owner_kind="recipe-run",
                    owner_id=run.id,
                    state=run.state,
                    classification="active-work",
                    detail="active recipe run retains its installed runtime image",
                    reason=f"active run {run.id}",
                )
            )

    from ..recipe_image_availability import OPERATION_KIND

    image_revisions = revisions_running_archives(session, selected)

    for operation in session.scalars(
        select(Job)
        .where(
            Job.kind == OPERATION_KIND,
            Job.state.in_(_ACTIVE_ARTIFACT_JOBS),
        )
        .order_by(Job.id)
        .execution_options(yield_per=64)
    ):
        payload = operation.payload
        if not isinstance(payload, Mapping):
            raise ArtifactReferenceUnverified(
                ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
                "active artifact operation payload is malformed; removal was deferred",
                retryable=True,
            )
        associated_archives = [
            archive
            for archive, revisions in image_revisions.items()
            if operation.authority_revision in revisions
        ]
        if associated_archives:
            account(payload)
            for archive in associated_archives:
                findings[archive].add(
                    _finding(
                        "runtime-image",
                        archive,
                        owner_kind="recipe-image-availability-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="active image preparation is authorized for this revision",
                        reason=(
                            f"active image preparation {operation.id} "
                            "for recipe revision"
                        ),
                    )
                )
        raw_reference = payload.get("image_reference_intent")
        if raw_reference is not None:
            reference = read_runtime_image_reference_intent(raw_reference)
            claim_owner = payload.get("claim_owner")
            if (
                reference is None
                or not isinstance(claim_owner, str)
                or not reference.belongs_to(
                    operation_id=operation.id,
                    recipe_revision_id=operation.authority_revision,
                    attempt=operation.current_attempt,
                    claim_owner=claim_owner,
                )
            ):
                raise ArtifactReferenceUnverified(
                    ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
                    "active runtime image reference intent is not owned by its current attempt; removal was deferred",
                    retryable=True,
                    reason=WaitReason.SCOPE_CHANGED,
                )
            account(reference)
            if reference.oci_archive_sha256 in selected:
                findings[reference.oci_archive_sha256].add(
                    _finding(
                        "runtime-image",
                        reference.oci_archive_sha256,
                        owner_kind="recipe-image-availability-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="active image operation has an exact publication intent",
                        reason=f"image publication operation {operation.id}",
                    )
                )
        image_result = payload.get("image_result")
        if isinstance(image_result, Mapping):
            archive = image_result.get("oci_archive_sha256")
            if isinstance(archive, str) and archive in selected:
                account(image_result)
                findings[archive].add(
                    _finding(
                        "runtime-image",
                        archive,
                        owner_kind="recipe-image-availability-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="active image operation has a verified publication result",
                        reason=f"image operation {operation.id}",
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


def runtime_image_reference_reasons(
    session: Session, archive_digests: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """Project typed runtime-image findings into existing presentation reasons."""

    return _reason_projection(
        runtime_image_reference_findings(session, archive_digests)
    )
