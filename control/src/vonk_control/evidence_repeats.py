"""Repeated contact observations and operation history, never retry authority."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as OperationKind
from vonk_agent_protocol import OutcomeUnknown, WaitReason
from vonk_agent_protocol.contracts import AgentFailureResult, AgentUpgradePayload

from . import agent_operation_states
from .models import (
    REPEATED_EVIDENCE_SEQUENCE,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    FleetProfileApplication,
)
from .recovery_policy import RECOVERY
from .repeated_evidence_contract import (
    RepeatedEvidence,
    RepeatedEvidenceSubject,
    is_contact_subject,
)
from .settings import PROFILE_STALL_WINDOW_SECONDS
from .stored_json import read_row_column

_LOGGER = logging.getLogger(__name__)
_SHOWN_FOR = timedelta(seconds=PROFILE_STALL_WINDOW_SECONDS)
_LOAD = RepeatedEvidenceSubject.PROFILE_LOAD.value.format(application_id="")
type EvidenceOwner = AgentNode | AgentOperation | FleetProfileApplication


def load_subject(application_id: str) -> str:
    return RepeatedEvidenceSubject.PROFILE_LOAD.value.format(
        application_id=application_id
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def owner_evidence(row: EvidenceOwner) -> list[RepeatedEvidence] | None:
    value = (
        row.contact_repeated_evidence
        if isinstance(row, AgentNode)
        else row.repeated_evidence
    )
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, RepeatedEvidence) for item in value
    ):
        _LOGGER.warning(
            "Repeated evidence on its owner is unreadable: %s", type(row).__name__
        )
        return None
    if isinstance(row, AgentNode):
        bound = all(
            item.node_id == row.node_id
            and is_contact_subject(item.subject)
            and item.code is not None
            and item.observation_sequence == 0
            for item in value
        )
    elif isinstance(row, AgentOperation):
        bound = all(
            item.node_id == row.node_id
            and item.subject == row.kind
            and item.observation_sequence > 0
            for item in value
        )
    else:
        bound = all(
            item.subject == load_subject(row.id)
            and item.code is not None
            and item.observation_sequence == 0
            for item in value
        )
    if not bound:
        _LOGGER.warning(
            "Repeated evidence does not bind its %s owner", type(row).__name__
        )
        return None
    return value


def _write(row: EvidenceOwner, evidence: list[RepeatedEvidence]) -> None:
    if isinstance(row, AgentNode):
        row.contact_repeated_evidence = evidence or None
    else:
        row.repeated_evidence = evidence or None


def _owner(
    session: Session, subject: str, node_id: str, operation: AgentOperation | None
) -> EvidenceOwner | None:
    if is_contact_subject(subject):
        return session.scalar(
            select(AgentNode)
            .where(AgentNode.node_id == node_id)
            .with_for_update(of=AgentNode)
        )
    if subject.startswith(_LOAD):
        return session.get(
            FleetProfileApplication, subject[len(_LOAD) :], with_for_update=True
        )
    if operation is None or operation.kind != subject or operation.node_id != node_id:
        raise ValueError("command repetition requires its exact operation owner")
    return session.scalar(
        select(AgentOperation)
        .where(AgentOperation.id == operation.id)
        .with_for_update(of=AgentOperation)
    )


def next_sequence(session: Session) -> int:
    if session.get_bind().dialect.name == "postgresql":
        value = session.scalar(REPEATED_EVIDENCE_SEQUENCE.next_value())
        assert value is not None
        return value
    # SQLite is a test store. The caller flushes its owner before reaching
    # here, so its existing write transaction serializes this allocation.
    values = session.scalars(select(AgentOperation.repeated_evidence))
    return (
        max(
            (
                item.observation_sequence
                for value in values
                if isinstance(value, list)
                for item in value
            ),
            default=0,
        )
        + 1
    )


def note_repeat(
    session: Session,
    subject: str,
    node_id: str,
    code: str,
    *,
    now: datetime,
    detail: str | None = None,
    operation: AgentOperation | None = None,
) -> None:
    """Retain an observation on its owner, with database-ordered command history."""
    session.flush()
    try:
        with session.begin_nested():
            row = _owner(session, subject, node_id, operation)
            if row is None:
                return
            evidence = owner_evidence(row)
            if evidence is None:
                return
            at = _utc(now)
            event = RepeatedEvidence(
                node_id=node_id,
                subject=subject,
                code=str(code),
                repeats=1,
                first_at=at,
                last_at=at,
                detail=None if detail is None else detail[:256],
            )
            if isinstance(row, AgentOperation):
                event.observation_sequence = next_sequence(session)
                evidence = [*evidence, event]
            else:
                previous = next(
                    (
                        item
                        for item in evidence
                        if (item.node_id, item.subject, item.code)
                        == (node_id, subject, code)
                    ),
                    None,
                )
                if previous is not None:
                    event.repeats = previous.repeats + 1
                    event.first_at = previous.first_at
                evidence = [
                    item
                    for item in evidence
                    if (item.node_id, item.subject, item.code)
                    != (node_id, subject, code)
                ] + [event]
            _write(row, evidence)
            session.flush()
    except SQLAlchemyError as error:
        _LOGGER.warning("repeated evidence was not recorded: %s", type(error).__name__)


def note_success(
    session: Session,
    subject: str,
    node_id: str,
    *,
    operation: AgentOperation | None = None,
    now: datetime | None = None,
) -> None:
    session.flush()
    try:
        with session.begin_nested():
            row = _owner(session, subject, node_id, operation)
            if row is None:
                return
            evidence = owner_evidence(row)
            if evidence is None:
                return
            if isinstance(row, AgentOperation):
                if evidence and evidence[-1].code is None:
                    return
                at = _utc(row.updated_at if now is None else now)
                evidence = [
                    *evidence,
                    RepeatedEvidence(
                        node_id=node_id,
                        subject=subject,
                        repeats=0,
                        first_at=at,
                        last_at=at,
                        observation_sequence=next_sequence(session),
                    ),
                ]
            else:
                evidence = [
                    item
                    for item in evidence
                    if (item.node_id, item.subject) != (node_id, subject)
                ]
            _write(row, evidence)
            session.flush()
    except SQLAlchemyError as error:
        _LOGGER.warning("repeated evidence was not cleared: %s", type(error).__name__)


def command_events(
    session: Session, node_ids: Sequence[str], *, subject: str | None = None
) -> tuple[list[tuple[str, RepeatedEvidence]], set[tuple[str, str]]]:
    result: list[tuple[str, RepeatedEvidence]] = []
    unreadable: set[tuple[str, str]] = set()
    rows = session.scalars(
        select(AgentOperation).where(
            AgentOperation.node_id.in_(node_ids),
            AgentOperation.repeated_evidence.is_not(None),
            *([AgentOperation.kind == subject] if subject is not None else []),
        )
    )
    for row in rows:
        evidence = owner_evidence(row)
        if evidence is None:
            unreadable.add((row.node_id, row.kind))
        else:
            result.extend((row.id, event) for event in evidence)
    return [
        item for item in result if (item[1].node_id, item[1].subject) not in unreadable
    ], unreadable


def command_evidence_needed(session: Session, operation: AgentOperation) -> bool:
    """Retain unresolved observations, and a clear until older failures are pruned."""
    own = owner_evidence(operation)
    if own is None:
        return True
    if not own:
        return False
    events, unreadable = command_events(
        session, (operation.node_id,), subject=operation.kind
    )
    if (operation.node_id, operation.kind) in unreadable:
        return True
    cleared = max(
        (event.observation_sequence for _, event in events if event.code is None),
        default=-1,
    )
    if any(
        event.code is not None and event.observation_sequence > cleared for event in own
    ):
        return True
    return any(
        event.code is None and event.observation_sequence == cleared for event in own
    ) and any(
        identity != operation.id
        and event.code is not None
        and event.observation_sequence < cleared
        for identity, event in events
    )


def repeating_evidence(
    session: Session, node_ids: Sequence[str], now: datetime
) -> dict[str, tuple[str, ...]]:
    """Project summaries from contact and their exact operation owners; never mutate."""
    summaries: list[RepeatedEvidence] = []
    for node in session.scalars(
        select(AgentNode).where(AgentNode.node_id.in_(node_ids))
    ):
        summaries.extend(owner_evidence(node) or [])
    for application in session.scalars(
        select(FleetProfileApplication).where(
            FleetProfileApplication.repeated_evidence.is_not(None)
        )
    ):
        summaries.extend(
            item
            for item in owner_evidence(application) or []
            if item.node_id in node_ids and _utc(item.last_at) >= _utc(now) - _SHOWN_FOR
        )
    events, unreadable = command_events(session, node_ids)
    cleared: dict[tuple[str, str], int] = {}
    for _, event in events:
        if event.code is None:
            key = (event.node_id, event.subject)
            cleared[key] = max(cleared.get(key, -1), event.observation_sequence)
    grouped: dict[tuple[str, str, str], RepeatedEvidence] = {}
    for _, event in events:
        if event.code is None or event.observation_sequence <= cleared.get(
            (event.node_id, event.subject), -1
        ):
            continue
        key = (event.node_id, event.subject, event.code)
        previous = grouped.get(key)
        if previous is None:
            grouped[key] = event.model_copy()
        else:
            previous.repeats += event.repeats
            previous.first_at = min(_utc(previous.first_at), _utc(event.first_at))
            previous.last_at = max(_utc(previous.last_at), _utc(event.last_at))
            if event.observation_sequence > previous.observation_sequence:
                previous.detail, previous.observation_sequence = (
                    event.detail,
                    event.observation_sequence,
                )
    summaries.extend(grouped.values())
    shown = _upgrade_identity_waits(session, node_ids)
    for node_id, subject in sorted(unreadable):
        shown.setdefault(node_id, []).append(
            f"Repeated evidence for {subject} is unreadable; its current repetition is unknown."
        )
    for item in sorted(
        summaries, key=lambda item: (item.node_id, item.subject, item.code or "")
    ):
        if item.code is None or not RECOVERY.exhausted(item.repeats):
            continue
        if (
            item.subject == OperationKind.AGENT_UPGRADE
            and item.code == WaitReason.UPGRADE_AWAITING_IDENTITY
        ):
            continue
        detail = (
            f"{item.code} x{item.repeats} on {item.subject} since "
            f"{_utc(item.first_at):%Y-%m-%dT%H:%MZ}, last {_utc(item.last_at):%Y-%m-%dT%H:%MZ}"
            + ("" if item.detail is None else f": {item.detail}")
        )
        shown.setdefault(item.node_id, []).append(detail[:256])
    return {node_id: tuple(details) for node_id, details in shown.items()}


def _upgrade_identity_waits(
    session: Session, node_ids: Sequence[str]
) -> dict[str, list[str]]:
    """Show each current, bounded upgrade handoff, even before it repeats.

    Historical attempt evidence stays available on the operation; it cannot
    keep a resolved wait alive on the node after identity reconciliation.
    """
    shown: dict[str, list[str]] = {}
    statement = (
        select(AgentOperation, AgentOperationAttempt)
        .join(
            AgentOperationAttempt,
            (AgentOperationAttempt.operation_id == AgentOperation.id)
            & (AgentOperationAttempt.attempt == AgentOperation.current_attempt),
        )
        .where(
            AgentOperation.node_id.in_(node_ids),
            AgentOperation.kind == OperationKind.AGENT_UPGRADE,
            AgentOperation.state.in_(agent_operation_states.LIVE),
        )
        .order_by(AgentOperation.created_at, AgentOperation.id)
    )
    try:
        rows = session.execute(statement).all()
    except SQLAlchemyError:
        return {}
    for operation, attempt in rows:
        result = read_row_column(attempt, "result")
        if (
            not isinstance(result, AgentFailureResult | OutcomeUnknown)
            or result.wait_reason != WaitReason.UPGRADE_AWAITING_IDENTITY
        ):
            continue
        payload = read_row_column(operation, "payload")
        if not isinstance(payload, AgentUpgradePayload):
            continue
        deadline = (
            _utc(operation.recovery_deadline or attempt.lease_deadline)
            .isoformat()
            .replace("+00:00", "Z")
        )
        shown.setdefault(operation.node_id, []).append(
            f"{WaitReason.UPGRADE_AWAITING_IDENTITY} op={operation.id} "
            f"target={payload.target_build_digest}/{payload.target_binary_digest} deadline={deadline}"
        )
    return shown
