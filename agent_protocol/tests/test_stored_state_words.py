"""The stored state vocabulary is the core's, and nothing else."""

from __future__ import annotations

import pytest
from vonk_agent_protocol import (
    TERMINAL_LIFECYCLE_STATES,
    LifecycleState,
    LifecycleSubject,
    adopt_state,
    check_words,
    input_state,
    is_live,
    is_state,
    live_words,
    stored_words,
)

#: Words older builds stored; nothing writes them now.
RETIRED = ("waiting-for-operator", "cancelling", "waiting", "partial", "expired")


def test_the_core_vocabulary_has_nine_states_and_superseded_is_terminal() -> None:
    assert [state.value for state in LifecycleState] == [
        "queued",
        "running",
        "observing",
        "backoff",
        "succeeded",
        "failed",
        "cancelled",
        "superseded",
        "needs-operator",
    ]
    assert LifecycleState.SUPERSEDED in TERMINAL_LIFECYCLE_STATES
    assert LifecycleState.NEEDS_OPERATOR not in TERMINAL_LIFECYCLE_STATES


@pytest.mark.parametrize("subject", list(LifecycleSubject))
def test_a_core_word_is_itself_and_a_retired_word_is_unknown(
    subject: LifecycleSubject,
) -> None:
    for state in LifecycleState:
        assert adopt_state(subject, state.value) is state
    for word in (*RETIRED, "draft", "unheard-of"):
        assert adopt_state(subject, word) is None
        assert not is_live(subject, word)
        assert not any(is_state(subject, word, state) for state in LifecycleState)


@pytest.mark.parametrize("subject", list(LifecycleSubject))
def test_selections_and_checks_name_only_core_words(subject: LifecycleSubject) -> None:
    core = {state.value for state in LifecycleState}
    assert set(stored_words(subject, LifecycleState)) == core
    assert set(check_words(subject, LifecycleState)) == core
    assert set(live_words(subject)) == core - {
        state.value for state in TERMINAL_LIFECYCLE_STATES
    }


def test_a_caller_names_a_state_with_a_core_word_only() -> None:
    for state in LifecycleState:
        assert input_state(state.value) is state
    for word in (*RETIRED, "nonsense"):
        assert input_state(word) is None
