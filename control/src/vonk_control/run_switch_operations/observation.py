"""Observation."""

from __future__ import annotations

import traceback
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import String, and_, cast, func, literal, or_, select
from vonk_agent_protocol import (
    LifecycleState,
    RunSwitchCode,
)

from .. import job_states
from ..bounded_json import require_integer
from ..failure_classification import error_code, error_detail, is_definite
from ..lifecycle.evidence import (
    BookkeepingReason,
    retire_as_unknown,
)
from ..logging import log_event, redact_text
from ..model_cache import ModelCacheService
from ..models import (
    Job,
)
from ..operation_blockers import (
    bound_blockers,
)
from ..persisted_waits import schedule_wait
from ..stored_json import read_row_column
from .constants import _LOGGER, _OPERATION_KINDS
from .errors import RunSwitchOperationConflict
from .evidence_waits import wake_evidence_waits
from .planning_helpers import _now
from .provider import _ADAPTER
from .result_helpers import (
    _persisted_result,
    _progress_damaged,
    _read_progress,
    _wait_blockers,
)

if TYPE_CHECKING:
    from .provider import RunSwitchOperationProvider
    from .service import RunSwitchOperationService


def _definite(error: Exception) -> bool:
    """Whether an advance failure raises the same way on every tick.

    A run-switch conflict states it itself (the phase handler reads the same
    flag); any other error is definite unless it names a transient cause.
    """

    if isinstance(error, RunSwitchOperationConflict):
        return error.definite
    return is_definite(error)


