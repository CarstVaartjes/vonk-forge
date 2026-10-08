"""Service for Fleet profiles."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from ..fleet_profile_contract import FleetProfileSwitchAdapter
from ..lifecycle.fleet_profile import FleetProfileAdapter
from ..model_cache_contract import CacheResolution
from .application_projection import FleetProfileService as _ApplicationProjection
from .apply import FleetProfileService as _Apply
from .assessment import FleetProfileService as _Assessment
from .assignment_assessment import FleetProfileService as _AssignmentAssessment
from .assignment_persistence import FleetProfileService as _AssignmentPersistence
from .cancellation import FleetProfileService as _Cancellation
from .cancellation_observation import FleetProfileService as _CancellationObservation
from .continuing_effects import FleetProfileService as _ContinuingEffects
from .contracts import (
    PreparationCanceller,
    PreparationStarter,
    StorageReliefProvider,
    _AssessmentProvider,
)
from .control_effects import FleetProfileService as _ControlEffects
from .endpoint_projection import FleetProfileService as _EndpointProjection
from .operation_projection import FleetProfileService as _OperationProjection
from .pending_admission import FleetProfileService as _PendingAdmission
from .pending_application import FleetProfileService as _PendingApplication
from .preparation import FleetProfileService as _Preparation
from .profile_projection import FleetProfileService as _ProfileProjection
from .queue_application import FleetProfileService as _QueueApplication
from .recipe_choices import FleetProfileService as _RecipeChoices
from .reconcile import FleetProfileService as _Reconcile
from .recovery import FleetProfileService as _Recovery
from .replan import FleetProfileService as _Replan
from .retry import FleetProfileService as _Retry
from .saved_profiles import FleetProfileService as _SavedProfiles
from .selection import FleetProfileService as _Selection


class FleetProfileService(
    _Selection,
    _RecipeChoices,
    _SavedProfiles,
    _EndpointProjection,
    _Assessment,
    _ControlEffects,
    _ContinuingEffects,
    _PendingApplication,
    _Apply,
    _QueueApplication,
    _Preparation,
    _Retry,
    _OperationProjection,
    _Cancellation,
    _Reconcile,
    _Replan,
    _PendingAdmission,
    _CancellationObservation,
    _Recovery,
    _AssignmentPersistence,
    _ProfileProjection,
    _AssignmentAssessment,
    _ApplicationProjection,
):
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        switch_adapter: FleetProfileSwitchAdapter | None = None,
        cache_resolver: Callable[..., CacheResolution] | None = None,
        assessment_provider: _AssessmentProvider | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._switch_adapter = switch_adapter
        self._cache_resolver = cache_resolver
        self._assessment_provider = assessment_provider
        self._preparation_starter: PreparationStarter | None = None
        self._storage_relief: StorageReliefProvider | None = None
        self._preparation_canceller: PreparationCanceller | None = None
        # The lifecycle adapter is the only writer of an application's state; its
        # hooks are this service's own claim release, child stop and observation.
        self._lifecycle = FleetProfileAdapter(
            sessions=sessions,
            clock=clock,
            stopper=self._stop_children,
            observer=self._observe_children,
            after_state=self._release_claims,
            finish=self._finish_cancel_records,
        )
        # Round-robin position of the bounded automatic-recovery scan, so rows
        # that stay ineligible cannot starve later due rows.
        self._recovery_cursor: str | None = None
        self._ordinary_cursor: str | None = None
