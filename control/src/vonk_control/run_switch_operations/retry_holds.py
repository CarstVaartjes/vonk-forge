"""Bounded observation checkpoints."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    OperationProgress,
    RunSwitchCode,
)

from .. import job_states
from ..bounded_json import require_integer
from ..lifecycle.run_switch import (
    RunSwitchAdapter,
)
from ..lifecycle.types import (
    State as _LifecycleState,
)
from ..models import (
    AgentNode,
    Job,
)
from ..recipe_operations import (
    RecipeOperationConflict,
)
from ..run_switch_contract import (
    RunSwitchOperationResult,
)
from ..stored_json import read_row_column
from .constants import (
    _INSTALL_PREFLIGHT_REFRESH_REASON,
    _LOGGER,
    _OBSERVING,
    _TERMINAL_STATES,
)
from .errors import RunSwitchIssuedWorkloadPending
from .ownership import _checkpoint_matches, _complete_cancellation
from .planning_helpers import _aware, _now, _run_switch_payload, _start_parent
from .provider import _ADAPTER
from .result_helpers import _observe_progress, _persisted_result, _read_progress

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class RetryHoldsMixin:
    def _never_installed(self, installation_id: str) -> bool:
        """Whether the installation's own assessment proves no node effect."""
        service = typing_cast("RunSwitchOperationService", self)

        if service._lifecycle is None:
            return False
        try:
            assessment = service._lifecycle.preview_uninstall(installation_id)
        except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError):
            return False
        return assessment.allowed and assessment.disposition == "abandon"

    @staticmethod
    def _scope_intent_status(session: Session, job: Job) -> str:
        """Distinguish malformed authority from a later authorized node head."""

        parent = _run_switch_payload(job)
        ordinal = parent.workload_intent_ordinal if parent is not None else None
        if type(ordinal) is not int or ordinal < 1:
            return "invalid"
        nodes = session.scalars(
            select(AgentNode).where(AgentNode.node_id.in_(job.targets))
        )
        current = list(nodes)
        if any(node.revoked_at is not None for node in current):
            return "invalid"
        if len(current) != len(job.targets):
            return "missing-target"
        if any(node.state != "active" for node in current):
            return "waiting"
        if any(node.workload_intent_ordinal != ordinal for node in current):
            return "superseded"
        return "current"

    def _hold_start_observation(
        self,
        operation_id: str,
        *,
        child_id: str,
        phase_index: int,
        item_index: int,
    ) -> bool:
        """Observe a progressing uncertain start until its immutable deadline."""
        service = typing_cast("RunSwitchOperationService", self)

        now = _now(service._clock)
        with service._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            progress = _read_progress(read_row_column(job, "result"))
            if not _checkpoint_matches(
                job, progress, phase_index, item_index, child_id
            ):
                return False
            raw_deadline = progress.observation_deadline_at
            if raw_deadline is not None:
                deadline = _aware(raw_deadline)
            else:
                child = session.get(Job, child_id)
                child_parent = _start_parent(child)
                child_deadline = (
                    child_parent.start_deadline if child_parent is not None else None
                )
                deadline = (
                    _aware(child_deadline)
                    if child_deadline is not None
                    else now + timedelta(seconds=120)
                )
            if now >= deadline:
                # Expiry establishes an overdue observation, not a stopped or
                # failed runtime. Preserve the exact child and reservations;
                # its owner alone can reconcile or retry the uncertain effect.
                progress.observation_deadline_at = deadline
                _ADAPTER.retry(
                    job,
                    progress,
                    RunSwitchCode.START_OBSERVATION_EXPIRED,
                    now,
                    visible=_OBSERVING,
                    record_reason=False,
                    describe=lambda due: (
                        f"{RunSwitchCode.START_OBSERVATION_EXPIRED}: exact effect remains "
                        f"unresolved; next observation at {due.isoformat()}"
                    ),
                )
                job.result = _persisted_result(progress)
                job.updated_at = now
                return True
            progress.observation_deadline_at = deadline
            _ADAPTER.project(
                job,
                progress,
                now,
                state=_LifecycleState.OBSERVING,
                due=min(deadline, now + timedelta(seconds=5)),
                visible=LifecycleState.RUNNING.value,
                reason="Start result uncertain; observing the existing run.",
            )
            job.result = _persisted_result(progress)
            job.updated_at = now
        return True

    def _hold_issued_observation(
        self,
        operation_id: str,
        *,
        phase_index: int,
        item_index: int,
        pending: RunSwitchIssuedWorkloadPending,
    ) -> bool:
        """Bound polling of an issued older effect without authorizing replay."""
        service = typing_cast("RunSwitchOperationService", self)

        now = _now(service._clock)
        with service._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            progress = _read_progress(read_row_column(job, "result"))
            if not _checkpoint_matches(job, progress, phase_index, item_index, None):
                return False
            deadline = _aware(pending.observation_deadline)
            # The lifecycle admission path already re-observes this exact
            # dependency before issuing anything. A missing cancellation receipt
            # cannot turn that safe observation into permanently parked intent.
            due = (
                now + timedelta(seconds=60)
                if now >= deadline
                else min(
                    deadline,
                    max(now + timedelta(seconds=5), _aware(pending.observe_due_at)),
                )
            )
            progress.observation_deadline_at = deadline
            _ADAPTER.project(
                job,
                progress,
                now,
                state=_LifecycleState.OBSERVING,
                due=due,
                visible=LifecycleState.RUNNING.value,
                reason=(
                    f"Observing older {pending.kind} operation {pending.job_id}; "
                    f"effect unresolved, next observation at {due.isoformat()}"
                ),
            )
            job.result = _persisted_result(progress)
            job.updated_at = now
        return True

    def _hold_capacity_writer(
        self,
        operation_id: str,
        phase_index: int,
        item_index: int,
        *,
        reason: str,
        detail: str | None = None,
        not_before: datetime | None = None,
    ) -> bool:
        """An unchanged capacity handoff uses bounded exponential backoff.

        The admission transaction has rolled back and released its locks.
        The wait belongs to the existing operation, holds no worker slot, and
        expires at the next admission attempt; busy SQL is not a failed effect.
        """
        service = typing_cast("RunSwitchOperationService", self)
        now = _now(service._clock)
        with service._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            progress = _read_progress(read_row_column(job, "result"))
            if not _checkpoint_matches(job, progress, phase_index, item_index, None):
                return False
            if progress.cancellation:
                _complete_cancellation(job, progress, now)
            else:
                attempt = (
                    require_integer(progress.retry_attempt, "retry attempt")
                    if progress.retry_reason == reason
                    and progress.retry_attempt is not None
                    else 1
                )
                _ADAPTER.retry(
                    job,
                    progress,
                    reason,
                    now,
                    reset_on_change=True,
                    retry_after=not_before,
                    describe=lambda due: (
                        (
                            detail
                            or "Admission is waiting for the Controller capacity writer"
                        )
                        + (
                            f"; admission retry {attempt} in "
                            f"{max(int((due - now).total_seconds()), 1)}s "
                            f"at {due.isoformat()}."
                        )
                    ),
                )
                progress.observation_deadline_at = progress.observation_due_at
                job.result = _persisted_result(progress)
                job.updated_at = now
        return True

    def _hold_for_preflight_refresh(
        self,
        operation_id: str,
        phase_index: int,
        item_index: int,
        *,
        cause: str | None = None,
    ) -> bool:
        """Keep the runtime-plan checkpoint so the existing gate reprobes.

        Acceptance rolled its transaction back, so no installation, reservation
        or agent child exists.  Leaving ``phase_index`` and ``item_index``
        untouched sends the next tick back through ``LifecyclePreflight.ensure``.
        Back off even when the gate's cached receipt is still fresh while
        admission selects a different, expired database receipt. The existing
        typed retry fields retain the compilation attempt, cause and next time;
        temporary freshness races cannot permanently abandon accepted intent.

        A hold never advances a cancelled operation into a subsequent
        acceptance.  Cancellation racing a successful acceptance is unchanged.
        """
        service = typing_cast("RunSwitchOperationService", self)

        with service._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            progress = _read_progress(read_row_column(job, "result"))
            if not _checkpoint_matches(job, progress, phase_index, item_index, None):
                return False
            # Compilation may have taken minutes; persist the actual hold time.
            now = _now(service._clock)
            if progress.cancellation:
                _complete_cancellation(job, progress, now)
                return True
            attempt = (
                require_integer(progress.retry_attempt, "retry attempt")
                if progress.retry_reason == _INSTALL_PREFLIGHT_REFRESH_REASON
                and progress.retry_attempt is not None
                else 1
            )
            progress.retry_reason = _INSTALL_PREFLIGHT_REFRESH_REASON
            progress.operation_phase_index = phase_index
            progress.operation = _observe_progress(
                progress.operation,
                OperationProgress.model_validate(
                    {
                        "phase": "install-preflight-refresh",
                        "completed_items": attempt,
                        "completed_bytes": 0,
                        "total_bytes_known": False,
                    }
                ),
                now,
            )
            service._schedule_checkpoint_retry(
                job, progress, _INSTALL_PREFLIGHT_REFRESH_REASON, now
            )
            if cause:
                # Say which runtime evidence was refused, not only that the
                # probe runs again: a repeating cause is the stall to look at.
                job.status_reason = (
                    f"{job.status_reason}; attempt {attempt}: {cause}"
                )[:512]
            _LOGGER.warning(
                "run/switch %s: install admission refused its runtime preflight "
                "evidence (attempt %d: %s); probing the Sparks again",
                operation_id,
                attempt,
                cause or "expired",
            )
        return True

    def _stop_child(self, session: Session, job: Job, now: datetime) -> bool:
        """The adapter's idempotent stop: whether nothing of the child still runs.

        A child that already ended, or a parked idempotent one that can be closed
        (its copied bytes stay on the Spark), is stopped.  A running child is
        observed, not aborted: its own lifecycle owns it, and the core's stop budget
        ends the cancel either way.
        """
        service = typing_cast("RunSwitchOperationService", self)

        child_id = _read_progress(read_row_column(job, "result")).child_operation_id
        if not isinstance(child_id, str) or not child_id:
            return True
        try:
            child = service._get_child_operation(child_id)
        except KeyError:
            return True  # nothing is left to stop
        if child is None or child.state in _TERMINAL_STATES:
            return True
        if child.state in job_states.words(LifecycleState.NEEDS_OPERATOR):
            return service._abandon_idempotent_child(session, child_id, now)
        return False

    def _cancel_with_session(
        self,
        session: Session,
        job: Job,
        progress: RunSwitchOperationResult,
        now: datetime,
        *,
        tick: bool = False,
    ) -> bool:
        """Drive a recorded cancel through the core (rule 4); it always completes.

        Returns whether the cancel moved (a tick that is not due changes nothing).
        """
        service = typing_cast("RunSwitchOperationService", self)

        adapter = RunSwitchAdapter(
            session, clock=lambda: now, stopper=service._stop_child
        )
        if tick:
            return adapter.tick_cancel(job, progress, now)
        adapter.request_cancel(job, progress, now)
        return True

    def _abandon_idempotent_child(
        self, session: Session, operation_id: str, now: datetime
    ) -> bool:
        """Ask the phase executor to close a parked idempotent child, if it can."""
        service = typing_cast("RunSwitchOperationService", self)

        abandon = getattr(service._phase_executor, "abandon", None)
        if not callable(abandon):
            return False
        return bool(
            abandon(
                session,
                operation_id,
                now,
                reason="Cancelled with its Run/Switch order; copied bytes remain "
                "on the Spark for reuse",
            )
        )