class ObservationMixin:
    def activity_provider(self) -> RunSwitchOperationProvider:
        """Return the Run/Switch family adapter for global Activity.

        ``operation_api`` owns the shared provider dataclass.  Keeping the
        adapter's data projection here lets the global registry bind it by
        duck type while that API evolves (and keeps Activity from reading the
        high-level job payload directly).
        """
        service = typing_cast("RunSwitchOperationService", self)

        from .provider import RunSwitchOperationProvider

        return RunSwitchOperationProvider(service)

    def bind_model_cache(self, model_cache: ModelCacheService) -> None:
        """Bind the authoritative NAS cache after production composition."""
        service = typing_cast("RunSwitchOperationService", self)

        binder = getattr(service._artifacts, "bind_model_cache", None)
        if not callable(binder):
            # An inspector without a model-cache binding reports its artifact
            # evidence as unavailable, which admission already reconciles; the
            # composition continues instead of failing the Controller start.
            retire_as_unknown(
                "run-switch.model-cache-binding",
                type(service._artifacts).__name__,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "artifact inspector does not support model-cache binding",
            )
            return
        binder(model_cache)

    def tick(self) -> bool:
        """Give every due independent operation a bounded chance to advance."""
        service = typing_cast("RunSwitchOperationService", self)

        collect = getattr(
            service._artifact_phase_executor, "reconcile_background", None
        )
        if callable(collect):
            collect()
        # A wait on a Spark's inventory is due as soon as that inventory lands.
        wake_evidence_waits(service._sessions, _now(service._clock))

        # Canonical JSON emits UTC as Z; the clock's isoformat uses +00:00.
        # Compare the same spelling so an exactly due operation is eligible.
        due_at = Job.next_action_at
        deadline_at = func.replace(
            Job.result["observation_deadline_at"].as_string(), "Z", "+00:00"
        )
        recovery_deadline_at = func.replace(
            Job.result["recovery_deadline_at"].as_string(), "Z", "+00:00"
        )
        from ..models import RunSwitchJournalRepairPending

        with service._sessions() as session:
            active = (
                select(Job.id)
                .where(
                    Job.kind.in_(_OPERATION_KINDS),
                    or_(
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.OBSERVING,
                            )
                        ),
                        # Legacy: a wait with a clock is observed, one without is
                        # healed (re-evaluated) by the first advance, never left.
                        Job.state.in_(job_states.words(LifecycleState.NEEDS_OPERATOR)),
                    ),
                    or_(
                        Job.id.in_(select(RunSwitchJournalRepairPending.job_id)),
                        # Broad, cast-safe triage only; the journal owner proves
                        # the exact typed fault before any repair. An unrelated
                        # child observation clock cannot conceal damaged evidence.
                        and_(
                            cast(Job.result["total_bytes_known"].as_string(), String)
                            == cast(literal(True), String),
                            cast(Job.result["total_bytes"].as_string(), String)
                            == cast(literal(0), String),
                            cast(Job.result["completed_bytes"].as_string(), String)
                            != cast(literal(0), String),
                        ),
                        due_at.is_(None),
                        due_at <= _now(service._clock),
                        deadline_at <= _now(service._clock).isoformat(),
                        recovery_deadline_at <= _now(service._clock).isoformat(),
                        # A cancel in flight is looked at on every tick: it ends as
                        # soon as its child does (its stop attempts are spaced by
                        # the core, not by this clock).
                        Job.result["cancellation"].as_string().is_not(None),
                    ),
                )
                .order_by(Job.id)
                .limit(16)
            )
            if service._tick_cursor is None:
                job_ids = tuple(session.scalars(active))
            else:
                following = tuple(
                    session.scalars(active.where(Job.id > service._tick_cursor))
                )
                job_ids = following + tuple(
                    session.scalars(
                        active.where(Job.id <= service._tick_cursor).limit(
                            16 - len(following)
                        )
                    )
                )
        if job_ids:
            service._tick_cursor = str(job_ids[-1])
        advanced = False
        for job_id in job_ids:
            advanced = service.redrive(str(job_id)) or advanced
        if callable(collect):
            collect()
        return advanced

    def preflight_completed(self, child_id: str, node_id: str) -> None:
        """Make the exact owner due; only the worker may advance its phases.

        The worker polls this durable clock, so lost notifications are harmless.
        """
        service = typing_cast("RunSwitchOperationService", self)
        now = _now(service._clock)
        with service._sessions.begin() as session:
            owners = tuple(
                session.scalars(
                    select(Job)
                    .where(
                        Job.kind.in_(_OPERATION_KINDS),
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.OBSERVING,
                            )
                        ),
                        Job.result["preflight"]["pending_jobs"][node_id].as_string()
                        == child_id,
                    )
                    .order_by(Job.id)
                    .with_for_update(skip_locked=True)
                )
            )
            for job in owners:
                schedule_wait(job, now)

    def redrive(self, job_id: str) -> bool:
        """Advance one identity through the same fenced observation path."""
        service = typing_cast("RunSwitchOperationService", self)
        advancing = True
        try:
            advanced = service._advance(str(job_id))
            advancing = False
            service._record_wait(str(job_id))
            service._cancel_ended_start(str(job_id))
        except Exception as error:  # noqa: BLE001 - isolate every per-job failure
            # One persisted operation must never deny unrelated operations
            # their turn.  A malformed contract is rejected and retained by
            # ``_advance`` itself; this contains an unexpected per-job
            # failure, reports it, and lets the rest of the batch advance.
            # Every type is contained: one that escaped (a database error
            # from a background image preparation) left no retry state, so
            # the next tick repeated the work with the same status forever.
            log_event(
                _LOGGER,
                "run_switch.job_advance_failed",
                service="control-worker",
                job_id=str(job_id),
                error=type(error).__name__,
                message=redact_text(error),
                traceback=redact_text(traceback.format_exc()),
            )
            service._hold_after_advance_failure(
                str(job_id), error, definite=advancing and _definite(error)
            )
            return False
        return advanced

    def _hold_after_advance_failure(
        self, operation_id: str, error: Exception, *, definite: bool = False
    ) -> None:
        """End a definite advance failure; show any other one and back off.

        A definite failure (advancing raised a deterministic error, which the
        same input raises again on every tick) ends the operation with the
        error as its evidence.  Anything else keeps the checkpoint and is tried
        again, never as a silent endless retry: its wait names the failure and
        the next try.
        """
        service = typing_cast("RunSwitchOperationService", self)

        now = _now(service._clock)
        try:
            with service._sessions.begin() as session:
                job = session.get(Job, operation_id, with_for_update=True)
                if job is None or job.state not in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.OBSERVING,
                ):
                    return
                if _progress_damaged(read_row_column(job, "result")):
                    # An unexpected observer error cannot replace evidence of
                    # an issued child with the empty projection fallback.
                    return
                progress = _read_progress(read_row_column(job, "result"))
                code = error_code(error) or RunSwitchCode.ADVANCE_FAILED
                if definite and not progress.cancellation:
                    service._mark_failed(
                        job,
                        f"{code}: {redact_text(error_detail(error))}",
                        now=now,
                        failure_code=code,
                        progress=progress,
                    )
                    return
                attempt = (
                    require_integer(progress.retry_attempt, "retry attempt")
                    if progress.retry_reason == code
                    and progress.retry_attempt is not None
                    else 1
                )
                _ADAPTER.retry(
                    job,
                    progress,
                    code,
                    now,
                    reset_on_change=True,
                    describe=lambda due: (
                        f"{code}: {type(error).__name__}: {redact_text(error)}"[:400]
                        + f"; retry {attempt} at {due.isoformat()}"
                    ),
                )
                job.result = _persisted_result(progress)
                job.updated_at = now
            service._record_wait(operation_id)
        except Exception:  # noqa: BLE001 - the log above still names the failure
            return

    def _record_wait(self, operation_id: str) -> None:
        """Store what a waiting or retrying operation waits for; log changes.

        A retry is a wait, so the reason is kept next to the progress and shown
        wherever the operation is shown. The list is a current snapshot: it is
        replaced at each check and empty once the operation runs on or settles.
        """
        service = typing_cast("RunSwitchOperationService", self)

        from ..run_switch_journal_repair import is_zero_transfer_journal_fault

        with service._sessions() as session:
            snapshot = session.get(Job, operation_id)
            if snapshot is not None and is_zero_transfer_journal_fault(snapshot):
                # The bounded repair already owns this unknown observation. Do
                # not turn its NOWAIT refusal into an unbounded wait here.
                return
        with service._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or job.kind not in _OPERATION_KINDS:
                return
            if _progress_damaged(read_row_column(job, "result")):
                return
            progress = _read_progress(read_row_column(job, "result"))
            wanted = bound_blockers(_wait_blockers(job, progress))
            stored = wanted
            if stored == (progress.blockers or []):
                return
            before = {(item.code, tuple(item.node_ids)) for item in progress.blockers}
            if stored:
                progress.blockers = stored
            else:
                progress.blockers = []
            job.result = _persisted_result(progress)
            after = {(item.code, tuple(item.node_ids)) for item in wanted}
            if wanted and before != after:
                _LOGGER.info(
                    "run/switch %s is waiting: %s",
                    operation_id,
                    "; ".join(f"{item.code}: {item.detail}" for item in wanted[:4]),
                )
