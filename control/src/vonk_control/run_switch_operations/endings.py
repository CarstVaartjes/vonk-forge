"""Endings."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import LifecycleState, RunSwitchCode
from vonk_agent_protocol.agent_words import ProfileSwitchChildKind

from .. import job_states
from ..bounded_json import require_integer
from ..failure_classification import error_code, is_security_failure
from ..models import (
    Job,
    RecipeRun,
)
from ..recipe_operations import (
    RecipeOperationView,
    RecipeRequestInvalid,
)
from ..reservation_owners import release_dead_owner_reservations
from ..run_switch_contract import (
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchPlan,
)
from ..run_switch_progress import RECOVERY_STALL_WINDOW
from ..run_switch_progress import (
    _merge_progress_evidence as _merge_progress_evidence,  # noqa: PLC0414 -- shared helper export
)
from ..stall_window import Verdict, stall_deadline
from ..stored_json import read_row_column
from .errors import RunSwitchOperationConflict
from .observation_helpers import _start_child_request_key
from .observation_window import checkpoint_deadline, checkpoint_verdict
from .ownership import _checkpoint_matches, _complete_cancellation
from .planning_helpers import _aware, _now, _stored_job_plan
from .provider import _ADAPTER
from .result_helpers import _persisted_result, _read_progress

if TYPE_CHECKING:
    from ..distribution_executor.receipts import _ChildView
    from .service import RunSwitchOperationService


class EndingsMixin:
    def _expire_distribution_observation(
        self, operation_id: str, now: datetime
    ) -> bool:
        """End expired artifact observation before any mutable re-planning.

        The transfer owner fences its exact attempts; successful or unobserved
        remote effects are retained without withdrawing routes.
        """
        service = typing_cast("RunSwitchOperationService", self)
        from vonk_agent_protocol.agent_words import ProfileChildPhase

        from ..agent_jobs.retirement import release_owned_reservations_in_session

        with service._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or job.state not in {
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.OBSERVING,
            }:
                return False
            progress = _read_progress(read_row_column(job, "result"))
            deadline = progress.recovery_deadline_at
            plan = _stored_job_plan(job)
            if (
                deadline is None
                or now < _aware(deadline)
                or plan is None
                or progress.cancellation is not None
            ):
                return False
            if progress.phase_index >= len(plan.phases) or plan.phases[
                progress.phase_index
            ].kind not in {
                ProfileChildPhase.PREPARE,
                ProfileChildPhase.TRANSFER,
                ProfileChildPhase.VERIFY,
            }:
                return False
            if checkpoint_verdict(job, progress, now) is Verdict.WAITING:
                # Waiting on a live owner (a Spark returning, a queue) is not a
                # stalled transfer: the window restarts instead of ending it.
                progress.recovery_deadline_at = max(
                    _aware(deadline), stall_deadline(now, None, RECOVERY_STALL_WINDOW)
                )
                job.result = _persisted_result(progress)
                return False
            expire = getattr(service._phase_executor, "expire", None)
            if callable(expire):
                expire(
                    session,
                    progress.child_operation_id or progress.recovery_child_operation_id,
                    now,
                    plan_digest=plan.plan_digest,
                )
            service._mark_failed(
                job,
                "distribution observation window exhausted; remote effects remain unobserved",
                now=now,
                retryable=True,
                progress=progress,
            )
            release_owned_reservations_in_session(session, "job", job.id, now)
            return True

    def _settle_stop_observation(
        self,
        session: Session,
        job: Job,
        plan: RunSwitchPlan,
        progress: RunSwitchOperationResult,
        now: datetime,
    ) -> bool:
        """End an absent target or expired stop observer without inventing effects.

        A standalone clear's accepted request time owns its observation budget,
        including child waits and exceptions before final verification. It survives a
        restart and changing retry causes. The runtime owner still reconciles
        issued stops; ending its observer cannot withdraw a serving route or
        certify that physical capacity is free.
        """
        if (
            plan.action != ProfileSwitchChildKind.STOP
            or progress.cancellation
            or progress.profile_application_id is not None
        ):
            # A profile child is the durable Stop effect, shared by continuing
            # assignments and replacement/startup adoption. Its creation time
            # is not the lifetime of any one observer. Profile cancellation and
            # exact Stop authority own its retirement; a standalone clear's
            # observation budget must never retire this reusable effect.
            return False
        if plan.run_id is not None and session.get(RecipeRun, plan.run_id) is None:
            release_dead_owner_reservations(session, now)
            code = RunSwitchCode.STOP_TARGET_DISAPPEARED
        elif checkpoint_verdict(job, progress, now) is Verdict.STALLED:
            # Its Stop child is the owner of the wait: only once nothing it
            # waits on is live for a whole window does the observer end.
            code = RunSwitchCode.FINAL_VERIFICATION_TIMEOUT
        else:
            return False
        self._mark_failed(job, code, now=now, failure_code=code, progress=progress)
        return True

    def _settle_checkpoint_observation(
        self,
        session: Session,
        job: Job,
        progress: RunSwitchOperationResult,
        now: datetime,
    ) -> bool:
        """End this observer, preserving independent effects and accepted scope.

        The dependency and verification clocks cannot be reset by changing causes,
        corrupt receipts, re-planning or a worker restart. Ending observation
        neither Stops a child nor withdraws a serving route.
        """
        if (
            progress.cancellation is not None
            or progress.profile_application_id is not None
            and (plan := _stored_job_plan(job)) is not None
            and plan.action == ProfileSwitchChildKind.STOP
        ):
            return False
        if checkpoint_verdict(job, progress, now) is not Verdict.STALLED:
            return False
        progress.observation_deadline_at = checkpoint_deadline(job, progress, now)
        verification = (
            (plan := _stored_job_plan(job)) is None
            or plan.action not in {"run", "switch"}
            or progress.final_verify_started_at is not None
        )
        code = (
            RunSwitchCode.FINAL_VERIFICATION_TIMEOUT
            if verification
            else RunSwitchCode.DEPENDENCY_WINDOW_EXHAUSTED
        )
        self._mark_failed(
            job,
            code
            if verification
            else "dependency recovery window exhausted; exact effects remain unobserved",
            now=now,
            retryable=True,
            failure_code=code,
            progress=progress,
        )
        release_dead_owner_reservations(session, now)
        return True

    def _cancel_ended_start(self, operation_id: str) -> None:
        """An ended Run/Switch leaves no start retrying on its behalf.

        Work exists only for a live request. The start this operation issued
        (its deterministic request key) is cancelled through the recipe
        lifecycle's own cancel; a start that already ended is left alone.
        """
        service = typing_cast("RunSwitchOperationService", self)
        if service._lifecycle is None:
            return
        with service._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.state not in job_states.words(
                LifecycleState.FAILED, LifecycleState.CANCELLED
            ):
                return
            state = job.state
            start_request_id = _start_child_request_key(job.request_id)
            start_id = session.scalar(
                select(Job.id).where(
                    Job.kind == WireAgentOperation.RECIPE_START.value,
                    Job.request_id == start_request_id,
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                )
            )
        if start_id is None:
            return
        try:
            service._lifecycle.cancel(
                start_id,
                actor="controller",
                request_id=str(uuid.uuid5(uuid.UUID(start_request_id), operation_id)),
                reason=f"Run/Switch {operation_id} {state}; its start has no request",
            )
        except RecipeRequestInvalid:
            return  # the start ended meanwhile: nothing is left to cancel

    def _get_child_operation(
        self, operation_id: str
    ) -> RecipeOperationView | _ChildView | RunSwitchOperation | None:
        service = typing_cast("RunSwitchOperationService", self)
        from ..distribution_executor.receipts import _ChildView

        getter = getattr(service._phase_executor, "get", None)
        if callable(getter):
            try:
                child = getter(operation_id)
            except KeyError:
                child = None
            if isinstance(child, RecipeOperationView | _ChildView | RunSwitchOperation):
                return child
        if service._lifecycle is not None:
            return service._lifecycle.get(operation_id)
        return None

    def _fail(
        self,
        operation_id: str,
        reason: str,
        *,
        replan: bool = False,
        failure_code: str | None = None,
        checkpoint: tuple[int, int, object] | None = None,
        child_evidence: object | None = None,
        checkpoint_guard: Callable[[Session], Job | None] | None = None,
        clear_child: bool = False,
        definite: bool = False,
    ) -> None:
        """Observe uncertain children under their original identity and budget.

        Cancellation keeps its native owner. A denied grant or rejected ingress
        ends immediately; otherwise this observer retries without replacing an
        attached child or declaring its physical effects absent.
        """
        service = typing_cast("RunSwitchOperationService", self)

        with service._sessions.begin() as session:
            job = (
                checkpoint_guard(session)
                if checkpoint_guard is not None
                else session.get(Job, operation_id, with_for_update=True)
            )
            if job is None or job.state not in {
                LifecycleState.QUEUED.value,
                LifecycleState.RUNNING.value,
                LifecycleState.OBSERVING.value,
            }:
                return
            progress = _read_progress(read_row_column(job, "result"))
            if checkpoint is not None and not _checkpoint_matches(
                job, progress, *checkpoint
            ):
                return
            now = _now(service._clock)
            if child_evidence is not None and checkpoint is not None:
                plan = _stored_job_plan(job)
                if plan is not None and checkpoint[0] < len(plan.phases):
                    _merge_progress_evidence(
                        progress, plan, plan.phases[checkpoint[0]], child_evidence, now
                    )
            if progress.cancellation:
                if progress.child_operation_id is None:
                    _complete_cancellation(job, progress, now)
                else:
                    # A failure under a cancel is not the end of it: the cancel is
                    # driven (the child stopped, observed up to the budget).
                    service._cancel_with_session(session, job, progress, now, tick=True)
                    job.result = _persisted_result(progress)
                    job.updated_at = now
                return
            elif (
                not definite
                and not is_security_failure(failure_code)
                and checkpoint is not None
            ):
                # Re-enter the current phase with the same accepted intent and
                # deterministic child identity. The executor reconciles any
                # issued effect before it retries; the parent never replaces a
                # live child or changes the exact workload plan.
                # A re-plan restarts from the first phase, so it is only used
                # before a Start could have launched this intent's workload.
                # A cause that a fresh plan did not remove is not planning
                # drift: the same refusal after a re-plan retries in place, so
                # the attempts accumulate and surface as a named stall instead
                # of replanning (and forgetting its attempts) forever.
                progress.force_replan = (
                    replan
                    and checkpoint[2] is None
                    and progress.retry_reason != reason[:512]
                    and "start" not in progress.completed_phases
                    and progress.phase != "start"
                )
                if clear_child and checkpoint[2] is None:
                    progress.child_operation_id = None
                service._schedule_checkpoint_retry(job, progress, reason, now)
                return
            service._mark_failed(
                job,
                reason,
                now=now,
                failure_code=failure_code,
                progress=progress,
            )

    @staticmethod
    def _settle_invalid_receipt(
        job: Job, error: RunSwitchOperationConflict, now: datetime, *, reissue: bool
    ) -> None:
        """Observe the original effect when its post-effect receipt is unreadable."""

        code = error_code(error)
        progress = _read_progress(read_row_column(job, "result"))
        if error.definite or is_security_failure(code):
            EndingsMixin._mark_failed(
                job, str(error), now=now, progress=progress, failure_code=code
            )
            return
        # Cleanup callers never reissue: only the native owner can reconcile
        # an uncertain destructive effect under its original identity.
        if reissue:
            progress.child_operation_id = None
            progress.phase_retry_generation = (
                require_integer(
                    progress.phase_retry_generation or 0, "phase retry generation"
                )
                + 1
            )
        EndingsMixin._schedule_checkpoint_retry(job, progress, str(error), now)

    @staticmethod
    def _schedule_checkpoint_retry(
        job: Job, progress: RunSwitchOperationResult, reason: str, now: datetime
    ) -> None:
        """Wait at the exact checkpoint without replacing accepted intent."""
        plan = _stored_job_plan(job)
        reusable_stop = (
            progress.profile_application_id is not None
            and plan is not None
            and plan.action == ProfileSwitchChildKind.STOP
        )
        # The profile's native Stop owner outlives each assignment observer.
        if reusable_stop:
            _ADAPTER.retry(job, progress, reason, now)
            job.result = _persisted_result(progress)
            job.updated_at = now
            return
        deadline = checkpoint_deadline(job, progress, now)
        progress.observation_deadline_at = deadline
        if checkpoint_verdict(job, progress, now) is Verdict.STALLED:
            verification = (
                plan is None
                or plan.action not in {"run", "switch"}
                or progress.final_verify_started_at is not None
            )
            EndingsMixin._mark_failed(
                job,
                RunSwitchCode.FINAL_VERIFICATION_TIMEOUT
                if verification
                else "dependency recovery window exhausted; exact effects remain unobserved",
                now=now,
                retryable=True,
                failure_code=RunSwitchCode.FINAL_VERIFICATION_TIMEOUT
                if verification
                else RunSwitchCode.DEPENDENCY_WINDOW_EXHAUSTED,
                progress=progress,
            )
            return
        _ADAPTER.retry(job, progress, reason, now)
        if progress.observation_due_at is not None:
            progress.observation_due_at = min(progress.observation_due_at, deadline)
        job.result = _persisted_result(progress)
        job.updated_at = now

    @staticmethod
    def _mark_failed(
        job: Job,
        reason: str,
        *,
        now: datetime,
        retryable: bool = False,
        failure_code: str | None = None,
        progress: RunSwitchOperationResult | None = None,
    ) -> None:
        """Record failure using the caller's transaction and existing row lock."""

        if progress is None:
            progress = _read_progress(read_row_column(job, "result"))
        _ADAPTER.fail(
            job,
            progress,
            reason,
            now,
            failure_code=failure_code or RunSwitchCode.REASON_UNCLASSIFIED,
            retryable=retryable,
        )
        job.result = _persisted_result(progress)
        job.updated_at = now
