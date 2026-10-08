"""Endings."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState, RunSwitchCode

from ..bounded_json import require_integer
from ..failure_classification import error_code, is_security_failure
from ..models import (
    Job,
)
from ..recipe_operations import (
    RecipeOperationView,
)
from ..run_switch_contract import (
    RunSwitchOperation,
    RunSwitchOperationResult,
)
from ..run_switch_progress import (
    _merge_progress_evidence as _merge_progress_evidence,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from .errors import RunSwitchOperationConflict
from .ownership import _checkpoint_matches, _complete_cancellation
from .planning_helpers import _now, _stored_job_plan
from .provider import _ADAPTER
from .result_helpers import _persisted_result, _read_progress

if TYPE_CHECKING:
    from ..distribution_executor import _ChildView
    from .service import RunSwitchOperationService


class EndingsMixin:
    def _get_child_operation(
        self, operation_id: str
    ) -> RecipeOperationView | _ChildView | RunSwitchOperation | None:
        service = typing_cast("RunSwitchOperationService", self)
        from ..distribution_executor import _ChildView

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
        """A phase could not be settled.

        Only a real security refusal (``is_security_failure``) is a definite end.
        Everything else (a receipt that does not validate, a verification that
        cannot be observed, a missing child, a wiring gap) is an unknown: the same
        idempotent phase is entered again at the core's bounded backoff, so the
        failure never ends a load whose bytes and workload are fine.  The caller
        no longer chooses; the classifier does (``failure_classification``).
        ``definite`` is only for an owner that typed its own verdict (the image
        preparation's non-retryable errors: an invalid archive or identity).
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
                and (checkpoint[2] is None or clear_child)
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
                    and progress.retry_reason != reason[:512]
                    and "start" not in progress.completed_phases
                    and progress.phase != "start"
                )
                if clear_child:
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
        """A receipt did not validate inside the checkpoint transaction (rule 5).

        Its effect is unknown, so the phase is entered again at the core's backoff:
        an idempotent child is issued again under a new identity (``reissue``), a
        receipt read from the Controller's own records is simply observed again.
        Only a reviewed destructive-effect or digest guard ends the operation.
        The progress is re-read, so nothing half-merged from the invalid receipt is
        kept.
        """

        code = error_code(error)
        progress = _read_progress(read_row_column(job, "result"))
        if error.definite or is_security_failure(code):
            EndingsMixin._mark_failed(
                job, str(error), now=now, progress=progress, failure_code=code
            )
            return
        if reissue:
            progress.child_operation_id = None
            progress.phase_retry_generation = (
                require_integer(
                    progress.phase_retry_generation or 0,
                    "phase retry generation",
                )
                + 1
            )
        EndingsMixin._schedule_checkpoint_retry(job, progress, str(error), now)

    @staticmethod
    def _schedule_checkpoint_retry(
        job: Job, progress: RunSwitchOperationResult, reason: str, now: datetime
    ) -> None:
        """Wait at the exact checkpoint without replacing accepted intent."""
        _ADAPTER.retry(job, progress, reason, now)
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
