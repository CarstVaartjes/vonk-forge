"""Observation."""

from __future__ import annotations

import traceback
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import func, or_, select
from vonk_agent_protocol import (
    LifecycleState,
    RunSwitchCode,
    UnknownOutcomeError,
)

from .. import job_states
from ..bounded_json import require_integer
from ..failure_classification import error_code
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
from ..stored_json import read_row_column
from .constants import _LOGGER, _OPERATION_KINDS
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

        # Canonical JSON emits UTC as Z; the clock's isoformat uses +00:00.
        # Compare the same spelling so an exactly due operation is eligible.
        due_at = func.replace(
            Job.result["observation_due_at"].as_string(), "Z", "+00:00"
        )
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
                        due_at.is_(None),
                        due_at <= _now(service._clock).isoformat(),
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
            try:
                advanced = service._advance(str(job_id)) or advanced
                service._record_wait(str(job_id))
            except (
                UnknownOutcomeError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                KeyError,
            ) as error:
                # One persisted operation must never deny unrelated operations
                # their turn.  A malformed contract is rejected and retained by
                # ``_advance`` itself; this contains an unexpected per-job
                # failure, reports it, and lets the rest of the batch advance.
                log_event(
                    _LOGGER,
                    "run_switch.job_advance_failed",
                    service="control-worker",
                    job_id=str(job_id),
                    error=type(error).__name__,
                    message=redact_text(error),
                    traceback=redact_text(traceback.format_exc()),
                )
                service._hold_after_advance_failure(str(job_id), error)
                continue
        return advanced

    def _hold_after_advance_failure(self, operation_id: str, error: Exception) -> None:
        """Show an unexpected advance failure on the operation and back off.

        The operation keeps its checkpoint and is tried again, but it is never
        a silent endless retry: its wait names the failure and the next try.
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
        except (OSError, RuntimeError, TypeError, ValueError, KeyError):
            return  # the log above still names the failure

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
