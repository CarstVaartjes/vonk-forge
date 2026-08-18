"""Authenticated Fleet update and revocation actions.

The service is deliberately control-plane only: agents receive protocol operations
through the existing durable Job/AgentOperation queue; no browser SSH or direct
host command execution is permitted.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from vonk_agent_protocol import AgentOperation, canonical_message
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .audit import AuditRecord
from .auth import Actor, require_capability
from .enrollment import EnrollmentDenied, EnrollmentService, RemoteRevocationUncertain
from .jobs import JobService
from .models import AgentNode, Job


class FleetActionError(ValueError):
    """A requested Fleet action cannot be accepted."""


class UpdatePreviewSigner(Protocol):
    def preview(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class SignedUpdatePreview:
    node_id: str
    payload: dict[str, object]
    payload_digest: str
    signature: dict[str, object]

    @property
    def exact(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "signature": self.signature,
        }


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
    signer: UpdatePreviewSigner | None = None


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


class FleetActionService:
    """Create durable, capability-checked actions and agent-directed commands."""

    def __init__(self, services: FleetActionServices) -> None:
        self._services = services

    def preview_update(
        self,
        actor: Actor,
        node_id: str,
        payload: Mapping[str, object],
    ) -> SignedUpdatePreview:
        require_capability(actor, "fleet:update")
        self._active_node(node_id)
        exact_payload = json.loads(canonical_message(dict(payload)))
        if not isinstance(exact_payload, dict):
            raise FleetActionError("update payload must be an object")
        signed = (
            self._services.signer.preview(exact_payload)
            if self._services.signer is not None
            else {"algorithm": "control-preview", "value": _digest(exact_payload)}
        )
        signature = dict(signed)
        return SignedUpdatePreview(
            node_id=node_id,
            payload=exact_payload,
            payload_digest=_digest(exact_payload),
            signature=signature,
        )

    def update(
        self,
        actor: Actor,
        request_id: str,
        base_commit: str,
        node_ids: Sequence[str],
        preview: SignedUpdatePreview,
    ) -> FleetAction:
        require_capability(actor, "fleet:update")
        if not node_ids or tuple(node_ids) != (preview.node_id,):
            raise FleetActionError("update targets must match the exact preview")
        if _digest(preview.payload) != preview.payload_digest:
            raise FleetActionError("update preview digest is not exact")
        for node_id in node_ids:
            self._active_node(node_id)
        job = self._services.jobs.enqueue(
            "fleet.update", actor.subject, base_commit, node_ids,
            {"preview": preview.exact, "operation": AgentOperation.AGENT_UPDATE.value},
            request_id=request_id,
        )
        action = FleetAction(str(job.id), "update", "queued", tuple(node_ids), dict(job.payload))
        self._audit(request_id, actor, "fleet.update", base_commit, tuple(node_ids))
        return action

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
        if job.kind not in {"fleet.update", "fleet.revoke"}:
            raise FleetActionError("Fleet action not found")
        rollback = job.result.get("rollback") if isinstance(job.result, dict) else None
        kind = "update" if job.kind == "fleet.update" else "revoke"
        return FleetAction(str(job.id), kind, job.state, tuple(job.targets), dict(job.payload), job.status_reason, rollback if isinstance(rollback, dict) else None)

    def _active_node(self, node_id: str) -> AgentNode:
        with self._services.sessions() as session:
            node = session.scalar(select(AgentNode).where(AgentNode.node_id == node_id))
            if node is None or node.state != "active" or node.revoked_at is not None:
                raise FleetActionError("Fleet node is not active")
            session.expunge(node)
            return node

    def _audit(self, request_id: str, actor: Actor, action: str, base_commit: str | None, targets: tuple[str, ...]) -> None:
        self._services.audits.append(AuditRecord(request_id, actor.subject, action, base_commit, targets))
