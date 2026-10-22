"""Real PostgreSQL ownership, concurrent observation and interrupted adoption."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, inspect, select, text
from vonk_agent_protocol import AgentOperation as OperationKind
from vonk_agent_protocol import ControllerErrorCode, LifecycleState
from vonk_control.db import initialize_database, node_facts
from vonk_control.evidence_repeats import (
    command_events,
    command_evidence_needed,
    load_subject,
    note_repeat,
    note_success,
    owner_evidence,
    repeating_evidence,
)
from vonk_control.fleet_profile_contract import (
    FleetProfileAssignmentInput,
    FleetProfileInput,
)
from vonk_control.models import AgentNode, AgentOperation, FleetProfileApplication
from vonk_control.repeated_evidence_contract import RepeatedEvidenceSubject

from .lifecycle_simulation.world import SOLO, build_world
from .test_agent_api import NODE_A, STOP_PAYLOAD, make_agent_system, parent


@pytest.mark.parametrize("orphan", [False, True])
def test_repeat_adoption_restarts_and_preserves_classified_owners(
    tmp_path, postgres_engine, monkeypatch, orphan
):
    """Catches putting command/profile summaries on contact or losing a copy on restart."""
    world = build_world(tmp_path, postgres_engine)
    try:
        profiles = world.controller.profiles
        profile = profiles.create(
            FleetProfileInput(
                name="Evidence owner",
                assignments=[
                    FleetProfileAssignmentInput(
                        recipe_selector=f"vonk-forge/{world.recipes[SOLO]}",
                        spark_ids=[world.nodes[0]],
                        assignment_name="owner",
                    ),
                ],
            ),
            actor="admin",
        )
        application = profiles.apply(
            profile.id,
            request_key="00000000-0000-4000-8000-000000000789",
            actor="admin",
        )
        with world.sessions.begin() as session:
            operation = session.scalar(
                select(AgentOperation)
                .where(AgentOperation.node_id == world.nodes[0])
                .order_by(AgentOperation.id)
            )
            assert operation is not None
            operation.state = LifecycleState.FAILED
            operation_id, operation_kind = operation.id, operation.kind
        subjects = [
            RepeatedEvidenceSubject.AGENT_RESULT,
            operation_kind,
            load_subject(application.id),
        ]
        if orphan:
            subjects.append("detached legacy observation")
        with postgres_engine.begin() as connection:
            for table, column in (
                ("agent_nodes", "contact_repeated_evidence"),
                ("agent_operations", "repeated_evidence"),
                ("fleet_profile_applications", "repeated_evidence"),
            ):
                connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
            connection.exec_driver_sql("""CREATE TABLE evidence_repeats (
                node_id varchar(64), subject varchar(128), code varchar(128), repeats integer,
                first_at timestamptz, last_at timestamptz, detail varchar(256), PRIMARY KEY(node_id,subject,code))""")
            for subject in subjects:
                connection.execute(
                    text(
                        "INSERT INTO evidence_repeats VALUES (:node,:subject,:code,7,:at,:at,NULL)"
                    ),
                    {
                        "node": world.nodes[0],
                        "subject": subject,
                        "code": ControllerErrorCode.INVALID_REQUEST.value,
                        "at": world.clock(),
                    },
                )
        original = node_facts.move_repeated_evidence

        def interrupted(connection):
            copied_command = False

            def stop(_conn, _cursor, statement, _parameters, _context, _many):
                nonlocal copied_command
                if statement.startswith(
                    "UPDATE agent_operations SET repeated_evidence"
                ):
                    copied_command = True
                if copied_command and statement.startswith(
                    "DELETE FROM evidence_repeats WHERE"
                ):
                    raise InterruptedError("Controller stopped after owner copy")

            event.listen(connection, "before_cursor_execute", stop)
            try:
                original(connection)
            finally:
                event.remove(connection, "before_cursor_execute", stop)

        url = postgres_engine.url.render_as_string(hide_password=False)
        with monkeypatch.context() as fault:
            fault.setattr(node_facts, "move_repeated_evidence", interrupted)
            with pytest.raises(InterruptedError):
                initialize_database(url)
        with postgres_engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM evidence_repeats"
            ).scalar_one() == len(subjects)
        initialize_database(url)
        initialize_database(url)
        with world.sessions() as session:
            node = session.get(AgentNode, world.nodes[0])
            operation = session.get(AgentOperation, operation_id)
            application_owner = session.get(FleetProfileApplication, application.id)
            assert (
                node is not None
                and operation is not None
                and application_owner is not None
            )
            assert [
                (item.subject, item.repeats) for item in owner_evidence(node) or []
            ] == [(RepeatedEvidenceSubject.AGENT_RESULT, 7)]
            assert [
                (item.subject, item.repeats) for item in owner_evidence(operation) or []
            ] == [(operation_kind, 7)]
            assert [
                (item.subject, item.repeats)
                for item in owner_evidence(application_owner) or []
            ] == [(load_subject(application.id), 7)]
        with postgres_engine.connect() as connection:
            assert (
                "evidence_repeats" in inspect(connection).get_table_names()
            ) is orphan
            if orphan:
                assert (
                    connection.exec_driver_sql(
                        "SELECT subject FROM evidence_repeats"
                    ).scalar_one()
                    == subjects[-1]
                )
    finally:
        world.close()


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
        node = session.get(AgentNode, NODE_A)
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
        assert len({item.ordinal for _, item in events}) == 6
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
