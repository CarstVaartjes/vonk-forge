"""Reservation."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RunSwitchCode,
    UnknownOutcomeError,
)

from .. import job_states
from ..agent_jobs import AgentJobService
from ..bounded_json import require_integer
from ..failure_classification import error_code, is_security_failure
from ..job_documents import (
    RunSwitchCleanupIntent,
    RunSwitchIntent,
    RunSwitchProfileStopIntent,
    RunSwitchRunIntent,
    RunSwitchStopIntent,
)
from ..lifecycle.types import (
    State as _LifecycleState,
)
from ..models import (
    AgentNode,
    Job,
)
from ..recipe_build_cancellation import (
    BuildConsumerError,
    lock_run_switch_build_dependency,
)
from ..run_switch_contract import (
    RunSwitchOperation,
    RunSwitchPlan,
)
from ..stored_json import read_row_column
from ..strict_json import (
    serialize_json_value,
)
from .constants import _OBSERVING, _OPERATION_KINDS
from .errors import RunSwitchRequestInvalid
from .identity_helpers import _string_or_none
from .image_receipts import _plan_target_node_ids, _planned_transfer_bytes
from .observation_helpers import _same_intent
from .plan_persistence import _reserve_run_switch_assets
from .planning_helpers import (
    _aware,
    _digest,
    _now,
    _run_switch_payload,
    _stored_job_plan,
)
from .provider import _ADAPTER
from .result_helpers import _persisted_result, _progress_damaged, _read_progress

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class ReservationMixin:
    def _apply_plan(
        self,
        plan: RunSwitchPlan,
        *,
        request_key: str,
        actor: str,
        kind: str,
        workload_intent_ordinal: int | None,
        profile_application_id: str | None = None,
        intent: RunSwitchIntent | None = None,
    ) -> RunSwitchOperation:
        service = typing_cast("RunSwitchOperationService", self)
        now = _now(service._clock)
        target_node_ids = _plan_target_node_ids(plan)
        total_bytes, member_totals = _planned_transfer_bytes(plan)
        payload = {
            "schema_version": 2,
            "operation_kind": kind,
            "action": plan.action,
            "plan_digest": plan.plan_digest,
            "plan": plan.model_dump(mode="json"),
            **({"intent": serialize_json_value(intent)} if intent is not None else {}),
            "progress": {
                **(
                    {"profile_application_id": profile_application_id}
                    if profile_application_id is not None
                    else {}
                ),
                "phase_index": 0,
                "item_index": 0,
                "phase": (plan.phases[0].kind if plan.phases else "final_verify"),
                "subphase": (plan.phases[0].subphase if plan.phases else None),
                "completed_phases": [],
                "child_operation_id": None,
                "phase_results": [],
                "completed_bytes": 0,
                "total_bytes": total_bytes,
                "total_bytes_known": total_bytes is not None,
                "members": [
                    {
                        "node_id": node.node_id,
                        "phase": plan.phases[0].kind if plan.phases else None,
                        "state": "pending",
                        "completed_bytes": 0,
                        "total_bytes": member_totals.get(node.node_id),
                        "error": None,
                    }
                    for node in plan.spark_group.nodes
                    if node.node_id in target_node_ids
                ],
            },
        }
        with service._sessions.begin() as session:
            nodes = list(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(target_node_ids))
                    .order_by(AgentNode.node_id)
                    .with_for_update()
                )
            )
            if len(nodes) != len(target_node_ids) or (
                plan.profile_stop_scope is not None
                and any(
                    node.revoked_at is not None or node.state != "active"
                    for node in nodes
                )
            ):
                raise RunSwitchRequestInvalid(
                    "run-switch target Spark scope changed",
                    reason=InvalidRequestReason.CONFLICT,
                )
            existing = session.scalar(select(Job).where(Job.request_id == request_key))
            if existing is not None:
                if existing.kind != kind or not _same_intent(existing, intent):
                    raise RunSwitchRequestInvalid(
                        RunSwitchCode.REQUEST_KEY_REUSED_DIFFERENTLY,
                        reason=InvalidRequestReason.CONFLICT,
                    )
                return service._operation_view(existing)
            _reserve_run_switch_assets(session, plan, now=now)
            try:
                lock_run_switch_build_dependency(session, plan)
            except BuildConsumerError as error:
                raise RunSwitchRequestInvalid(f"{error.code}: {error}") from error
            if workload_intent_ordinal is None:
                workload_intent_ordinal = (
                    max((node.workload_intent_ordinal for node in nodes), default=0) + 1
                )
                for node in nodes:
                    node.workload_intent_ordinal = workload_intent_ordinal
                service.request_superseded_workload_cancellation_in_session(
                    session,
                    tuple(node.node_id for node in nodes),
                    workload_intent_ordinal,
                    now,
                )
            elif (
                type(workload_intent_ordinal) is not int
                or workload_intent_ordinal < 1
                or any(
                    node.workload_intent_ordinal != workload_intent_ordinal
                    for node in nodes
                )
            ):
                raise RunSwitchRequestInvalid(
                    f"{RunSwitchCode.SUPERSEDED}: Spark scope has a later workload intent",
                    reason=InvalidRequestReason.SUPERSEDED,
                )
            payload["workload_intent_ordinal"] = workload_intent_ordinal
            payload["progress"]["workload_intent_ordinal"] = workload_intent_ordinal
            job = _ADAPTER.new_operation(
                allowed=plan.allowed,
                id=str(uuid.uuid4()),
                request_id=request_key,
                kind=kind,
                actor=actor,
                authority_revision=(plan.recipe_content_sha256 or plan.plan_digest),
                targets=list(target_node_ids),
                payload_digest=_digest(payload),
                payload=payload,
                result=_persisted_result(_read_progress(payload["progress"])),
                created_at=now,
                updated_at=now,
            )
            if not plan.allowed:
                progress = _read_progress(payload["progress"])
                blocked = "; ".join(reason.code for reason in plan.blockers[:8])
                _ADAPTER.retry(
                    job,
                    progress,
                    blocked,
                    now,
                    visible=_OBSERVING,
                    record_reason=False,
                    describe=lambda due: (
                        f"{blocked}; next re-plan at {due.isoformat()}"
                    ),
                )
                job.result = _persisted_result(progress)
            session.add(job)
            session.flush()
            return service._operation_view(job)

    def request_superseded_workload_cancellation_in_session(
        self,
        session: Session,
        targets: Sequence[str],
        ordinal: int,
        now: datetime,
    ) -> None:
        """Cancel exact older agent orders in the same ordinal-admission transaction."""

        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, targets, ordinal, now
        )

    def _existing_request_operation(
        self,
        request_key: str,
        *,
        kind: str,
        intent: RunSwitchIntent,
    ) -> RunSwitchOperation | None:
        """Replay a durable operation before re-planning mutable evidence.

        A client may retry after losing the initial response.  Looking up the
        request key first keeps that retry idempotent even if inventory or
        workload state has changed since the original preview.
        """
        service = typing_cast("RunSwitchOperationService", self)

        with service._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_key))
            if existing is None:
                return None
            if existing.kind != kind or not _same_intent(existing, intent):
                raise RunSwitchRequestInvalid(
                    RunSwitchCode.REQUEST_KEY_REUSED_DIFFERENTLY,
                    reason=InvalidRequestReason.CONFLICT,
                )
            return service._operation_view(existing)

    def _refresh_blocked_plan(self, operation_id: str, now: datetime) -> bool:
        """Re-plan accepted intent when its current admission evidence was blocked."""
        service = typing_cast("RunSwitchOperationService", self)
        with service._sessions() as session:
            row = session.get(Job, operation_id)
            if row is None or row.kind not in _OPERATION_KINDS:
                return False
            plan = _stored_job_plan(row)
            if _progress_damaged(read_row_column(row, "result")):
                # Unreadable evidence of what was issued is never re-planned
                # from the accepted payload: a Start may already exist.
                return False
            persisted_progress = _read_progress(read_row_column(row, "result"))
            if (
                plan is not None
                and plan.allowed
                and not persisted_progress.force_replan
            ):
                return False
            # Cancellation and newer intent are settled before any re-plan.
            if persisted_progress.cancellation or (
                service._scope_intent_status(session, row) != "current"
            ):
                return False
            due = persisted_progress.observation_due_at
            if due is not None and now < _aware(due):
                return False
            parent = _run_switch_payload(row)
            intent = parent.intent if parent is not None else None
            if intent is None:
                return False
            actor = row.actor
            profile_application_id = _string_or_none(
                persisted_progress.profile_application_id
            )

        try:
            if isinstance(intent, RunSwitchRunIntent):
                refreshed = service.preview(
                    intent.request,
                    actor=actor,
                    profile_application_id=profile_application_id,
                )
            elif isinstance(intent, RunSwitchStopIntent):
                refreshed = service.preview_stop(intent, actor=actor)
            elif isinstance(intent, RunSwitchCleanupIntent):
                refreshed = service.preview_cleanup(intent, actor=actor)
            elif isinstance(intent, RunSwitchProfileStopIntent):
                refreshed = service.preview_stop(
                    intent.run_id,
                    actor=actor,
                    profile_stop_scope=intent.profile_stop_scope,
                )
            else:
                # An intent this plan cannot be refreshed from is read as no
                # refreshed plan; the waiting operation backs off below.
                refreshed = None
        except UnknownOutcomeError as error:
            service._hold_after_advance_failure(operation_id, error)
            return True
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            if is_security_failure(error_code(error)):
                with service._sessions.begin() as session:
                    current = session.get(Job, operation_id, with_for_update=True)
                    if current is not None:
                        service._mark_failed(current, str(error), now=now)
                return True
            refreshed = None

        with service._sessions.begin() as session:
            current = session.get(Job, operation_id, with_for_update=True)
            if current is None or current.state not in job_states.words(
                LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.OBSERVING
            ):
                return True
            current_plan = _stored_job_plan(current)
            if _progress_damaged(read_row_column(current, "result")):
                return False
            progress = _read_progress(read_row_column(current, "result"))
            if (
                current_plan is not None
                and current_plan.allowed
                and not progress.force_replan
            ):
                return False
            if progress.cancellation or (
                service._scope_intent_status(session, current) != "current"
            ):
                return False
            # Target membership is fenced by the accepted workload ordinal on
            # exactly these Sparks. A refreshed plan naming other Sparks is not
            # this intent; wait (with backoff) until the plan matches again or a
            # newer request supersedes it.
            targets_changed = (refreshed is not None and refreshed.allowed) and sorted(
                _plan_target_node_ids(refreshed)
            ) != sorted(current.targets)
            if refreshed is not None and refreshed.allowed and not targets_changed:
                current_parent = _run_switch_payload(current)
                if current_parent is None:
                    return False
                current.payload = serialize_json_value(
                    current_parent.model_copy(
                        update={"plan": refreshed, "plan_digest": refreshed.plan_digest}
                    )
                )
                current.authority_revision = (
                    refreshed.recipe_content_sha256 or refreshed.plan_digest
                )
                total_bytes, _ = _planned_transfer_bytes(refreshed)
                # Replanned phases are idempotent, and even an unchanged shape
                # does not prove the old phase remains valid under new authority.
                # The new plan starts with no results from the old one and a new
                # child identity generation, so no installation, run, or adopted
                # Start of the old plan can be attributed to the new plan.
                phase_index = 0
                progress.retry_attempt = None
                progress = progress.model_copy(
                    update={
                        "phase_index": phase_index,
                        "item_index": 0,
                        "completed_phases": [],
                        "phase_results": [],
                        "child_operation_id": None,
                        "phase_retry_generation": require_integer(
                            progress.phase_retry_generation or 0,
                            "phase retry generation",
                        )
                        + 1,
                        "phase": refreshed.phases[phase_index].kind
                        if refreshed.phases
                        else "final_verify",
                        "subphase": refreshed.phases[phase_index].subphase
                        if refreshed.phases
                        else None,
                        "total_bytes": total_bytes,
                        "total_bytes_known": total_bytes is not None,
                        "observation_due_at": None,
                        "force_replan": False,
                    }
                )
                # The last refusal stays on record: if a fresh plan meets the
                # same refusal again, ``_fail`` retries in place and counts it.
                progress.failed_phase = None
                _ADAPTER.project(
                    current, progress, now, state=_LifecycleState.QUEUED, reason=None
                )
            else:
                reasons = (
                    RunSwitchCode.PLAN_TARGETS_CHANGED
                    if targets_changed
                    else "; ".join(reason.code for reason in refreshed.blockers[:8])
                    if refreshed is not None
                    else RunSwitchCode.PLAN_REFRESH_UNAVAILABLE
                )
                _ADAPTER.retry(
                    current,
                    progress,
                    reasons,
                    now,
                    visible=_OBSERVING,
                    record_reason=False,
                    describe=lambda due: (
                        f"{reasons}; next re-plan at {due.isoformat()}"
                    ),
                )
            current.result = _persisted_result(progress)
            current_parent = _run_switch_payload(current)
            if current_parent is None:
                return False
            current.payload = serialize_json_value(
                current_parent.model_copy(update={"progress": progress})
            )
            current.payload_digest = _digest(current.payload)
            current.updated_at = now
        return True
