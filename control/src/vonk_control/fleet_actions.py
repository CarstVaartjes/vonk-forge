"""Authenticated Fleet revocation actions.

The service is deliberately control-plane only; no browser SSH or direct host
command execution is permitted.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .audit import AuditRecord
from .auth import Actor, require_capability
from .enrollment import EnrollmentDenied, EnrollmentService, RemoteRevocationUncertain
from .jobs import JobService
from .models import AgentNode, Job


class FleetActionError(ValueError):
    """A requested Fleet action cannot be accepted."""


@dataclass(frozen=True, slots=True)
class FleetAction:
    id: str
    kind: str
    state: str
    node_ids: tuple[str, ...]
    payload: dict[str, object]
    error: str | None = None
    rollback: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class FleetActionServices:
    sessions: sessionmaker[Session]
    jobs: JobService
    enrollment: EnrollmentService
    audits: Any
    clock: Callable[[], datetime]


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class FleetActionService:
    """Create durable, capability-checked actions and agent-directed commands."""

    def __init__(self, services: FleetActionServices) -> None:
        self._services = services

    def revoke(
        self,
        actor: Actor,
        request_id: str,
        node_id: str,
        *,
        confirmed: bool,
    ) -> FleetAction:
        require_capability(actor, "fleet:revoke")
        if confirmed is not True:
            raise FleetActionError("revocation requires explicit confirmation")
        self._active_node(node_id)
        try:
            self._services.enrollment.revoke_node(node_id, actor.subject)
        except RemoteRevocationUncertain as error:
            job = self._services.jobs.enqueue(
                "fleet.revoke", actor.subject, "fleet", (node_id,),
                {"terminal": True, "remote_revocation": "uncertain", "error": str(error)},
                request_id=request_id,
            )
            with self._services.sessions.begin() as session:
                row = session.get(Job, job.id)
                assert row is not None
                row.state = "failed"
                row.status_reason = str(error)
                row.result = {"rollback": {"state": "required", "reason": str(error)}}
                row.updated_at = _now(self._services.clock)
            self._audit(request_id, actor, "fleet.revoke", None, (node_id,))
            return FleetAction(str(job.id), "revoke", "failed", (node_id,), {}, str(error), {"state": "required", "reason": str(error)})
        except (EnrollmentDenied, ValueError) as error:
            raise FleetActionError(str(error)) from error
        job = self._services.jobs.enqueue(
            "fleet.revoke", actor.subject, "fleet", (node_id,), {"terminal": True}, request_id=request_id
        )
        with self._services.sessions.begin() as session:
            row = session.get(Job, job.id)
            assert row is not None
            row.state = "succeeded"
            row.result = {"terminal": True}
            row.updated_at = _now(self._services.clock)
        self._audit(request_id, actor, "fleet.revoke", None, (node_id,))
        return FleetAction(str(job.id), "revoke", "succeeded", (node_id,), {"terminal": True})

    def get(self, action_id: str) -> FleetAction:
        try:
            job = self._services.jobs.get(action_id)
        except KeyError as error:
            raise FleetActionError("Fleet action not found") from error
        if job.kind != "fleet.revoke":
            raise FleetActionError("Fleet action not found")
        rollback = job.result.get("rollback") if isinstance(job.result, dict) else None
        return FleetAction(str(job.id), "revoke", job.state, tuple(job.targets), dict(job.payload), job.status_reason, rollback if isinstance(rollback, dict) else None)

    def _active_node(self, node_id: str) -> AgentNode:
        with self._services.sessions() as session:
            node = session.scalar(select(AgentNode).where(AgentNode.node_id == node_id))
            if node is None or node.state != "active" or node.revoked_at is not None:
                raise FleetActionError("Fleet node is not active")
            session.expunge(node)
            return node

    def _audit(self, request_id: str, actor: Actor, action: str, authority_revision: str | None, targets: tuple[str, ...]) -> None:
        self._services.audits.append(AuditRecord(request_id, actor.subject, action, authority_revision, targets))
