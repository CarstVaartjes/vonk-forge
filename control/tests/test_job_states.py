"""The generic job table speaks the core vocabulary through one module."""

from __future__ import annotations

import pytest
from vonk_agent_protocol import LifecycleState
from vonk_control import job_states


def test_a_selection_names_only_the_core_word_of_a_state() -> None:
    for state in LifecycleState:
        assert job_states.words(state) == (state.value,)
        assert job_states.attempt_words(state) == (state.value,)


@pytest.mark.parametrize(
    "word", ["waiting-for-operator", "cancelling", "waiting", "partial", "expired"]
)
def test_a_retired_word_is_unknown_to_the_job_and_its_attempt(word: str) -> None:
    assert job_states.core(word) is None
    assert not job_states.means(word, *LifecycleState)
    assert not job_states.attempt_means(word, *LifecycleState)
