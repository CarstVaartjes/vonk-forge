"""The lifecycle tables admit only the core state words, and startup never refuses.

A stored word outside ``LifecycleState`` cannot be written to a job, an order or
their attempts: the CHECK refuses it.  A database that already holds such a row
(written before the constraint existed) still starts: the constraint is added
``NOT VALID``, the row is kept untouched, and every later write of a retired
word is refused.  Starting again changes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from vonk_control.db import reconcile_schema
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
    JobAttempt,
)

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
NODE = "spk_" + "5" * 32
#: A word an older build stored and nothing writes now.
RETIRED = "waiting-for-operator"
#: Each lifecycle table and its state CHECK.
CHECKS = {
    "jobs": "ck_jobs_state",
    "job_attempts": "ck_job_attempts_state",
    "agent_operations": "ck_agent_operations_state",
    "agent_operation_attempts": "ck_agent_operation_attempts_state",
}


def _seed(postgres_engine) -> None:
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE, state="active"))
        session.add(
            AgentCertificate(
                serial="serial-state-check",
                node_id=NODE,
                not_before=NOW - timedelta(hours=1),
                not_after=NOW + timedelta(hours=1),
                fingerprint="fingerprint-state-check",
                state="active",
                generation=1,
            )
        )
        job = Job(
            request_id="00000000-0000-4000-8000-0000000000a1",
            kind="probe",
            state="running",
            actor="test",
            authority_revision="a" * 64,
            targets=[NODE],
            payload_digest="b" * 64,
            payload={},
            current_attempt=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(job)
        session.flush()
        session.add(
            JobAttempt(
                job_id=job.id,
                attempt=1,
                fence="00000000-0000-4000-8000-0000000000a2",
                worker_id="worker",
                lease_deadline=NOW,
                state="running",
            )
        )
        operation = AgentOperation(
            parent_job_id=job.id,
            node_id=NODE,
            kind="recipe.stop",
            payload_digest="c" * 64,
            payload={},
            authority_revision="a" * 64,
            state="running",
            current_attempt=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(operation)
        session.flush()
        session.add(
            AgentOperationAttempt(
                operation_id=operation.id,
                attempt=1,
                fence="00000000-0000-4000-8000-0000000000a3",
                lease_deadline=NOW,
                agent_certificate_serial="serial-state-check",
                state="running",
            )
        )


def _constraint(connection, name: str) -> tuple[int, bool]:
    oid, validated = connection.execute(
        text("SELECT oid, convalidated FROM pg_constraint WHERE conname = :name"),
        {"name": name},
    ).one()
    return int(oid), bool(validated)


def _set_state(connection, table: str, word: str) -> None:
    connection.execute(text(f"UPDATE {table} SET state = :word"), {"word": word})


def test_a_retired_word_is_refused_by_every_lifecycle_table(postgres_engine):
    with postgres_engine.begin() as connection:
        Base.metadata.create_all(connection)
    _seed(postgres_engine)
    with postgres_engine.begin() as connection:
        for table in CHECKS:
            for word in (RETIRED, "waiting", "partial", "compensated"):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    _set_state(connection, table, word)
            _set_state(connection, table, "observing")  # a core word is written


@pytest.mark.parametrize("retired_row", [False, True], ids=["clean", "retired-row"])
def test_startup_adds_the_state_checks_and_never_refuses(
    postgres_engine, retired_row: bool
):
    with postgres_engine.begin() as connection:
        Base.metadata.create_all(connection)
    _seed(postgres_engine)
    # The database as the previous release left it: no state CHECK on these
    # tables, and (in one case) a row with a word nothing writes any more.
    with postgres_engine.begin() as connection:
        for table, name in CHECKS.items():
            connection.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {name}"))
            if retired_row:
                _set_state(connection, table, RETIRED)

    with postgres_engine.begin() as connection:
        reconcile_schema(connection)  # startup does not refuse either database

    with postgres_engine.begin() as connection:
        first = {name: _constraint(connection, name) for name in CHECKS.values()}
        for table, name in CHECKS.items():
            assert first[name][1] is not retired_row, table
            stored = connection.execute(text(f"SELECT state FROM {table}")).scalars()
            assert set(stored) == {RETIRED if retired_row else "running"}
            with pytest.raises(IntegrityError), connection.begin_nested():
                _set_state(connection, table, "partial")

    # A second startup is a no-op: the same constraints, nothing rewritten.
    with postgres_engine.begin() as connection:
        reconcile_schema(connection)
    with postgres_engine.begin() as connection:
        assert {name: _constraint(connection, name) for name in CHECKS.values()} == (
            first
        )
