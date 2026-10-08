"""Scope for the node-scoped agent queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..admission_locking import AdmissionRowLock, lock_admission_rows
from ..models import AgentNode, Job
from ..models import AgentOperation as StoredOperation
from .stored import column_value

if TYPE_CHECKING:
    from .service import AgentJobService


def _target_scope(targets: object) -> tuple[str, ...] | None:
    if (
        not isinstance(targets, list)
        or not targets
        or not all(isinstance(node_id, str) for node_id in targets)
        or len(targets) != len(set(targets))
    ):
        return None
    return tuple(sorted(targets))


def _lock_operation_scopes(
    cls: type[AgentJobService],
    session: Session,
    operation_ids: tuple[str, ...],
    node_id: str,
    *,
    nowait: bool = False,
) -> dict[str, tuple[str, tuple[str, ...]]] | None:
    """Lock hinted target nodes, then parents; callers pin and refresh operations.

    ``nowait`` is for background writers: they must never queue for a node an
    admission is trying to take (a queued writer refuses every NOWAIT
    admission behind it), so a busy node raises ``AdmissionLockBusy`` and the
    caller skips it until the next pass.
    """
    rows = (
        session.execute(
            select(
                StoredOperation.id,
                StoredOperation.parent_job_id,
                StoredOperation.node_id,
                Job,
            )
            .join(Job, Job.id == StoredOperation.parent_job_id)
            .where(StoredOperation.id.in_(operation_ids))
        ).all()
        if operation_ids
        else []
    )
    if len(rows) != len(operation_ids):
        return None
    scopes = {}
    for operation_id, parent_id, operation_node, parent_hint in rows:
        scope = cls._target_scope(column_value(parent_hint, "targets"))
        if scope is None or node_id not in scope or operation_node != node_id:
            return None
        scopes[operation_id] = (parent_id, scope)
    if not cls._lock_target_scopes(session, scopes, node_id, nowait=nowait):
        return None
    return scopes


def _lock_target_scopes(
    cls: type[AgentJobService],
    session: Session,
    scopes: dict[str, tuple[str, tuple[str, ...]]],
    node_id: str,
    *,
    nowait: bool = False,
) -> bool:
    nodes = sorted(
        {node_id} | {target for _, scope in scopes.values() for target in scope}
    )
    locked_rows = None
    if nowait:
        locked_rows = lock_admission_rows(
            session,
            (
                AdmissionRowLock(
                    "target-agent-nodes",
                    AgentNode,
                    select(AgentNode).where(AgentNode.node_id.in_(nodes)),
                ),
                AdmissionRowLock(
                    "target-parent-jobs",
                    Job,
                    select(Job).where(
                        Job.id.in_(
                            sorted({parent_id for parent_id, _ in scopes.values()})
                        )
                    ),
                ),
            ),
        )
        locked = list(locked_rows.get("target-agent-nodes", ()))
    else:
        locked = list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(nodes))
                .order_by(AgentNode.node_id)
                .with_for_update(of=AgentNode)
                .execution_options(populate_existing=True)
            )
        )
    if [node.node_id for node in locked] != nodes:
        return False
    parent_ids = sorted({parent_id for parent_id, _ in scopes.values()})
    parents = (
        {
            job.id: job
            for job in (
                locked_rows.get("target-parent-jobs", ())
                if locked_rows is not None
                else session.scalars(
                    select(Job)
                    .where(Job.id.in_(parent_ids))
                    .order_by(Job.id)
                    .with_for_update(of=Job)
                    .execution_options(populate_existing=True)
                )
            )
        }
        if parent_ids
        else {}
    )
    return not any(
        parent_id not in parents
        or cls._target_scope(column_value(parents[parent_id], "targets")) != scope
        for parent_id, scope in scopes.values()
    )
