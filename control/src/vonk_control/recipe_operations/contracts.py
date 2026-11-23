"""Typed composition of recipe parent documents before durable publication."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict
from vonk_agent_protocol.wire_model import WireModel

from ..fleet_profiles.stop_authority import (
    JobRunStopScope,
    ProfileJobRunStopAuthorization,
)
from ..jobs.documents import (
    DistributedRecoveryMarker,
    ProfilePartialStop,
    ServiceRunStopReview,
)
from ..library.contract import UuidId
from ..recipe_builds.cancellation import RecipeBuildIntent
from ..run_switch_contract.requests import RunSwitchReconciliationAuthority
from ..strict_json import StrictJSONModel
from .lifecycle_contract import RecipeOperationCancellationResult


class RecipeOperationContext(StrictJSONModel):
    """Optional, typed additions to an operation-specific parent contract.

    This is a composition input; the selected parent model owns the final
    persisted document, including all explicit nulls and declared defaults.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    build_intent: RecipeBuildIntent | None = None
    force_rebuild: Literal[True] | None = None
    build_cancellation: RecipeOperationCancellationResult | None = None
    start_deadline: str | None = None
    recovery: DistributedRecoveryMarker | None = None
    profile_partial_stop: ProfilePartialStop | None = None
    service_stop_review: ServiceRunStopReview | None = None
    job_run_stop_authorization: JobRunStopScope | None = None
    execution_mode: Literal["one-shot-jobs", "profile-jobrun-stop"] | None = None
    profile_application_id: str | None = None
    profile_operation_id: str | None = None
    profile_stop_authorization: ProfileJobRunStopAuthorization | None = None
    reconciliation_authority: RunSwitchReconciliationAuthority | None = None


class RecipeInstallationDisposal(StrEnum):
    """Disposition of an installation that produced no node effects."""

    ABANDONED = "abandoned"


class RecipeInstallationAbandonResult(WireModel):
    """Receipt for disposing of a plan that never reached a Spark."""

    installation_id: UuidId
    disposition: Literal[RecipeInstallationDisposal.ABANDONED] = (
        RecipeInstallationDisposal.ABANDONED
    )
