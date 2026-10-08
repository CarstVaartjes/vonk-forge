"""Phase planning."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Literal,
)
from typing import cast as typing_cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationState,
    ReservationState,
    RunSwitchCode,
)

from ..cluster_mappings import (
    mapping_option_choices,
    validate_mapping_parameters,
)
from ..models import (
    ClusterMapping,
    ClusterMappingNode,
    ResourceReservation,
)
from ..recipe_operations import (
    RecipeOperationConflict,
)
from ..run_switch_contract import (
    ArtifactStorageImpact,
    MappingSelection,
    RunSwitchAction,
    RunSwitchPhase,
    RunSwitchPhaseKind,
    RunSwitchPlan,
    RunSwitchReason,
    RunSwitchRetention,
    RunSwitchSubphase,
    RuntimeImageStorageImpact,
    SparkGroup,
    SparkGroupNode,
    StopImpact,
)
from .identity_helpers import _is_hex_digest
from .interfaces import ArtifactInspection
from .planning_helpers import (
    _as_reason,
    _digest,
    _inspection_unavailable,
    _plan_identity,
)

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class PhasePlanningMixin:
    def _inspect_artifacts(
        self,
        session: Session,
        model_digest: str | None,
        revision_id: str | None,
        group: SparkGroup,
        *,
        retention: str,
        now: datetime,
    ) -> ArtifactInspection:
        service = typing_cast("RunSwitchOperationService", self)
        if model_digest is None or revision_id is None:
            return ArtifactInspection(
                required_bytes=None,
                reused_bytes=0,
                copied_bytes=0,
                missing_nas_bytes=None,
                missing_spark_bytes=None,
                reclaimable_bytes=0,
                nas_coverage="unknown",
                spark_coverage="unknown",
                artifact_digests=(),
                reclaimable_digests=(),
                blockers=(
                    _as_reason(
                        RunSwitchCode.ARTIFACT_IDENTITY_UNKNOWN,
                        "The exact model artifact identity is unavailable.",
                        scope="artifact",
                    ),
                ),
            )
        try:
            inspection = service._artifacts.inspect(
                session,
                model_content_sha256=model_digest,
                recipe_revision_id=revision_id,
                node_ids=tuple(node.node_id for node in group.nodes),
                retention=retention,
                now=now,
            )
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            return _inspection_unavailable(str(error))
        if (
            inspection.missing_spark_bytes not in (None, 0)
            and inspection.nas_coverage == "unknown"
        ):
            inspection = ArtifactInspection(
                required_bytes=inspection.required_bytes,
                reused_bytes=inspection.reused_bytes,
                copied_bytes=inspection.copied_bytes,
                missing_nas_bytes=inspection.missing_nas_bytes,
                missing_spark_bytes=inspection.missing_spark_bytes,
                missing_spark_bytes_by_node=inspection.missing_spark_bytes_by_node,
                reclaimable_bytes=inspection.reclaimable_bytes,
                nas_coverage=inspection.nas_coverage,
                spark_coverage=inspection.spark_coverage,
                artifact_digests=inspection.artifact_digests,
                reclaimable_digests=inspection.reclaimable_digests,
                artifact_set_sha256=inspection.artifact_set_sha256,
                artifact_set_bytes=inspection.artifact_set_bytes,
                dependency_model_content_sha256=inspection.dependency_model_content_sha256,
                freshness=inspection.freshness,
                blockers=(
                    *inspection.blockers,
                    _as_reason(
                        RunSwitchCode.NAS_COVERAGE_UNKNOWN,
                        "Spark copies are missing but NAS coverage is unknown; the Controller cannot promise a reusable source.",
                        scope="artifact",
                    ),
                ),
                warnings=inspection.warnings,
            )
        if (
            inspection.required_bytes is None
            or inspection.required_bytes < 1
            or not inspection.artifact_digests
            or not _is_hex_digest(inspection.artifact_set_sha256)
            or any(not _is_hex_digest(value) for value in inspection.artifact_digests)
        ):
            inspection = replace(
                inspection,
                blockers=(
                    *inspection.blockers,
                    _as_reason(
                        RunSwitchCode.ARTIFACT_MANIFEST_UNKNOWN,
                        "The authoritative complete model artifact set and byte manifest are unavailable.",
                        scope="artifact",
                    ),
                ),
            )
        return inspection

    @staticmethod
    def _storage(
        inspection: ArtifactInspection, *, retention: RunSwitchRetention
    ) -> ArtifactStorageImpact:
        return ArtifactStorageImpact(
            artifact_set_sha256=inspection.artifact_set_sha256,
            artifact_set_bytes=inspection.artifact_set_bytes,
            required_bytes=inspection.required_bytes,
            reused_bytes=inspection.reused_bytes,
            copied_bytes=inspection.copied_bytes,
            missing_nas_bytes=inspection.missing_nas_bytes,
            missing_spark_bytes=inspection.missing_spark_bytes,
            missing_spark_bytes_by_node=(
                dict(inspection.missing_spark_bytes_by_node)
                if inspection.missing_spark_bytes_by_node is not None
                else None
            ),
            reclaimable_bytes=inspection.reclaimable_bytes,
            reclaimed_bytes=(
                inspection.reclaimable_bytes
                if retention == "reclaim-unreferenced"
                else 0
            ),
            nas_coverage=inspection.nas_coverage,
            spark_coverage=inspection.spark_coverage,
            retention=retention,
            artifact_digests=list(inspection.artifact_digests),
            reclaimable_digests=list(inspection.reclaimable_digests),
        )

    def _phases(
        self,
        *,
        action: RunSwitchAction,
        group: SparkGroup,
        phase_node_ids: Sequence[str] | None = None,
        installation_id: str | None,
        installation_state: str | None,
        stops: Sequence[StopImpact],
        inspection: ArtifactInspection,
        partial_stop: bool = False,
        runtime_storage: RuntimeImageStorageImpact | None,
        retention: RunSwitchRetention,
        blockers: Sequence[RunSwitchReason],
        stop_before_transfer: bool,
        stop_before_prepare: bool,
        build_required: bool = False,
        build_on_target: bool = False,
        cleanup_disposition: Literal["uninstall", "abandon"] = "uninstall",
        cleanup_mode: Literal["uninstall", "reconcile"] = "uninstall",
    ) -> list[RunSwitchPhase]:
        node_ids = list(
            phase_node_ids
            if phase_node_ids is not None
            else (node.node_id for node in group.nodes)
        )
        phases: list[RunSwitchPhase] = []
        if action == "cleanup":
            # Disposal is authorized by the installation's own uninstall
            # assessment, so the phase sequence never consults launch
            # readiness: being unable to start work must not prevent removing
            # work.  A plan that never reached a node has nothing to remove,
            # so its first phase abandons the record instead of uninstalling it.
            phases.append(
                RunSwitchPhase(
                    index=0,
                    kind="uninstall",
                    state="planned" if not blockers else "blocked",
                    node_ids=node_ids,
                    detail=(
                        "Abandon the persisted plan that never reached a node; "
                        "there are no installed bytes to remove."
                        if cleanup_disposition == "abandon"
                        else "Reconcile the exact stored installation effects from "
                        "successful install provenance while preserving shared caches."
                        if cleanup_mode == "reconcile"
                        else "Remove the installation that is no longer desired, "
                        "scoped to its authorized membership."
                    ),
                )
            )
            phases.append(
                RunSwitchPhase(
                    index=len(phases),
                    kind="final_verify",
                    state="planned" if not blockers else "blocked",
                    node_ids=node_ids,
                    detail=(
                        "Verify exact reconciliation receipts and retained reservations "
                        "for every rank before final release."
                        if cleanup_mode == "reconcile"
                        else "Verify the installation and its runtime are removed."
                    ),
                )
            )
            return phases
        if action == "stop":
            if stops:
                phases.append(
                    RunSwitchPhase(
                        index=0,
                        kind="stop",
                        state="planned" if not blockers else "blocked",
                        node_ids=node_ids,
                        detail=(
                            "Stop the selected workload on the reachable Sparks; "
                            "the profile will report the missing ranks separately."
                            if partial_stop
                            else "Stop the selected workload as one complete group."
                        ),
                    )
                )
            phases.append(
                RunSwitchPhase(
                    index=len(phases),
                    kind="final_verify",
                    state="planned" if not blockers else "blocked",
                    node_ids=node_ids,
                    detail=(
                        "Verify that reachable ranks are stopped and the model route "
                        "is withdrawn."
                        if partial_stop
                        else "Verify that the selected workload is stopped and its route is withdrawn."
                    ),
                )
            )
            return phases

        needs_model_download = inspection.missing_nas_bytes not in (None, 0)
        needs_target_copy = (
            installation_id is None
            or inspection.missing_spark_bytes not in (None, 0)
            or (
                runtime_storage is not None
                and runtime_storage.missing_image_distribution_bytes not in (None, 0)
            )
        )
        # Image preparation is a Controller-side phase.  It must complete
        # before install admission compiles and persists the schema-2 agent
        # payload, even when the selected Spark already has a copy.  A pending
        # source build has no image identity at preview time, so it is also an
        # explicit preparation input for the same durable phase sequence.
        needs_runtime_image_prepare = (
            build_required
            or (runtime_storage is not None and runtime_storage.preparation_required)
            or (
                runtime_storage is not None
                and runtime_storage.missing_nas_bytes not in (None, 0)
            )
            or (
                runtime_storage is not None
                and runtime_storage.missing_image_distribution_bytes not in (None, 0)
            )
            or (
                runtime_storage is not None
                and runtime_storage.image_digest is not None
                and runtime_storage.oci_layout_sha256 is None
            )
        )
        needs_prepare = (
            installation_id is None or installation_state != InstallationState.INSTALLED
        )
        needs_cleanup = retention == "reclaim-unreferenced" and (
            inspection.reclaimable_bytes > 0
            or (runtime_storage is not None and runtime_storage.reclaimable_bytes > 0)
        )
        stop_added = False

        def add(
            kind: RunSwitchPhaseKind,
            detail: str,
            *,
            subphase: RunSwitchSubphase | None = None,
        ) -> None:
            phases.append(
                RunSwitchPhase(
                    index=len(phases),
                    kind=kind,
                    subphase=subphase,
                    state="blocked"
                    if blockers and kind in {"prepare", "start", "final_verify"}
                    else "planned",
                    node_ids=node_ids,
                    detail=detail,
                )
            )

        # Only a Spark build inside the group needs the memory the old
        # workload holds. Controller image preparation, transfer and install
        # run beside it, so without such a build the old workload keeps
        # serving until the one stop right before start (whose post-stop
        # memory check still guards the start).
        stop_for_build = stop_before_prepare and build_required and build_on_target
        if build_required and not build_on_target:
            # Controller build output is an input to transfer.  Keep it ahead
            # of target disk cleanup/stop because this builder is outside the
            # selected inference group.
            add(
                "prepare",
                "Build the exact linux-arm64 runtime container in Controller storage before target transfer.",
                subphase="container-build",
            )
        if stops and stop_before_transfer:
            add("stop", "Stop conflicting workloads before disk capacity is consumed.")
            stop_added = True
        if build_required and build_on_target and not stop_before_prepare:
            add(
                "prepare",
                "Build the exact linux-arm64 runtime container in Controller storage before target transfer.",
                subphase="container-build",
            )
        if needs_model_download:
            add(
                "transfer",
                "Download the exact model artifact set into Controller/NAS cache before Spark distribution.",
                subphase="model-download",
            )
        if stops and stop_for_build and not stop_added:
            add(
                "stop",
                "Stop conflicting workloads before the Spark build needs their memory.",
            )
            stop_added = True
        if stop_for_build:
            # A build outside the group was already placed first above.
            # This remains a ``prepare`` phase for the shared lifecycle
            # vocabulary; the subphase makes Controller OCI preparation
            # explicit and gives clients a stable progress label.
            add(
                "prepare",
                "Build the exact linux-arm64 runtime container in Controller storage before target transfer.",
                subphase="container-build",
            )
        if needs_runtime_image_prepare:
            add(
                "prepare",
                "Exact Controller OCI runtime image preparation and verification precedes install admission.",
                subphase="runtime-image",
            )
        if needs_prepare:
            add(
                "prepare",
                "Compile and persist the exact schema-2 launch plan before target transfer.",
                subphase="runtime-plan",
            )
        if needs_target_copy:
            add(
                "transfer",
                "Copy missing model artifacts and runtime images after the verified schema-2 payload is persisted.",
                subphase="target-copy",
            )
            add(
                "verify",
                "Verify every transferred artifact against its immutable model set and OCI identity.",
                subphase="target-copy",
            )
        if needs_prepare:
            add(
                "prepare",
                "Start the prepared installation after target copy and verification complete.",
                subphase="runtime-install",
            )
        if needs_cleanup:
            add(
                "cleanup",
                "Reclaim only unreferenced Spark-local artifacts under the selected retention policy.",
            )
        if stops and not stop_added:
            add(
                "stop", "Stop conflicting workloads as one complete group before start."
            )
        if action in {"run", "switch"}:
            add("start", "Start the selected exact model and recipe outcome.")
        add(
            "final_verify",
            "Verify the intended final observed state for every affected rank.",
        )
        return phases

    def _mapping_selection(
        self,
        mapping: ClusterMapping | None,
        nodes: Sequence[ClusterMappingNode],
    ) -> MappingSelection | None:
        if mapping is None:
            return None
        return MappingSelection(
            mapping_id=mapping.id,
            mapping_generation=mapping.generation,
            topology_name=mapping.topology_name,
            parameters=validate_mapping_parameters(mapping.parameters),
            option_choices=mapping_option_choices(mapping.parameters),
            placement_digest=mapping.placement_digest,
            action="reuse",
            nodes=[
                SparkGroupNode(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    endpoint_owner=node.endpoint_owner,
                )
                for node in nodes
            ],
        )

    def _stop_digest(
        self,
        run_id: str,
        *,
        target_node_ids: Sequence[str] | None = None,
    ) -> str | None:
        service = typing_cast("RunSwitchOperationService", self)
        if service._lifecycle is None:
            return None
        try:
            plan = service._lifecycle.preview_stop(
                run_id, profile_target_node_ids=target_node_ids
            )
            return plan.plan_digest if plan.allowed else None
        except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError):
            return None

    @staticmethod
    def _run_reserved_bytes(
        session: Session, run_id: str, *, node_ids: Sequence[str] | None = None
    ) -> int:
        statement = select(
            func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)
        ).where(
            ResourceReservation.owner_kind == "run",
            ResourceReservation.owner_id == run_id,
            ResourceReservation.state == ReservationState.ACTIVE,
        )
        if node_ids is not None:
            statement = statement.where(ResourceReservation.node_id.in_(node_ids))
        return int(session.scalar(statement) or 0)

    @staticmethod
    def _finalize_plan(plan: RunSwitchPlan) -> RunSwitchPlan:
        identity = _plan_identity(plan.model_dump(mode="json"))
        return plan.model_copy(update={"plan_digest": _digest(identity)})
