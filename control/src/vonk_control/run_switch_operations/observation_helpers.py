"""Observation helpers."""

from __future__ import annotations

import uuid
from dataclasses import replace

import httpx2
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RunState,
    RunSwitchCode,
)

from .. import job_states
from ..failure_classification import error_code, is_security_failure
from ..job_documents import (
    RunSwitchIntent,
)
from ..models import (
    ACTIVE_RUN_STATES,
    Job,
)
from ..recipe_operations import (
    RecipeOperationService,
    RecipeOperationView,
)
from ..run_switch_contract import (
    RunSwitchPhase,
    RunSwitchPlan,
)
from .errors import RunSwitchRequestInvalid
from .planning_helpers import _run_switch_payload


def _EstablishedEffect(child: RecipeOperationView, run_id: str) -> RecipeOperationView:
    """Present the same exact child's established effect without duck delegation."""
    return replace(child, state=LifecycleState.SUCCEEDED.value, owner_id=run_id)


def _established_start_effect(
    lifecycle: RecipeOperationService | None,
    phase: RunSwitchPhase,
    child: object,
) -> str | None:
    if (
        lifecycle is None
        or phase.kind != "start"
        or not isinstance(child, RecipeOperationView)
        or child.state not in job_states.words(LifecycleState.NEEDS_OPERATOR)
    ):
        return None
    try:
        status = lifecycle.run_status(child.owner_id)
    except (KeyError, RuntimeError, TypeError, ValueError):
        return None
    return child.owner_id if status.healthy else None


def _start_still_progressing(
    lifecycle: RecipeOperationService | None,
    phase: RunSwitchPhase,
    child: object,
) -> bool:
    if (
        lifecycle is None
        or phase.kind != "start"
        or not isinstance(child, RecipeOperationView)
        or child.state not in job_states.words(LifecycleState.NEEDS_OPERATOR)
    ):
        return False
    try:
        status = lifecycle.run_status(child.owner_id)
    except (KeyError, RuntimeError, TypeError, ValueError):
        return False
    return status.state in ACTIVE_RUN_STATES and any(
        rank.fresh
        and rank.state in {RunState.PLANNED, RunState.STARTING, RunState.RUNNING}
        for rank in status.ranks
    )


def _failure_code_of(error: BaseException) -> str | None:
    """The code ``fail`` classifies: an authentication refusal from a dependency
    (HTTP 401/403) is a real security boundary and the only terminal case; every
    other error is an unknown that is observed again."""

    if isinstance(error, httpx2.HTTPError):
        status = (
            error.response.status_code
            if isinstance(error, httpx2.HTTPStatusError)
            else None
        )
        if status in {401, 403}:
            return str(status)
    return error_code(error)


def _same_intent(existing: Job, intent: RunSwitchIntent | None) -> bool:
    """A reused request key names the same canonical accepted intent."""
    payload = _run_switch_payload(existing)
    if payload is None:
        return False
    stored = payload.intent
    if stored is None or intent is None:
        return True
    return stored.model_dump(
        mode="json", exclude_none=True, exclude={"request_key"}
    ) == intent.model_dump(mode="json", exclude_none=True, exclude={"request_key"})


def _require_reviewed_plan(reviewed_digest: str | None, plan: RunSwitchPlan) -> None:
    if reviewed_digest is not None and reviewed_digest != plan.plan_digest:
        raise RunSwitchRequestInvalid(
            f"{RunSwitchCode.STALE_PLAN}: effects changed; review the current plan",
            reason=InvalidRequestReason.SUPERSEDED,
        )


def _plan_blockers_are_waitable(plan: RunSwitchPlan) -> bool:
    return bool(plan.blockers) and not any(
        is_security_failure(reason.code) for reason in plan.blockers
    )


def _stop_child_request_key(
    request_key: str, run_id: str, profile_application_id: str | None
) -> str:
    """The one request identity for this exact run Stop and accepted profile."""
    child_key = str(uuid.uuid5(uuid.UUID(request_key), f"stop:{run_id}"))
    if profile_application_id is not None:
        child_key = str(
            uuid.uuid5(uuid.UUID(child_key), f"profile-stop:{profile_application_id}")
        )
    return child_key


def _phase_request_key(
    operation_id: str, phase_index: int, item_index: int, attempt: int
) -> str:
    """Keep one idempotency key per exact phase attempt."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:run-switch-phase:{operation_id}:{phase_index}:{item_index}:{attempt}",
        )
    )
