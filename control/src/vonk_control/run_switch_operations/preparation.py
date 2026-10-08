"""Preparation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    RunSwitchCode,
)

from ..models import (
    STOPPABLE_RUN_STATES,
    CatalogDocumentRevision,
    RecipeBuild,
    RecipeRun,
    RunNode,
)
from ..preparation_contract import (
    ControllerAssetState,
    ModelArtifactPreparation,
    PreparationReason,
    RolloutPreparation,
    RuntimeImagePreparation,
    TargetAssetState,
    controller_assets_ready,
)
from ..recipe_operations import (
    RecipeOperationConflict,
)
from ..recipe_runtime_specs import (
    RUNTIME_INTERFACE,
)
from ..run_switch_contract import (
    RunSwitchReason,
    RuntimeImageStorageImpact,
    SparkFit,
    SparkGroup,
    StopImpact,
)
from .identity_helpers import (
    _is_hex_digest,
    _is_oci_digest,
    _node_missing_bytes,
    _primary_model_digest,
)
from .interfaces import ArtifactInspection
from .planning_helpers import _as_reason

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class PreparationMixin:
    @staticmethod
    def _preparation(
        *,
        revision: CatalogDocumentRevision | None,
        group: SparkGroup,
        inspection: ArtifactInspection,
        build: RecipeBuild | None,
        build_candidate: RecipeBuild | None,
        runtime_storage: RuntimeImageStorageImpact,
        now: datetime,
        reasons: Sequence[RunSwitchReason],
    ) -> RolloutPreparation | None:
        """Normalize model and runtime asset readiness for shared callers."""

        if revision is None or (build is None and runtime_storage.image_digest is None):
            # There is no honest immutable runtime image identity to place in
            # RolloutPreparation until a successful build receipt
            # exists.
            return None
        primary_model_digest = _primary_model_digest(revision.document)
        if primary_model_digest is None:
            return None
        model_digests = tuple(inspection.artifact_digests)
        model_expected = inspection.artifact_set_bytes
        if model_expected is None or model_expected < 1:
            # The shared preparation contract requires the exact model set
            # size.  Keep this unknown rather than manufacturing a byte count.
            return None
        artifact_set_digest = inspection.artifact_set_sha256
        if not _is_hex_digest(artifact_set_digest):
            return None
        artifact_set_bytes = inspection.artifact_set_bytes or model_expected
        if artifact_set_bytes != model_expected:
            return None
        model_missing_by_node = inspection.missing_spark_bytes_by_node
        model_controller_expected = model_expected
        model_controller_verified = (
            model_controller_expected
            if inspection.nas_coverage == "complete"
            and inspection.missing_nas_bytes in (None, 0)
            else 0
        )
        model_controller_missing = (
            None
            if model_controller_expected is None
            else model_controller_expected - model_controller_verified
        )
        model_controller_ready = (
            model_controller_expected is not None
            and model_controller_missing == 0
            and inspection.nas_coverage == "complete"
        )
        model_controller = ControllerAssetState(
            state="ready" if model_controller_ready else "unknown",
            expected_bytes=model_controller_expected,
            verified_bytes=model_controller_verified,
            missing_bytes=model_controller_missing,
            verified_sha256=artifact_set_digest if model_controller_ready else None,
            verified_at=now if model_controller_ready else None,
            source="nas-cache" if inspection.nas_coverage != "unknown" else "unknown",
            reason=(
                None
                if model_controller_ready
                else "NAS coverage for the exact model artifact set is not proven."
            ),
        )
        model_targets: list[TargetAssetState] = []
        for node in group.nodes:
            model_missing = _node_missing_bytes(
                model_missing_by_node,
                node.node_id,
                inspection.missing_spark_bytes,
                len(group.nodes),
            )
            target_ready = model_expected is not None and model_missing == 0
            model_targets.append(
                TargetAssetState(
                    node_id=node.node_id,
                    state="ready" if target_ready else "unknown",
                    expected_bytes=model_expected,
                    present_bytes=(
                        max(0, model_expected - model_missing)
                        if model_missing is not None
                        else 0
                    ),
                    # Unknown per-node evidence means "treat as missing"; the
                    # distribution step then copies or verifies it.
                    missing_bytes=(
                        model_missing if model_missing is not None else model_expected
                    ),
                    verified_sha256=artifact_set_digest if target_ready else None,
                    verified_at=now if target_ready else None,
                    reason=(
                        None
                        if target_ready
                        else "The exact model artifact set is not verified on this Spark."
                    ),
                )
            )
        model_completeness = (
            "unknown"
            if not model_digests
            else "complete"
            if model_controller_ready
            and all(target.state == "ready" for target in model_targets)
            else "incomplete"
        )
        model = ModelArtifactPreparation(
            artifact_set_sha256=artifact_set_digest,
            model_content_sha256=primary_model_digest,
            recipe_revision_sha256=revision.content_digest,
            artifact_count=max(1, len(model_digests)),
            artifact_set_bytes=artifact_set_bytes,
            dependency_model_content_sha256=sorted(
                set(inspection.dependency_model_content_sha256)
            ),
            completeness=model_completeness,
            controller=model_controller,
            targets=model_targets,
        )
        image_digest = (
            build.image_digest if build is not None else runtime_storage.image_digest
        )
        image_bytes = (
            build.image_bytes if build is not None else runtime_storage.image_bytes
        )
        layout_digest = (
            build.oci_layout_sha256
            if build is not None
            else runtime_storage.oci_layout_sha256
        )
        if (
            not _is_oci_digest(image_digest)
            or image_bytes is None
            or not _is_hex_digest(layout_digest)
        ):
            return None
        runtime_controller = ControllerAssetState(
            state=(
                "ready"
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else LifecycleState.FAILED.value
                if any(
                    reason.code == RunSwitchCode.RUNTIME_IMAGE_AUTHORIZATION_MISMATCH
                    for reason in reasons
                )
                else "missing"
                if runtime_storage.missing_nas_bytes not in (None, 0)
                else "unknown"
            ),
            expected_bytes=image_bytes,
            verified_bytes=(
                image_bytes
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else 0
            ),
            missing_bytes=(
                0
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else image_bytes
            ),
            verified_sha256=(
                layout_digest
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else None
            ),
            verified_at=(
                now
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else None
            ),
            source="controller-build",
            reason=(
                None
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else next(
                    (
                        reason.detail[:256]
                        for reason in reasons
                        if reason.code
                        == RunSwitchCode.RUNTIME_IMAGE_AUTHORIZATION_MISMATCH
                    ),
                    "The exact OCI archive is missing from Controller storage."
                    if runtime_storage.missing_nas_bytes not in (None, 0)
                    else "Controller storage coverage for the exact OCI image is not proven.",
                )
            ),
        )
        runtime_targets = []
        for node in group.nodes:
            image_missing = _node_missing_bytes(
                runtime_storage.missing_image_distribution_bytes_by_node,
                node.node_id,
                runtime_storage.missing_image_distribution_bytes,
                len(group.nodes),
            )
            image_ready = image_missing == 0
            runtime_targets.append(
                TargetAssetState(
                    node_id=node.node_id,
                    state="ready" if image_ready else "unknown",
                    expected_bytes=image_bytes,
                    present_bytes=(
                        max(0, image_bytes - image_missing)
                        if image_missing is not None
                        else 0
                    ),
                    missing_bytes=(
                        image_missing if image_missing is not None else image_bytes
                    ),
                    verified_sha256=layout_digest if image_ready else None,
                    verified_at=now if image_ready else None,
                    imported_image_digest=image_digest if image_ready else None,
                    reason=(
                        None
                        if image_ready
                        else "The exact OCI image is not imported on this Spark."
                    ),
                )
            )
        runtime = RuntimeImagePreparation(
            image_digest=image_digest,
            oci_layout_sha256=layout_digest,
            image_bytes=image_bytes,
            architecture="linux-arm64",
            runtime_interface=RUNTIME_INTERFACE,
            build_id=build.id
            if build is not None
            else build_candidate.id
            if build_candidate is not None
            else None,
            controller=runtime_controller,
            targets=runtime_targets,
        )
        prep_reasons = [
            PreparationReason(
                code=reason.code,
                detail=reason.detail,
                severity=reason.severity,
                node_ids=reason.node_ids,
            )
            for reason in reasons
            if reason.severity in {"blocker", "warning", "info"}
        ]
        controller_ready = controller_assets_ready(model, runtime)
        targets_ready = all(
            target.state == "ready"
            for asset in (model, runtime)
            for target in asset.targets
        )
        ready = (
            controller_ready
            and targets_ready
            and not any(reason.severity == "blocker" for reason in prep_reasons)
        )
        return RolloutPreparation(
            model=model,
            runtime_image=runtime,
            exceptions=[],
            target_node_ids=sorted(node.node_id for node in group.nodes),
            controller_ready=controller_ready,
            targets_ready=targets_ready,
            ready=ready,
            reasons=prep_reasons,
        )

    def _conflicts(
        self,
        session: Session,
        node_ids: tuple[str, ...],
        *,
        action: str,
    ) -> tuple[list[RunSwitchReason], list[StopImpact], list[RunSwitchReason]]:
        service = typing_cast("RunSwitchOperationService", self)
        conflicts: list[RunSwitchReason] = []
        stops: list[StopImpact] = []
        blockers: list[RunSwitchReason] = []
        rows = tuple(
            session.scalars(
                select(RecipeRun)
                .where(
                    RecipeRun.id.in_(
                        select(RunNode.run_id).where(RunNode.node_id.in_(node_ids))
                    ),
                    RecipeRun.state.in_(STOPPABLE_RUN_STATES),
                )
                .order_by(RecipeRun.created_at, RecipeRun.id)
            )
        )
        wanted = set(node_ids)
        for run in rows:
            run_nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run.id)
                    .order_by(RunNode.rank)
                )
            )
            run_ids = {node.node_id for node in run_nodes}
            overlap = wanted & run_ids
            if not overlap:
                continue
            if run_ids - wanted:
                reason = _as_reason(
                    RunSwitchCode.CROSS_GROUP_CONFLICT,
                    "An active distributed run crosses the selected complete Spark group; partial stop is unsafe.",
                    scope="conflict",
                    node_ids=tuple(sorted(overlap)),
                )
                conflicts.append(reason)
                blockers.append(reason)
                continue
            if action == "run":
                reason = _as_reason(
                    RunSwitchCode.ACTIVE_RUN_CONFLICT,
                    "The selected Spark group already has an active workload and must be stopped before starting this outcome.",
                    scope="conflict",
                    severity="warning",
                    node_ids=tuple(sorted(run_ids)),
                )
                conflicts.append(reason)
            try:
                stop_plan = (
                    service._lifecycle.preview_stop(run.id)
                    if service._lifecycle
                    else None
                )
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                stop_plan = None
            if stop_plan is None or not stop_plan.allowed:
                reason = _as_reason(
                    RunSwitchCode.STOP_PLAN_UNAVAILABLE,
                    "The existing workload cannot be represented by a safe stop plan.",
                    scope="conflict",
                    node_ids=tuple(sorted(run_ids)),
                )
                blockers.append(reason)
                continue
            stops.append(
                StopImpact(
                    run_id=run.id,
                    run_plan_digest=run.plan_digest,
                    alias=run.alias,
                    state=run.state,
                    node_ids=sorted(run_ids),
                    reserved_bytes=service._run_reserved_bytes(session, run.id),
                    plan_digest=stop_plan.plan_digest,
                )
            )
        return conflicts, stops, blockers

    @staticmethod
    def _placement_fit(fit: SparkFit, blockers: Sequence[RunSwitchReason]) -> SparkFit:
        all_blockers = [*blockers, *fit.blockers]
        return fit.model_copy(
            update={"allowed": not all_blockers, "blockers": all_blockers}
        )
