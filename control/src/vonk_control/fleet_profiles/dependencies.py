"""Dependencies for Fleet profiles."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import cast as _typing_cast

from pydantic import TypeAdapter
from vonk_agent_protocol import (
    InstallationState,
    LifecycleState,
    ObservedAssignmentState,
    ProfileReasonCode,
    RunState,
    RunSwitchCode,
    RuntimeImageCode,
)
from vonk_agent_protocol.agent_words import (
    ProfileChildPhase,
    ProfileEffectState,
    ProfileReportedPhase,
    ProfileRetryDisposition,
)

from .. import job_states
from ..fleet_profile_contract import (
    FleetProfileAssignmentState,
    FleetProfileChildPhase,
    FleetProfileInstallationPolicy,
    FleetProfileOperationState,
)
from ..lifecycle.types import (
    State as _LifecycleState,
)
from ..profile_error_summary import (
    _error_summary as _error_summary,  # noqa: PLC0414 -- shared helper export
)
from ..storage_demands import (
    STORAGE_EVICTING,
    STORAGE_EVICTION_TIMED_OUT,
    STORAGE_INSUFFICIENT,
)

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")

_ACTIVE_INSTALL_STATES = frozenset(
    {
        InstallationState.PLANNED,
        InstallationState.INSTALLING,
        InstallationState.INSTALLED,
        InstallationState.PARTIAL,
        InstallationState.FAILED,
    }
)

_CHILD_PENDING_STATES = frozenset(
    {
        LifecycleState.QUEUED.value,
        ProfileEffectState.PENDING.value,
        RunState.RUNNING,
        RunState.STARTING,
        RunState.STOPPING,
        InstallationState.INSTALLING,
        *job_states.words(LifecycleState.NEEDS_OPERATOR),
    }
)

_CHILD_FAILED_STATES = frozenset(
    job_states.words(LifecycleState.FAILED, LifecycleState.CANCELLED)
)

_PROFILE_ACTIVITY_ACTIVE_STATES = job_states.words(
    LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
)

_OPERATION_STATE_ADAPTER = TypeAdapter(FleetProfileOperationState)

_OBSERVED_ASSIGNMENT_LABELS: Mapping[FleetProfileAssignmentState, str] = {
    ObservedAssignmentState.NOT_PLACED: "Not placed",
    ObservedAssignmentState.PLACED: "Placed",
    ObservedAssignmentState.INSTALLING: "Installing",
    ObservedAssignmentState.INSTALLED: "Installed",
    ObservedAssignmentState.RUNNING: "Running",
    ObservedAssignmentState.DEGRADED: "Degraded",
}

_PROFILE_PHASE_ADAPTER = TypeAdapter(FleetProfileChildPhase)

_INSTALLATION_POLICY_ADAPTER = TypeAdapter(FleetProfileInstallationPolicy)

_PROFILE_RECOVERY_REFUSED_CODES = frozenset(
    {
        RunSwitchCode.RECEIPT_INVALID,
        RuntimeImageCode.ARCHIVE_UNAVAILABLE,
        RuntimeImageCode.RECEIPT_IDENTITY_CONFLICT,
        RuntimeImageCode.RECEIPT_IDENTITY_INVALID,
        RuntimeImageCode.RECEIPT_INVALID,
    }
)

PROFILE_REPEATED_FAILURE_CODE = ProfileReasonCode.FAILURE_REPEATED

_MAX_PARKED_APPLICATION_OBSERVATIONS = 8

_CANCELLATION_OBSERVATION_SECONDS = 5

_LOGGER = logging.getLogger(__name__)

_PROFILE_PHASE_BY_RUN_PHASE = {
    ProfileChildPhase.TRANSFER.value: ProfileChildPhase.TARGET_COPY.value,
    ProfileChildPhase.VERIFY.value: ProfileChildPhase.FINAL_VERIFY.value,
    ProfileChildPhase.PREPARE.value: ProfileChildPhase.RUNTIME_INSTALL.value,
    ProfileReportedPhase.FINAL_VERIFY.value: ProfileChildPhase.FINAL_VERIFY.value,
}

RETRY_WAIT = ProfileRetryDisposition.WAIT.value

RETRY_SUPERSEDE = ProfileRetryDisposition.SUPERSEDE.value

_PREPARATION_RESOLVABLE_CODES = frozenset(
    {ProfileReasonCode.PREPARATION_UNAVAILABLE, RunSwitchCode.RECIPE_BUILD_UNAVAILABLE}
)

_DISK_REFUSALS = frozenset(
    {RunSwitchCode.INSUFFICIENT_DISK, RunSwitchCode.DISK_EVICTION_PLANNED}
)

_STORAGE_CODES = frozenset(
    {STORAGE_EVICTING, STORAGE_INSUFFICIENT, STORAGE_EVICTION_TIMED_OUT}
)

_CANCELLED_OPERATION: FleetProfileOperationState = _typing_cast(
    "FleetProfileOperationState", _LifecycleState.CANCELLED.value
)
