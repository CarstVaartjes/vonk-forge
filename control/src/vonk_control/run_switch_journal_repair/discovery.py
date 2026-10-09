"""Run switch journal repair: discovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..admission_locking import AdmissionRowLock, lock_admission_rows
from ..models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    FleetProfileApplication,
    Job,
)
from ..run_switch_contract import (
    RunSwitchOperationResult,
)
from ..run_switch_journal_contract import (
    JournalRepairCode,
)

REPAIR_WAIT = JournalRepairCode.EVIDENCE_UNAVAILABLE


def journal_document(value: object) -> str:
    """Stable exact encoding of the SQL JSON document, including stored nulls."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _candidate(job: Job) -> RunSwitchOperationResult | None:
    raw = job.result
    if not isinstance(raw, dict):
        return None
    completed = raw.get("completed_bytes")
    total = raw.get("total_bytes")
    if (
        type(completed) is not int
        or completed <= 0
        or type(total) is not int
        or total != 0
        or raw.get("total_bytes_known") is not True
    ):
        return None
    corrected = dict(raw)
    corrected["completed_bytes"] = 0
    try:
        return RunSwitchOperationResult.model_validate_json(
            journal_document(corrected), strict=True
        )
    except (TypeError, ValueError):
        return None


def is_zero_transfer_journal_fault(job: Job) -> bool:
    return _candidate(job) is not None


@dataclass(frozen=True, slots=True)
class _Discovery:
    """Transient complete lock declaration; never a persisted authority."""

    targets: tuple[str, ...]
    job_ids: tuple[str, ...]
    application_id: str | None
    native_ids: tuple[str, ...]
    attempt_ids: tuple[str, ...]
    fingerprints: tuple[tuple[str, str, str], ...]


def _fingerprint(
    row: AgentNode
    | Job
    | FleetProfileApplication
    | AgentOperation
    | AgentOperationAttempt,
) -> tuple[str, str, str]:
    # SQL ownership snapshots include state, cancellation, fences and samples.
    # Datetimes are encoded only for equality, never interpreted as a schedule.
    values = {
        column.key: (
            value.isoformat()
            if isinstance(value := getattr(row, column.key), datetime)
            else value
        )
        for column in row.__mapper__.columns
    }
    identity = str(row.node_id if isinstance(row, AgentNode) else row.id)
    return (
        row.__tablename__,
        identity,
        hashlib.sha256(journal_document(values).encode()).hexdigest(),
    )


def _discover(session: Session, operation_id: str) -> _Discovery | None:
    job = session.get(Job, operation_id, populate_existing=True)
    progress = _candidate(job) if job is not None else None
    if job is None or progress is None or progress.child_operation_id is None:
        return None
    child = session.get(Job, progress.child_operation_id, populate_existing=True)
    if child is None:
        return None
    nodes = tuple(
        session.scalars(
            select(AgentNode)
            .where(AgentNode.node_id.in_(job.targets))
            .execution_options(populate_existing=True)
        )
    )
    application = (
        session.get(
            FleetProfileApplication,
            progress.profile_application_id,
            populate_existing=True,
        )
        if progress.profile_application_id
        else None
    )
    operations = tuple(
        session.scalars(
            select(AgentOperation)
            .where(AgentOperation.parent_job_id == child.id)
            .execution_options(populate_existing=True)
        )
    )
    attempts = tuple(
        session.scalars(
            select(AgentOperationAttempt)
            .join(
                AgentOperation, AgentOperation.id == AgentOperationAttempt.operation_id
            )
            .where(
                AgentOperation.parent_job_id == child.id,
                AgentOperationAttempt.attempt == AgentOperation.current_attempt,
            )
            .execution_options(populate_existing=True)
        )
    )
    rows: list[
        AgentNode
        | Job
        | FleetProfileApplication
        | AgentOperation
        | AgentOperationAttempt
    ] = [job, child, *nodes, *operations, *attempts]
    if application is not None:
        rows.append(application)
    return _Discovery(
        targets=tuple(sorted(job.targets)),
        job_ids=tuple(sorted({job.id, child.id})),
        application_id=progress.profile_application_id,
        native_ids=tuple(sorted(row.id for row in operations)),
        attempt_ids=tuple(sorted(row.id for row in attempts)),
        fingerprints=tuple(sorted(_fingerprint(row) for row in rows)),
    )


def _lock_discovery(session: Session, captured: _Discovery) -> None:
    requests = [
        AdmissionRowLock(
            "repair-nodes",
            AgentNode,
            select(AgentNode).where(AgentNode.node_id.in_(captured.targets)),
        ),
        AdmissionRowLock(
            "repair-jobs", Job, select(Job).where(Job.id.in_(captured.job_ids))
        ),
        AdmissionRowLock(
            "repair-native",
            AgentOperation,
            select(AgentOperation).where(AgentOperation.id.in_(captured.native_ids)),
        ),
        AdmissionRowLock(
            "repair-attempts",
            AgentOperationAttempt,
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.id.in_(captured.attempt_ids)
            ),
        ),
    ]
    if captured.application_id is not None:
        requests.append(
            AdmissionRowLock(
                "repair-profile",
                FleetProfileApplication,
                select(FleetProfileApplication).where(
                    FleetProfileApplication.id == captured.application_id
                ),
            )
        )
    # One complete declaration, common namespace+immutable-PK order. Newly
    # discovered identities are NEVER appended to this transaction's lock set.
    lock_admission_rows(session, requests)


def _plan_digest(job: Job) -> str:
    from ..run_switch_operations import _run_switch_payload

    parent = _run_switch_payload(job)
    if parent is None:
        raise ValueError("accepted parent disappeared")
    return parent.plan_digest
