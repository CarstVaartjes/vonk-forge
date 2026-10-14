"""How long a Run/Switch observer waits: never while an owner of its wait is live.

A Run/Switch that waits for a target Spark to return, for its start to load a
model, or for its child operation to finish is waiting on a live owner, and
that wait never expires (``stall_window.progress_verdict``). Its observation
window runs only while nothing it waits on is live, and restarts when that
owner ended: a start that loaded for an hour leaves the full verification
window for the route evidence that follows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import object_session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import RunSwitchCode

from ..admission_locking import AdmissionLockBusy
from ..install_admission import InstallAdmissionBusy
from ..models import Job
from ..recipe_builds import RecipeBuildAdmissionBusy
from ..reservation_owners import Owner, OwnerKind, OwnerRef, owner
from ..run_admission import (
    RETRYABLE_PLAN_BLOCKERS,
    RUN_ADMISSION_WAIT_CODES,
    RunAdmissionBusy,
)
from ..run_switch_contract import RunSwitchOperationResult
from ..stall_window import Verdict, progress_deadline
from .constants import _FINAL_VERIFICATION_MAX_SECONDS
from .errors import RunSwitchPostStopEvidencePending
from .observation_helpers import _start_child_request_key
from .planning_helpers import _aware, _stored_job_plan

#: Dependency recovery before verification: stayed unreadable.
DEPENDENCY_WINDOW = timedelta(hours=1)
#: Exact run and route evidence after the start (or the child) ended.
VERIFICATION_WINDOW = timedelta(seconds=_FINAL_VERIFICATION_MAX_SECONDS)
#: Waits on the target Sparks' queue or owners: a Spark returning, a busy
#: admission, capacity another load holds, a Spark's post-stop report.
TARGET_WAITS = frozenset(
    {
        RunSwitchCode.TARGET_NOT_ACTIVE,
        AdmissionLockBusy.code,
        InstallAdmissionBusy.code,
        RunAdmissionBusy.code,
        RecipeBuildAdmissionBusy.code,
        RunSwitchPostStopEvidencePending.code,
        *RETRYABLE_PLAN_BLOCKERS,
        *RUN_ADMISSION_WAIT_CODES,
    }
)


def observation_owners(
    job: Job, progress: RunSwitchOperationResult
) -> tuple[Owner, ...]:
    """What this observer waits on: its child, its issued start, its Sparks."""

    session = object_session(job)
    if session is None:
        return ()
    refs = [
        OwnerRef(OwnerKind.JOB, child)
        for child in (progress.child_operation_id, progress.recovery_child_operation_id)
        if child is not None
    ]
    plan = _stored_job_plan(job)
    run_or_switch = plan is not None and plan.action in {"run", "switch"}
    if run_or_switch:
        start = session.scalar(
            select(Job.id).where(
                Job.kind == WireAgentOperation.RECIPE_START.value,
                Job.request_id == _start_child_request_key(job.request_id),
            )
        )
        if start is not None:
            refs.append(OwnerRef(OwnerKind.JOB, start))
    owners = [owner(session, ref) for ref in refs]
    if (
        run_or_switch
        and progress.final_verify_started_at is None
        and progress.retry_reason in TARGET_WAITS
        and job.targets
    ):
        # Waiting for a target Spark to return waits on every target: one that
        # left the Fleet ends the wait, and the window restarts when it left.
        sparks = [
            owner(session, OwnerRef(OwnerKind.NODE, node)) for node in job.targets
        ]
        ended = [spark.ended_at for spark in sparks if spark.ended_at is not None]
        owners.append(
            Owner(
                OwnerRef(OwnerKind.NODE, ",".join(job.targets)),
                all(spark.live for spark in sparks),
                "; ".join(spark.why for spark in sparks),
                max(ended) if ended else None,
            )
        )
    return tuple(owners)


def _window(job: Job, progress: RunSwitchOperationResult) -> tuple[datetime, timedelta]:
    plan = _stored_job_plan(job)
    if plan is not None and plan.action in {"run", "switch"}:
        if progress.final_verify_started_at is None:
            return _aware(job.created_at), DEPENDENCY_WINDOW
        started = datetime.fromtimestamp(progress.final_verify_started_at, UTC)
        return started, VERIFICATION_WINDOW
    return _aware(job.created_at), VERIFICATION_WINDOW


def _transfer_deadline(job: Job, progress: RunSwitchOperationResult) -> datetime | None:
    """A preparation or transfer's own byte-anchored stall window, if it runs one."""

    plan = _stored_job_plan(job)
    if (
        plan is not None
        and plan.action in {"run", "switch"}
        and progress.final_verify_started_at is None
    ):
        return progress.recovery_deadline_at
    return None


def checkpoint_deadline(
    job: Job,
    progress: RunSwitchOperationResult,
    now: datetime,
    owners: tuple[Owner, ...] | None = None,
) -> datetime:
    """When this observer stalls; a transfer's byte-anchored window counts too."""

    owners = observation_owners(job, progress) if owners is None else owners
    started, window = _window(job, progress)
    waited = progress_deadline(
        started_at=started,
        progressed_at=None,
        window=window,
        now=now,
        waiting_on=owners,
    )
    transfer = _transfer_deadline(job, progress)
    if transfer is None:
        return waited
    if any(item.live for item in owners):
        return max(_aware(transfer), waited)
    return transfer


def checkpoint_verdict(
    job: Job, progress: RunSwitchOperationResult, now: datetime
) -> Verdict:
    """``STALLED`` only once nothing it waits on is live for a whole window."""

    owners = observation_owners(job, progress)
    if any(item.live for item in owners):
        return Verdict.WAITING
    if _aware(now) >= _aware(checkpoint_deadline(job, progress, now, owners)):
        return Verdict.STALLED
    return Verdict.PROGRESSING
