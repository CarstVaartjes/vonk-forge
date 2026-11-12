"""The jobs a Run/Switch awaits: one answer for its wake and its observation window.

A Run/Switch waits on its phase child, its recovery child, the start it issued
and the preflight probes of its gate. The child-end wake and the observation
window both ask this, so a job one of them waits on cannot be one the other
forgets.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation

from ..models import Job
from ..run_switch_contract import RunSwitchOperationResult
from .observation_helpers import _start_child_request_key
from .planning_helpers import _stored_job_plan


def awaited_job_ids(
    session: Session, job: Job, progress: RunSwitchOperationResult
) -> tuple[str, ...]:
    """Every job id this Run/Switch waits on, each once."""

    ids = [progress.child_operation_id, progress.recovery_child_operation_id]
    plan = _stored_job_plan(job)
    if plan is not None and plan.action in {"run", "switch"}:
        ids.append(
            session.scalar(
                select(Job.id).where(
                    Job.kind == WireAgentOperation.RECIPE_START.value,
                    Job.request_id == _start_child_request_key(job.request_id),
                )
            )
        )
    if progress.preflight is not None:
        ids.extend(progress.preflight.pending_jobs.values())
    return tuple(dict.fromkeys(child for child in ids if child is not None))
