"""Service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InvalidRequestReason,
)

from ..categorized_errors import (
    InvalidValue,
    MissingRecord,
)
from ..cluster_mappings import (
    ClusterMappingService,
    candidate_placements,
)
from ..model_cache import ModelCacheService
from ..preparation_contract import (
    RuntimeImageIdentity,
)
from ..recipe_operations import (
    RecipeOperationService,
)
from ..recipe_runtime_specs import (
    recipe_topology,
)
from ..resource_planning import (
    PLATFORM_MEMORY_FLOOR_BYTES,
)
from ..run_switch_contract import (
    RunSwitchPlan,
    RunSwitchPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from .acceptance import AcceptanceMixin
from .advance import AdvanceMixin
from .artifact_inspection import DatabaseRunSwitchArtifactInspector
from .build_evidence import BuildEvidenceMixin
from .build_selection import BuildSelectionMixin
from .cancellation_retry import CancellationRetryMixin
from .cleanup_planning import CleanupPlanningMixin
from .constants import _active_recipe_revision
from .endings import EndingsMixin
from .errors import RunSwitchRuntimeSpecInvalid
from .executor import RecipeLifecyclePhaseExecutor
from .identity_helpers import _primary_model_digest
from .interfaces import (
    RunSwitchArtifactInspector,
    RunSwitchArtifactPhaseExecutor,
    RunSwitchPhaseExecutor,
)
from .observation import ObservationMixin
from .phase_dispatch import PhaseDispatchMixin
from .phase_planning import PhasePlanningMixin
from .preparation import PreparationMixin
from .projection import ProjectionMixin
from .reservation import ReservationMixin
from .resolution import ResolutionMixin
from .resource_admission import ResourceAdmissionMixin
from .resource_fit import ResourceFitMixin
from .retry_holds import RetryHoldsMixin
from .run_planning import RunPlanningMixin
from .stop_planning import StopPlanningMixin


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
        inventory_max_age_seconds: int = 300,
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
    ) -> RunSwitchPlan:
        """Read admission without authoring preparation work.

        An existing authorized cache-recovery operation may defer a missing
        source build until its worker runs. Other blockers remain unchanged.
        This flag grants no execution authority and never creates a build.
        """
        return self._preview_run(
            request,
            actor=actor,
            create_build=False,
            defer_source_build=defer_source_build,
            reviewed_runtime_image=expected_runtime_image,
            excluded_profile_application_ids=excluded_profile_application_ids,
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
