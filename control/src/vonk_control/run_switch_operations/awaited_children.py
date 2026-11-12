"""The jobs a Run/Switch awaits: one answer for its wake, window and cleanup.

A Run/Switch waits on its phase child, its recovery child, the start it issued
and the preflight probes of its gate. The child-end wake, the observation
window and the ended-parent cleanup all ask this, so a job one of them waits
on cannot be one another forgets.

The answer is pure (no session): the start is named by its deterministic
request key, which each caller resolves with the one query it already makes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Job
from ..run_switch_contract import RunSwitchOperationResult, RunSwitchPlan
from .observation_helpers import _start_child_request_key


@dataclass(frozen=True, slots=True)
class AwaitedJobs:
    """Job ids named by the progress, and the key of the start it may have issued."""

    job_ids: tuple[str, ...]
    start_request_key: str | None


def start_request_key(job: Job) -> str:
    """The request key of the start this Run/Switch issues (whether or not it did)."""
    return _start_child_request_key(job.request_id)


def child_job_ids(progress: RunSwitchOperationResult) -> tuple[str, ...]:
    """The jobs the progress names: phase child, recovery child, preflight probes."""

    ids = [progress.child_operation_id, progress.recovery_child_operation_id]
    if progress.preflight is not None:
        ids.extend(progress.preflight.pending_jobs.values())
    return tuple(dict.fromkeys(child for child in ids if child is not None))


def awaited_jobs(
    job: Job, progress: RunSwitchOperationResult, plan: RunSwitchPlan | None
) -> AwaitedJobs:
    """Everything this Run/Switch waits on, with the key of the start it issues."""

    issues_start = plan is not None and plan.action in {"run", "switch"}
    return AwaitedJobs(
        child_job_ids(progress), start_request_key(job) if issues_start else None
    )
