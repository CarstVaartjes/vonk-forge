"""Follow the accepted profile and exact child's persisted observation clocks."""

from datetime import datetime

from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.run_switch_operations import RunSwitchOperationService


def next_profile_due(
    profiles: FleetProfileService,
    planner: RunSwitchOperationService,
    application_id: str,
) -> datetime | None:
    observed = profiles.application(application_id)
    now = profiles._clock()
    due = [observed.progress.retry_due_at]
    adapter = observed.progress.switch_adapter
    if adapter is not None:
        due.extend(
            planner.get(child.operation_id).next_attempt_at
            for child in adapter.pending_children
        )
    future = [instant for instant in due if instant is not None and instant > now]
    if not future:
        return None
    if (
        observed.progress.retry_due_at is not None
        and observed.progress.retry_due_at > now
    ):
        profiles.tick()
        held = profiles.application(application_id)
        assert held.current_operation_id == observed.current_operation_id
        assert held.progress.step_results == observed.progress.step_results
        if adapter is not None:
            assert held.progress.switch_adapter is not None
            assert (
                held.progress.switch_adapter.pending_children
                == adapter.pending_children
            )
    return min(future)
