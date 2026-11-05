"""The rendezvous and the insert collision fail with the real cause, never a hang."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from .insert_collision_support import Rendezvous, collide_inserts


def test_a_second_wait_on_one_thread_fails_at_once() -> None:
    rendezvous = Rendezvous(1, timeout=None)
    rendezvous.wait()

    # A raw barrier would hold this thread until its timeout broke it.
    with pytest.raises(AssertionError, match="already passed the rendezvous"):
        rendezvous.wait()


def test_a_call_that_never_inserts_raises_its_own_error(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'items.db'}", paramstyle="named")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE items (key TEXT PRIMARY KEY)"))

    def inserts() -> str:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO items (key) VALUES (:key)"), {"key": "k"}
            )
        return "inserted"

    def fails_before_its_insert() -> str:
        raise ValueError("the admission refused before writing")

    # Without a timeout only the abort can release the call held at its
    # insert, so a missing abort hangs (and pytest-timeout ends it).
    with pytest.raises(ValueError, match="refused before writing"):
        collide_inserts(
            engine,
            "items",
            "key",
            "k",
            (inserts, fails_before_its_insert),
            timeout=None,
        )
    engine.dispose()
