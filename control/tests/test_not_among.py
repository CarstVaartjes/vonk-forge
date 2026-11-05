"""The range spelling of "state not ended" selects what NOT IN selects."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import Engine, column, create_engine, select, table, text
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleSubject, stored_words
from vonk_control.catalog_revision_collection import (
    TERMINAL_LIFECYCLE_STATES,
    not_among,
)

UNKNOWN = ["", "a", "zzz", "Ended", "ended!", "~", "x-y", "x_y"]


@pytest.mark.parametrize("subject", list(LifecycleSubject))
def test_ended_words_are_lowercase_letters(subject) -> None:
    """Catches a word whose collation order differs from ``sorted`` (-, _, capitals)."""
    for word in stored_words(subject, TERMINAL_LIFECYCLE_STATES):
        assert re.fullmatch(r"[a-z]+", word), (subject, word)


def _equal(engine: Engine, subject) -> None:
    ended = stored_words(subject, TERMINAL_LIFECYCLE_STATES)
    states = sorted({*stored_words(subject, LifecycleState), *UNKNOWN})
    words = table("words", column("state"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TEMPORARY TABLE words (state TEXT)")
        connection.exec_driver_sql("INSERT INTO words VALUES (NULL)")
        for state in states:
            connection.execute(
                text("INSERT INTO words VALUES (:state)"), {"state": state}
            )
        by_ranges = set(
            connection.execute(
                select(words.c.state).where(not_among(words.c.state, ended))
            ).scalars()
        )
        by_not_in = set(
            connection.execute(
                select(words.c.state).where(words.c.state.not_in(ended))
            ).scalars()
        )
        connection.exec_driver_sql("DROP TABLE words")
    assert by_ranges == by_not_in
    assert set(UNKNOWN) <= by_ranges
    assert not set(ended) & by_ranges


@pytest.mark.parametrize("subject", list(LifecycleSubject))
def test_the_range_spelling_equals_not_in_for_the_whole_vocabulary(subject) -> None:
    """Catches a range that drops a word, or an unknown word counted as ended."""
    _equal(create_engine("sqlite://"), subject)


@pytest.mark.parametrize("subject", list(LifecycleSubject))
def test_the_range_spelling_equals_not_in_on_postgres(
    subject, postgres_engine: Engine
) -> None:
    _equal(postgres_engine, subject)
