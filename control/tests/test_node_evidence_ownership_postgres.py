"""Real PostgreSQL ownership, concurrent observation and interrupted adoption."""

from concurrent.futures import ThreadPoolExecutor

from vonk_agent_protocol import AgentOperation as OperationKind
from vonk_agent_protocol import ControllerErrorCode
from vonk_control.evidence_repeats import (
    command_events,
    command_evidence_needed,
    note_repeat,
    note_success,
    owner_evidence,
    repeating_evidence,
)
from vonk_control.models import (
    AgentNodeContact,
    AgentOperation,
)
from vonk_control.repeated_evidence_contract import RepeatedEvidenceSubject

from .agent_api_support import NODE_A, STOP_PAYLOAD, make_agent_system, parent


def test_concurrent_contact_notes_do_not_lose_counts(tmp_path, postgres_engine):
    """Catches racing read/modify/write of the contact JSON item."""
    _client, services, _codec, clock = make_agent_system(
        tmp_path, engine=postgres_engine
    )

    def observe(_worker):
        for _ in range(8):
            with services.sessions.begin() as session:
                note_repeat(
                    session,
                    RepeatedEvidenceSubject.AGENT_RESULT,
                    NODE_A,
                    ControllerErrorCode.INVALID_REQUEST,
                    now=clock.now,
                )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(observe, range(2)))
    with services.sessions() as session:
        node = session.get(AgentNodeContact, NODE_A)
        assert node is not None
        assert [item.repeats for item in owner_evidence(node) or []] == [16]


def test_command_order_uses_database_identity_and_retains_clear_until_history_prunes(
    tmp_path, postgres_engine
):
    """Catches timestamp ties, lost concurrent events and resurrected warnings after GC."""
    _client, services, _codec, clock = make_agent_system(
        tmp_path, engine=postgres_engine
    )
    operations = [
        services.operations.enqueue(
            parent(services.sessions, clock).id,
            NODE_A,
            OperationKind.RECIPE_STOP,
            "a" * 64,
            STOP_PAYLOAD,
        )
        for _ in range(2)
    ]

    def observe(operation):
        with services.sessions.begin() as session:
            for _ in range(3):
                note_repeat(
                    session,
                    operation.kind,
                    NODE_A,
                    ControllerErrorCode.INVALID_REQUEST,
                    now=clock.now,
                    operation=operation,
                )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(observe, operations))
    with services.sessions() as session:
        events, unreadable = command_events(session, [NODE_A])
        assert not unreadable
        assert len({item.observation_sequence for _, item in events}) == 6
        assert any(
            "x6" in detail
            for detail in repeating_evidence(session, [NODE_A], clock.now)[NODE_A]
        )
    with services.sessions.begin() as session:
        note_success(
            session, operations[1].kind, NODE_A, operation=operations[1], now=clock.now
        )
    with services.sessions() as session:
        assert repeating_evidence(session, [NODE_A], clock.now) == {}
        first = session.get(AgentOperation, operations[0].id)
        second = session.get(AgentOperation, operations[1].id)
        assert first is not None and second is not None
        assert not command_evidence_needed(session, first)
        assert command_evidence_needed(session, second)
    with services.sessions.begin() as session:
        first = session.get(AgentOperation, operations[0].id)
        assert first is not None
        session.delete(first)
    with services.sessions() as session:
        second = session.get(AgentOperation, operations[1].id)
        assert second is not None
        assert not command_evidence_needed(session, second)
        assert repeating_evidence(session, [NODE_A], clock.now) == {}
