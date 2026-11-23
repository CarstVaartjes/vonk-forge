"""Service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts.memory_admission import PLATFORM_MEMORY_FLOOR_BYTES

from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason
from vonk_control.categorized_errors import InvalidValue, MissingRecord
from vonk_control.cluster_mappings import ClusterMappingService, candidate_placements
from vonk_control.inventory.repository import INVENTORY_MAX_AGE_SECONDS
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.preparation_contract import RuntimeImageIdentity
from vonk_control.recipe_operations.runtime_specs import recipe_topology
from vonk_control.recipe_operations.service import RecipeOperationService
from vonk_control.run_switch_contract.plans import RunSwitchPlan
from vonk_control.run_switch_contract.requests import (
    RunSwitchPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations.acceptance import AcceptanceMixin
from vonk_control.run_switch_operations.advance import AdvanceMixin
from vonk_control.run_switch_operations.artifact_inspection import (
    DatabaseRunSwitchArtifactInspector,
)
from vonk_control.run_switch_operations.build_evidence import BuildEvidenceMixin
from vonk_control.run_switch_operations.build_selection import BuildSelectionMixin
from vonk_control.run_switch_operations.cancellation_retry import CancellationRetryMixin
from vonk_control.run_switch_operations.cleanup_planning import CleanupPlanningMixin
from vonk_control.run_switch_operations.constants import _active_recipe_revision
from vonk_control.run_switch_operations.endings import EndingsMixin
from vonk_control.run_switch_operations.errors import RunSwitchRuntimeSpecInvalid
from vonk_control.run_switch_operations.executor import RecipeLifecyclePhaseExecutor
from vonk_control.run_switch_operations.identity_helpers import _primary_model_digest
from vonk_control.run_switch_operations.interfaces import (
    RunSwitchArtifactInspector,
    RunSwitchArtifactPhaseExecutor,
    RunSwitchPhaseExecutor,
)
from vonk_control.run_switch_operations.observation import ObservationMixin
from vonk_control.run_switch_operations.phase_dispatch import PhaseDispatchMixin
from vonk_control.run_switch_operations.phase_planning import PhasePlanningMixin
from vonk_control.run_switch_operations.preparation import PreparationMixin
from vonk_control.run_switch_operations.projection import ProjectionMixin
from vonk_control.run_switch_operations.reservation import ReservationMixin
from vonk_control.run_switch_operations.resolution import ResolutionMixin
from vonk_control.run_switch_operations.resource_admission import ResourceAdmissionMixin
from vonk_control.run_switch_operations.resource_fit import ResourceFitMixin
from vonk_control.run_switch_operations.retry_holds import RetryHoldsMixin
from vonk_control.run_switch_operations.run_planning import RunPlanningMixin
from vonk_control.run_switch_operations.stop_planning import StopPlanningMixin


class RunSwitchOperationService(
    StopPlanningMixin,
    CleanupPlanningMixin,
    AcceptanceMixin,
    CancellationRetryMixin,
    ObservationMixin,
    RunPlanningMixin,
    ResolutionMixin,
    BuildSelectionMixin,
    BuildEvidenceMixin,
    PreparationMixin,
    ResourceAdmissionMixin,
    ResourceFitMixin,
    PhasePlanningMixin,
    ReservationMixin,
    AdvanceMixin,
    RetryHoldsMixin,
    EndingsMixin,
    ProjectionMixin,
    PhaseDispatchMixin,
):
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        lifecycle: RecipeOperationService | None = None,
        clock: Callable[[], datetime],
        mappings: ClusterMappingService | None = None,
        artifacts: RunSwitchArtifactInspector | None = None,
        artifact_phase_executor: RunSwitchArtifactPhaseExecutor | None = None,
        phase_executor: RunSwitchPhaseExecutor | None = None,
        model_cache: ModelCacheService | None = None,
        build_archive_available: Callable[[str, int], bool] | None = None,
        inventory_max_age_seconds: int = INVENTORY_MAX_AGE_SECONDS,
        memory_floor_bytes: int = PLATFORM_MEMORY_FLOOR_BYTES,
    ) -> None:
        if not 1 <= inventory_max_age_seconds <= 86_400:
            raise InvalidValue("run/switch inventory age is invalid")
        if memory_floor_bytes < 0:
            raise InvalidValue("run/switch memory floor is invalid")
        self._sessions = sessions
        self._lifecycle = lifecycle
        self._clock = clock
        self._mappings = mappings or ClusterMappingService(sessions)
        self._artifacts = artifacts or DatabaseRunSwitchArtifactInspector(model_cache)
        self._artifact_phase_executor = artifact_phase_executor
        self._build_archive_available = build_archive_available
        self._custom_phase_executor = phase_executor is not None
        self._phase_executor = phase_executor or (
            RecipeLifecyclePhaseExecutor(
                lifecycle,
                sessions,
                self._mappings,
                clock,
                artifact_executor=artifact_phase_executor,
                inventory_max_age_seconds=inventory_max_age_seconds,
            )
            if lifecycle is not None
            else None
        )
        self._inventory_max_age = inventory_max_age_seconds
        self._memory_floor = memory_floor_bytes
        self._tick_cursor: str | None = None

    @property
    def inventory_max_age_seconds(self) -> int:
        return self._inventory_max_age

    @property
    def memory_floor_bytes(self) -> int:
        return self._memory_floor

    def preview(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
        profile_application_id: str | None = None,
    ) -> RunSwitchPlan:
        return self._preview_run(
            request,
            actor=actor,
            profile_application_id=profile_application_id,
            excluded_profile_application_ids=(profile_application_id,)
            if profile_application_id is not None
            else (),
        )

    def preview_run(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
    ) -> RunSwitchPlan:
        return self._preview_run(request, actor=actor)

    def inspect_request(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
        defer_source_build: bool = False,
        expected_runtime_image: RuntimeImageIdentity | None = None,
        excluded_profile_application_ids: tuple[str, ...] = (),
        planned_ports: frozenset[tuple[str, int]] = frozenset(),
        kept_run_ids: frozenset[str] = frozenset(),
        replaced_run_ids: frozenset[str] = frozenset(),
        own_run_id: str | None = None,
    ) -> RunSwitchPlan:
        """Read admission without authoring preparation work.

        An existing authorized cache-recovery operation may defer a missing
        source build until its worker runs. Other blockers remain unchanged.
        This flag grants no execution authority and never creates a build.
        ``planned_ports`` are ``(node, port)`` the same profile review already
        gives other assignments; ``kept_run_ids`` are the runs it keeps for them
        and ``replaced_run_ids`` the runs it stops as a whole.
        """
        return self._preview_run(
            request,
            actor=actor,
            create_build=False,
            defer_source_build=defer_source_build,
            reviewed_runtime_image=expected_runtime_image,
            excluded_profile_application_ids=excluded_profile_application_ids,
            planned_ports=planned_ports,
            kept_run_ids=kept_run_ids,
            replaced_run_ids=replaced_run_ids,
            own_run_id=own_run_id,
        )

    def inspect_candidate(
        self, recipe_revision_id: str, node_ids: tuple[str, ...], *, actor: str
    ) -> RunSwitchPlan:
        """Inspect one library placement without creating a planned build."""
        with self._sessions() as session:
            revision = _active_recipe_revision(session, recipe_revision_id)
            if revision is None:
                raise MissingRecord(recipe_revision_id)
            placements = candidate_placements(
                recipe_topology(revision.document), node_ids
            )
            model_digest = _primary_model_digest(revision.document)
            if model_digest is None:
                raise RunSwitchRuntimeSpecInvalid(
                    "recipe has no exact primary model",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            request = RunSwitchPreviewRequest(
                recipe_revision_id=recipe_revision_id,
                model_content_sha256=model_digest,
                spark_group=SparkGroup(
                    nodes=[
                        SparkGroupNode(
                            node_id=node.node_id,
                            rank=node.rank,
                            role=node.role,
                            endpoint_owner=node.endpoint_owner,
                        )
                        for node in placements
                    ]
                ),
                alias=revision.slug,
            )
        return self.inspect_request(request, actor=actor)
