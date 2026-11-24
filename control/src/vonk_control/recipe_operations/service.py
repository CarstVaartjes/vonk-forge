"""Service for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from vonk_agent_protocol.reason_codes.admission import InstallAdmissionCode
from vonk_control.categorized_errors import InvalidValue
from vonk_control.cluster_mappings import ClusterMappingPlan, ClusterMappingService
from vonk_control.install_admission.contracts import InstallPlan
from vonk_control.install_admission.service import InstallAdmissionService
from vonk_control.mapping_parameters import MappingParameters
from vonk_control.recipe_builds.service import RecipeBuildService
from vonk_control.recipe_operations.action_plans import ActionPlansMixin
from vonk_control.recipe_operations.build import BuildMixin
from vonk_control.recipe_operations.build_cancellation import BuildCancellationMixin
from vonk_control.recipe_operations.cancellation import CancellationMixin
from vonk_control.recipe_operations.install import InstallMixin
from vonk_control.recipe_operations.installation_preparation import (
    InstallationPreparationMixin,
)
from vonk_control.recipe_operations.interfaces import AgentJobQueue
from vonk_control.recipe_operations.job_activation import JobActivationMixin
from vonk_control.recipe_operations.job_run_completion import JobRunCompletionMixin
from vonk_control.recipe_operations.job_run_stop import JobRunStopMixin
from vonk_control.recipe_operations.offline_stop import OfflineStopMixin
from vonk_control.recipe_operations.persistence import PersistenceMixin
from vonk_control.recipe_operations.profile_job_run_stop import ProfileJobRunStopMixin
from vonk_control.recipe_operations.projection import ProjectionMixin
from vonk_control.recipe_operations.reconciliation_authority import (
    ReconciliationAuthorityMixin,
)
from vonk_control.recipe_operations.result_consumption import ResultConsumptionMixin
from vonk_control.recipe_operations.retirement import RetirementMixin
from vonk_control.recipe_operations.retry import RetryMixin
from vonk_control.recipe_operations.run_observation import RunObservationMixin
from vonk_control.recipe_operations.start import StartMixin
from vonk_control.recipe_operations.start_cleanup import StartCleanupDispatch
from vonk_control.recipe_operations.start_payloads import (
    validate_distributed_start_timeout_seconds,
)
from vonk_control.recipe_operations.stop import StopMixin
from vonk_control.recipe_operations.stop_acceptance import StopAcceptanceMixin
from vonk_control.recipe_operations.stop_dispatch import StopDispatchMixin
from vonk_control.recipe_operations.supersession import SupersessionMixin
from vonk_control.recipe_operations.terminal_projection import TerminalProjectionMixin
from vonk_control.recipe_operations.uninstall import UninstallMixin
from vonk_control.recipe_routes.service import RecipeRouteService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.storage_demands import StorageDemands, spark_scope


class RecipeOperationService(
    TerminalProjectionMixin,
    BuildMixin,
    InstallationPreparationMixin,
    RunObservationMixin,
    InstallMixin,
    StartMixin,
    JobActivationMixin,
    SupersessionMixin,
    StopMixin,
    StopAcceptanceMixin,
    OfflineStopMixin,
    StopDispatchMixin,
    ReconciliationAuthorityMixin,
    UninstallMixin,
    RetryMixin,
    ResultConsumptionMixin,
    ProjectionMixin,
    CancellationMixin,
    RetirementMixin,
    BuildCancellationMixin,
    JobRunStopMixin,
    JobRunCompletionMixin,
    ProfileJobRunStopMixin,
    ActionPlansMixin,
    PersistenceMixin,
):
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        install_admission: InstallAdmissionService,
        run_admission: RunAdmissionService,
        agent_jobs: AgentJobQueue,
        clock: Callable[[], datetime],
        route_withdrawer: Callable[[str], None] | None = None,
        route_publications: RecipeRouteService | None = None,
        builds: RecipeBuildService | None = None,
        mappings: ClusterMappingService | None = None,
        run_health_maximum_age_seconds: int = 300,
        distributed_start_timeout_seconds: int = 3600,
    ) -> None:
        if not 1 <= run_health_maximum_age_seconds <= 300:
            raise InvalidValue("recipe run health age is invalid")
        self._distributed_start_timeout_seconds = (
            validate_distributed_start_timeout_seconds(
                distributed_start_timeout_seconds
            )
        )
        self._sessions = sessions
        self._install_admission = install_admission
        self._run_admission = run_admission
        self._agent_jobs = agent_jobs
        self._clock = clock
        self._cancelled_runtime_cursor: str | None = None
        self._route_withdrawer = route_withdrawer or (lambda _run_id: None)
        self._route_publications = route_publications
        self._builds = builds
        # The mapping service holds nothing but the sessions, so a process that
        # was not handed one builds its own instead of refusing mapping requests.
        self._mappings = (
            mappings if mappings is not None else ClusterMappingService(sessions)
        )
        self._build_cleanup_cursor: str | None = None
        self._run_health_maximum_age = timedelta(seconds=run_health_maximum_age_seconds)
        StartCleanupDispatch.install(self)

    _storage_demands: StorageDemands | None = None

    def bind_storage_demands(self, demands: StorageDemands) -> None:
        """Attach the register an install refused for lack of disk asks space in."""

        self._storage_demands = demands

    def _request_install_storage(self, plan: InstallPlan) -> None:
        """Ask for the free disk each Spark that refused this install lacks."""

        if self._storage_demands is None:
            return
        for node in plan.nodes:
            if (
                node.free_bytes is None
                or node.free_after_bytes is None
                or not any(
                    reason.code == InstallAdmissionCode.INSUFFICIENT_DISK
                    for reason in node.blockers
                )
            ):
                continue
            self._storage_demands.request(
                spark_scope(node.node_id),
                node.free_bytes - node.free_after_bytes + node.disk_floor_bytes,
                source="install",
                subject=plan.recipe_revision_id,
                reason=InstallAdmissionCode.INSUFFICIENT_DISK,
            )

    _install_preparation: Callable[[InstallPlan, str, str], None] | None = None

    def bind_install_preparation(
        self, prepare: Callable[[InstallPlan, str, str], None]
    ) -> None:
        """Attach the existing durable image owner to installation requests."""
        self._install_preparation = prepare

    def preview_mapping(
        self,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        *,
        parameters: MappingParameters,
        actor: str,
    ) -> ClusterMappingPlan:
        return self._mappings.preview(recipe_revision_id, node_ids, parameters, actor)
