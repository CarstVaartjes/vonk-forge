"""Orchestration for the node-scoped agent queue."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentClaim, AgentOperation, InvalidRequestReason
from vonk_agent_protocol.contracts import canonical_payload

from ..admission_locking import acquire_admission_keys, node_admission_key
from ..categorized_errors import InvalidValue, MissingRecord
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..models import AgentNode, Job
from ..models import AgentOperation as StoredOperation
from .contracts import (
    _CONTROL_OPERATIONS,
    _RECIPE_CAPABILITIES,
    _TERMINAL_PARENT_STATES,
    _WORKLOAD_INTENT_OPERATIONS,
)
from .predicates import _document
from .stored import column_field, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


def enqueue(
    self: AgentJobService,
    parent_job_id: str,
    node_id: str,
    operation: str,
    authority_revision: str,
    payload: object,
) -> StoredOperation:
    with self._sessions.begin() as session:
        stored = self.enqueue_in_session(
            session,
            parent_job_id,
            node_id,
            operation,
            authority_revision,
            payload,
            operation_id=str(uuid.uuid4()),
        )
    self.notify_available()
    return stored


def enqueue_in_session(
    self: AgentJobService,
    session: Session,
    parent_job_id: str,
    node_id: str,
    operation: str,
    authority_revision: str,
    payload: object,
    *,
    operation_id: str,
) -> StoredOperation:
    """Attach a caller-identified operation to the caller's transaction."""
    self._mark_started()
    now = self._clock()
    try:
        protocol_operation = AgentOperation(operation)
    except ValueError as error:
        raise InvalidValue(
            "agent operation is not supported by the control plane",
            reason=InvalidRequestReason.UNSUPPORTED,
        ) from error
    if protocol_operation.value not in _CONTROL_OPERATIONS:
        raise InvalidValue(
            "agent operation is not supported by the control plane",
            reason=InvalidRequestReason.UNSUPPORTED,
        )
    parent_hint = session.get(Job, parent_job_id)
    if parent_hint is None:
        raise MissingRecord(parent_job_id, reason=InvalidRequestReason.NOT_FOUND)
    scope = self._target_scope(column_value(parent_hint, "targets"))
    if scope is None or node_id not in scope:
        raise InvalidValue(
            "agent operation node must be a parent target",
            reason=InvalidRequestReason.CONFLICT,
        )
    uses_workload_admission = protocol_operation.value in _RECIPE_CAPABILITIES
    # A busy admission lock is an unknown outcome the owner of this
    # transaction already translates and retries (``AdmissionLockBusy`` is
    # what every caller catches); it is not translated a second time here.
    if uses_workload_admission:
        acquire_admission_keys(
            session,
            tuple(node_admission_key(target) for target in scope),
            holder="agent-job",
        )
    scopes_locked = self._lock_target_scopes(
        session,
        {"enqueue": (parent_job_id, scope)},
        node_id,
        nowait=uses_workload_admission,
    )
    if not scopes_locked:
        raise InvalidValue(
            "agent operation parent target scope changed",
            reason=InvalidRequestReason.SUPERSEDED,
        )
    node = session.scalar(select(AgentNode).where(AgentNode.node_id == node_id))
    if node is None:
        raise MissingRecord(node_id, reason=InvalidRequestReason.NOT_FOUND)
    if node.state != "active" or node.revoked_at is not None:
        raise InvalidValue(
            "agent operation node must be active",
            reason=InvalidRequestReason.NOT_READY,
        )
    parent = session.scalar(select(Job).where(Job.id == parent_job_id))
    if parent is None:
        raise MissingRecord(parent_job_id, reason=InvalidRequestReason.NOT_FOUND)
    if parent.state in _TERMINAL_PARENT_STATES:
        raise InvalidValue(
            "cannot enqueue an agent operation beneath a terminal parent",
            reason=InvalidRequestReason.NOT_READY,
        )
    if parent.authority_revision != authority_revision:
        raise InvalidValue(
            "agent operation authority revision must match its parent",
            reason=InvalidRequestReason.SUPERSEDED,
        )
    if node_id not in column_value(parent, "targets"):
        raise InvalidValue(
            "agent operation node must be a parent target",
            reason=InvalidRequestReason.CONFLICT,
        )
    workload_intent_ordinal = (
        column_field(parent, "payload", "workload_intent_ordinal")
        if operation in _WORKLOAD_INTENT_OPERATIONS
        else None
    )
    if operation in _WORKLOAD_INTENT_OPERATIONS and workload_intent_ordinal is None:
        raise InvalidValue(
            "workload operation requires a bound intent",
            reason=InvalidRequestReason.INCOMPLETE,
        )
    if workload_intent_ordinal is not None and (
        type(workload_intent_ordinal) is not int
        or workload_intent_ordinal < 1
        or workload_intent_ordinal != node.workload_intent_ordinal
    ):
        raise InvalidValue(
            "agent operation workload intent was superseded",
            reason=InvalidRequestReason.SUPERSEDED,
        )
    reserved_fence = str(uuid.uuid4())
    payload_bytes = canonical_payload(protocol_operation, payload)
    final_payload = json.loads(payload_bytes)
    validated = AgentClaim(
        fence=reserved_fence,
        operation=protocol_operation,
        payload=final_payload,
        deadline=now,
    )
    stored = AgentOperationAdapter.new_order(
        id=operation_id,
        parent_job_id=parent_job_id,
        node_id=node_id,
        kind=protocol_operation.value,
        payload_digest=hashlib.sha256(payload_bytes).hexdigest(),
        payload=_document(validated.payload),
        authority_revision=authority_revision,
        workload_intent_ordinal=workload_intent_ordinal,
        current_attempt=0,
        created_at=now,
        updated_at=now,
    )
    session.add(stored)
    session.flush()
    return stored


def notify_available(self: AgentJobService) -> None:
    """Wake long polls after a caller-managed enqueue transaction commits."""
    with self._available:
        self._available.notify_all()
