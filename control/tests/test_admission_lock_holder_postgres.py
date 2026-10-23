"""A busy admission lock names the work that holds it."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    lock_admission_rows,
    node_admission_key,
)
from vonk_control.models import AgentNode, Base

NODE = "spk_" + "a" * 32


def test_busy_node_key_releases_for_fresh_admission(postgres_engine) -> None:
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as holder:
        acquire_admission_keys(
            holder, (node_admission_key(NODE),), holder="recipe-operation"
        )
        pid = holder.scalar(text("SELECT pg_backend_pid()"))
        with sessions.begin() as taker, pytest.raises(AdmissionLockBusy) as ending:
            acquire_admission_keys(
                taker, (node_admission_key(NODE),), holder="profile-admission"
            )
        assert ending.value.holder == f"pid {pid} recipe-operation"
    with sessions.begin() as fresh:
        acquire_admission_keys(fresh, (node_admission_key(NODE),))


def test_unlabelled_busy_node_key_clears_for_a_fresh_admission(
    postgres_engine,
) -> None:
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as holder:
        # A pooled connection can retain an application name from earlier work.
        # Establish the unlabelled boundary explicitly for this transaction.
        holder.execute(text("SELECT set_config('application_name', '', true)"))
        acquire_admission_keys(holder, (node_admission_key(NODE),))
        with sessions.begin() as taker, pytest.raises(Exception) as _ending:
            acquire_admission_keys(taker, (node_admission_key(NODE),))
    with sessions.begin() as fresh:
        acquire_admission_keys(fresh, (node_admission_key(NODE),))


def test_the_label_ends_with_the_transaction(postgres_engine) -> None:
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as first:
        acquire_admission_keys(
            first, (node_admission_key(NODE),), holder="recipe-operation"
        )
    with sessions.begin() as second:
        acquire_admission_keys(
            second, (node_admission_key(NODE),), holder="profile-admission"
        )


def test_nowait_does_not_blame_an_idle_reader_for_another_writers_lock(
    postgres_engine,
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as seed:
        seed.add(AgentNode(node_id=NODE, state=NodeIdentityState.ACTIVE))
    with sessions.begin() as reader, sessions.begin() as writer:
        reader.execute(text("SET LOCAL application_name = 'innocent-reader'"))
        reader.scalars(select(AgentNode)).all()
        writer.scalars(select(AgentNode).with_for_update()).all()
        # pg_stat_activity only shows the last statement, not this prior lock.
        writer.execute(text("SELECT 1"))
        with sessions.begin() as taker, pytest.raises(AdmissionLockBusy) as ending:
            lock_admission_rows(
                taker,
                (AdmissionRowLock("nodes", AgentNode, select(AgentNode)),),
            )
        assert ending.value.sqlstate == "55P03"
        assert "innocent-reader" not in str(ending.value)
        assert "blocking pid unavailable" in str(ending.value)
    with sessions.begin() as fresh:
        lock_admission_rows(
            fresh, (AdmissionRowLock("nodes", AgentNode, select(AgentNode)),)
        )
