"""Service for Fleet profiles."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts.memory_admission import PLATFORM_MEMORY_FLOOR_BYTES

from vonk_control.fleet_profile_contract.adapter import FleetProfileSwitchAdapter
from vonk_control.fleet_profiles.application_projection import (
    FleetProfileService as _ApplicationProjection,
)
from vonk_control.fleet_profiles.apply import FleetProfileService as _Apply
from vonk_control.fleet_profiles.assessment import FleetProfileService as _Assessment
from vonk_control.fleet_profiles.assignment_persistence import (
    FleetProfileService as _AssignmentPersistence,
)
from vonk_control.fleet_profiles.cancellation import (
    FleetProfileService as _Cancellation,
)
from vonk_control.fleet_profiles.cancellation_observation import (
    FleetProfileService as _CancellationObservation,
)
from vonk_control.fleet_profiles.contracts import (
    PreparationCanceller,
    PreparationProgress,
    PreparationStarter,
    StorageReliefProvider,
    _AssessmentProvider,
)
from vonk_control.fleet_profiles.deletion import FleetProfileService as _Deletion
from vonk_control.fleet_profiles.endpoint_projection import (
    FleetProfileService as _EndpointProjection,
)
from vonk_control.fleet_profiles.operation_projection import (
    FleetProfileService as _OperationProjection,
)
from vonk_control.fleet_profiles.pending_admission import (
    FleetProfileService as _PendingAdmission,
)
from vonk_control.fleet_profiles.pending_application import (
    FleetProfileService as _PendingApplication,
)
from vonk_control.fleet_profiles.preparation import FleetProfileService as _Preparation
from vonk_control.fleet_profiles.profile_projection import (
    FleetProfileService as _ProfileProjection,
)
from vonk_control.fleet_profiles.queue_application import (
    FleetProfileService as _QueueApplication,
)
from vonk_control.fleet_profiles.recipe_choices import (
    FleetProfileService as _RecipeChoices,
)
from vonk_control.fleet_profiles.reconcile import FleetProfileService as _Reconcile
from vonk_control.fleet_profiles.recovery import FleetProfileService as _Recovery
from vonk_control.fleet_profiles.replan import FleetProfileService as _Replan
from vonk_control.fleet_profiles.retry import FleetProfileService as _Retry
from vonk_control.fleet_profiles.saved_profiles import (
    FleetProfileService as _SavedProfiles,
)
from vonk_control.fleet_profiles.selection import FleetProfileService as _Selection
from vonk_control.inventory.repository import INVENTORY_MAX_AGE_SECONDS
from vonk_control.lifecycle.fleet_profile import FleetProfileAdapter
from vonk_control.model_cache.contract import CacheResolution


class FleetProfileService(
    _Selection,
    _RecipeChoices,
    _SavedProfiles,
    _Deletion,
    _EndpointProjection,
    _Assessment,
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
        memory_floor_bytes: int = PLATFORM_MEMORY_FLOOR_BYTES,
        inventory_max_age_seconds: int = INVENTORY_MAX_AGE_SECONDS,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._switch_adapter = switch_adapter
        self._cache_resolver = cache_resolver
        self._assessment_provider = assessment_provider
        # What a restored claim is admitted against (the Run/Switch settings).
        self._memory_floor = memory_floor_bytes
        self._inventory_max_age = inventory_max_age_seconds
        self._preparation_starter: PreparationStarter | None = None
        self._storage_relief: StorageReliefProvider | None = None
        self._preparation_canceller: PreparationCanceller | None = None
        self._preparation_progress: PreparationProgress | None = None
        # The lifecycle adapter is the only writer of an application's state; its
        # hooks are this service's own claim release, child stop and observation,
        # and its retry decision when a load fails.
        self._lifecycle = FleetProfileAdapter(
            sessions=sessions,
            clock=clock,
            stopper=self._stop_children,
            observer=self._observe_children,
            after_state=self._release_claims,
            finish=self._finish_cancel_records,
            retrying=self._retries_failure,
        )
        # Round-robin position of the bounded automatic-recovery scan, so rows
        # that stay ineligible cannot starve later due rows.
        self._recovery_cursor: str | None = None
        self._ordinary_cursor: str | None = None
