"""Acceptance."""

from __future__ import annotations

import uuid
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from vonk_agent_protocol import (
    RunSwitchCode,
    canonical_message,
)

from ..categorized_errors import (
    MissingRecord,
)
from ..job_documents import (
    RunSwitchCleanupIntent,
    RunSwitchProfileStopIntent,
    RunSwitchRunIntent,
    RunSwitchStopIntent,
)
from ..models import (
    Job,
)
from ..run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchCleanupApplyRequest,
    RunSwitchOperation,
    RunSwitchProfileStopScope,
    RunSwitchStopApplyRequest,
)
from .constants import _OPERATION_KINDS
from .errors import RunSwitchRequestInvalid
from .observation_helpers import _plan_blockers_are_waitable, _require_reviewed_plan

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class AcceptanceMixin:
    def apply_cleanup(
        self,
        request: RunSwitchCleanupApplyRequest,
        *,
        actor: str,
        workload_intent_ordinal: int | None = None,
    ) -> RunSwitchOperation:
        service = typing_cast("RunSwitchOperationService", self)
        request_key = request.request_key or str(uuid.uuid4())
        intent = RunSwitchCleanupIntent.model_validate_json(
            canonical_message(
                {
                    "type": "cleanup",
                    **request.model_dump(
                        mode="json", exclude={"request_key"}, exclude_none=True
                    ),
                }
            )
        )
        if request.request_key is not None:
            existing = service._existing_request_operation(
                request.request_key,
                kind="recipe.cleanup.v2",
                intent=intent,
            )
            if existing is not None:
                return existing
        preview = service.preview_cleanup(request, actor=actor)
        _require_reviewed_plan(request.plan_digest, preview)
        if not preview.allowed and not _plan_blockers_are_waitable(preview):
            raise RunSwitchRequestInvalid(
                f"{RunSwitchCode.PLAN_BLOCKED}: "
                + "; ".join(reason.code for reason in preview.blockers[:8])
            )
        return service._apply_plan(
            preview,
            request_key=request_key,
            actor=actor,
            kind="recipe.cleanup.v2",
            workload_intent_ordinal=workload_intent_ordinal,
            intent=intent,
        )

    def apply(
        self,
        request: RunSwitchApplyRequest,
        *,
        actor: str,
        workload_intent_ordinal: int | None = None,
        profile_application_id: str | None = None,
    ) -> RunSwitchOperation:
        service = typing_cast("RunSwitchOperationService", self)
        request_key = request.request_key or str(uuid.uuid4())
        intent = RunSwitchRunIntent(
            type="run", request=request.model_copy(update={"request_key": None})
        )
        if request.request_key is not None:
            existing = service._existing_request_operation(
                request.request_key,
                kind="recipe.run-switch.v2",
                intent=intent,
            )
            if existing is not None:
                return existing
        plan = service.preview(
            request, actor=actor, profile_application_id=profile_application_id
        )
        _require_reviewed_plan(request.plan_digest, plan)
        if not plan.allowed and not _plan_blockers_are_waitable(plan):
            raise RunSwitchRequestInvalid(
                f"{RunSwitchCode.PLAN_BLOCKED}: "
                + "; ".join(reason.code for reason in plan.blockers[:8])
            )
        return service._apply_plan(
            plan,
            request_key=request_key,
            actor=actor,
            kind="recipe.run-switch.v2",
            workload_intent_ordinal=workload_intent_ordinal,
            profile_application_id=profile_application_id,
            intent=intent,
        )

    def apply_run(
        self,
        request: RunSwitchApplyRequest,
        *,
        actor: str,
    ) -> RunSwitchOperation:
        service = typing_cast("RunSwitchOperationService", self)
        return service.apply(request, actor=actor)

    def apply_stop(
        self,
        request: RunSwitchStopApplyRequest,
        *,
        actor: str,
        workload_intent_ordinal: int | None = None,
        profile_application_id: str | None = None,
    ) -> RunSwitchOperation:
        service = typing_cast("RunSwitchOperationService", self)
        request_key = request.request_key or str(uuid.uuid4())
        intent = RunSwitchStopIntent.model_validate_json(
            canonical_message(
                {
                    "type": "stop",
                    **request.model_dump(
                        mode="json", exclude={"request_key"}, exclude_none=True
                    ),
                }
            )
        )
        if request.request_key is not None:
            existing = service._existing_request_operation(
                request.request_key,
                kind="recipe.stop.v2",
                intent=intent,
            )
            if existing is not None:
                return existing
        preview = service.preview_stop(request, actor=actor)
        _require_reviewed_plan(request.plan_digest, preview)
        if not preview.allowed and not _plan_blockers_are_waitable(preview):
            raise RunSwitchRequestInvalid(
                f"{RunSwitchCode.PLAN_BLOCKED}: "
                + "; ".join(reason.code for reason in preview.blockers[:8])
            )
        return service._apply_plan(
            preview,
            request_key=request_key,
            actor=actor,
            kind="recipe.stop.v2",
            workload_intent_ordinal=workload_intent_ordinal,
            profile_application_id=profile_application_id,
            intent=intent,
        )

    def apply_profile_stop(
        self,
        run_id: str,
        profile_stop_scope: RunSwitchProfileStopScope,
        *,
        plan_digest: str,
        request_key: str,
        actor: str,
        workload_intent_ordinal: int,
        profile_application_id: str,
    ) -> RunSwitchOperation:
        """Apply only a FleetProfile-reviewed reachable-rank Stop scope."""
        service = typing_cast("RunSwitchOperationService", self)

        intent = RunSwitchProfileStopIntent(
            type="profile-stop", run_id=run_id, profile_stop_scope=profile_stop_scope
        )
        existing = service._existing_request_operation(
            request_key,
            kind="recipe.stop.v2",
            intent=intent,
        )
        if existing is not None:
            return existing
        plan = service.preview_stop(
            run_id,
            actor=actor,
            profile_stop_scope=profile_stop_scope,
        )
        if not plan.allowed and not _plan_blockers_are_waitable(plan):
            raise RunSwitchRequestInvalid(
                f"{RunSwitchCode.PLAN_BLOCKED}: "
                + "; ".join(reason.code for reason in plan.blockers[:8])
            )
        return service._apply_plan(
            plan,
            request_key=request_key,
            actor=actor,
            kind="recipe.stop.v2",
            workload_intent_ordinal=workload_intent_ordinal,
            profile_application_id=profile_application_id,
            intent=intent,
        )

    def get(self, operation_id: str) -> RunSwitchOperation:
        service = typing_cast("RunSwitchOperationService", self)
        with service._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind not in _OPERATION_KINDS:
                raise MissingRecord(operation_id)
            return service._operation_view(job)
