"""Service for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InstallAdmissionCode,
)

from ..categorized_errors import (
    InvalidValue,
)
from ..cluster_mappings import ClusterMappingPlan, ClusterMappingService
from ..install_admission import (
    InstallAdmissionService,
    InstallPlan,
)
from ..mapping_parameters import MappingParameters
from ..recipe_builds import (
    RecipeBuildService,
)
from ..recipe_routes import (
    RecipeRouteService,
)
from ..recipe_start_payloads import (
    validate_distributed_start_timeout_seconds,
)
from ..run_admission import (
    RunAdmissionService,
)
from ..storage_demands import StorageDemands, spark_scope
from .action_plans import ActionPlansMixin
from .build import BuildMixin
from .build_cancellation import BuildCancellationMixin
from .cancellation import CancellationMixin
from .errors import RecipeRequestInvalid
from .install import InstallMixin
from .installation_preparation import InstallationPreparationMixin
from .interfaces import AgentJobQueue
from .job_activation import JobActivationMixin
from .job_run_completion import JobRunCompletionMixin
from .job_run_stop import JobRunStopMixin
from .offline_stop import OfflineStopMixin
from .persistence import PersistenceMixin
from .profile_job_run_stop import ProfileJobRunStopMixin
from .projection import ProjectionMixin
from .reconciliation_authority import ReconciliationAuthorityMixin
from .result_consumption import ResultConsumptionMixin
from .retirement import RetirementMixin
from .retry import RetryMixin
from .run_observation import RunObservationMixin
from .start import StartMixin
from .stop import StopMixin
from .stop_acceptance import StopAcceptanceMixin
from .stop_dispatch import StopDispatchMixin
from .supersession import SupersessionMixin
from .terminal_projection import TerminalProjectionMixin
from .uninstall import UninstallMixin


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

    def preview_mapping(
        self,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        *,
        parameters: MappingParameters,
        actor: str,
    ) -> ClusterMappingPlan:
        return self._mappings.preview(recipe_revision_id, node_ids, parameters, actor)

    def create_mapping(self, plan: ClusterMappingPlan, *, actor: str) -> str:
        try:
            return self._mappings.materialize(plan, actor=actor, now=self._clock())
        except (RuntimeError, ValueError) as error:
            raise RecipeRequestInvalid(str(error)) from error
