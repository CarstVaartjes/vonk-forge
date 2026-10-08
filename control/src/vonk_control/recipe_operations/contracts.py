"""Typed composition of recipe parent documents before durable publication."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict
from vonk_agent_protocol import WireModel

from ..job_documents import (
    DistributedRecoveryMarker,
    ProfilePartialStop,
    ServiceRunStopReview,
)
from ..library_contract import UuidId
from ..profile_stop_authority import JobRunStopScope, ProfileJobRunStopAuthorization
from ..recipe_build_cancellation import RecipeBuildIntent
from ..recipe_lifecycle_contract import RecipeOperationCancellationResult
from ..run_switch_contract import RunSwitchReconciliationAuthority
from ..strict_json import StrictJSONModel


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
