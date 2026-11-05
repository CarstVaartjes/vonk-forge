"""The range spelling of "state not ended" selects what NOT IN selects."""

from __future__ import annotations

import pytest
from sqlalchemy import column, create_engine, select, table
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleSubject, stored_words
from vonk_control.catalog_revision_collection import (
    TERMINAL_LIFECYCLE_STATES,
    not_among,
)


@pytest.mark.parametrize("subject", list(LifecycleSubject))
def test_the_range_spelling_equals_not_in_for_the_whole_vocabulary(subject) -> None:
    """Catches a range that drops a word, or an unknown word counted as ended."""
    ended = stored_words(subject, TERMINAL_LIFECYCLE_STATES)
    vocabulary = stored_words(subject, LifecycleState)
    unknown = ["", "a", "zzz", "Ended", "ended!", "~"]
    states = sorted({*vocabulary, *unknown})
    engine = create_engine("sqlite://")
    words = table("words", column("state"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE words (state TEXT)")
        connection.exec_driver_sql("INSERT INTO words VALUES (NULL)")
        for state in states:
            connection.exec_driver_sql("INSERT INTO words VALUES (?)", (state,))
        by_ranges = set(
            connection.execute(
                select(words.c.state).where(not_among(words.c.state, ended, "sqlite"))
            ).scalars()
        )
        by_not_in = set(
            connection.execute(
                select(words.c.state).where(words.c.state.not_in(ended))
            ).scalars()
        )
    assert by_ranges == by_not_in
    assert set(unknown) <= by_ranges
    assert not set(ended) & by_ranges
