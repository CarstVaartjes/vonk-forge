"""The stored state vocabulary is the core's; the retired words are one alias table."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    INPUT_ALIASES,
    STATE_ALIASES,
    TERMINAL_LIFECYCLE_STATES,
    AdoptedState,
    LifecycleState,
    LifecycleSubject,
    StateAlias,
    adopt_state,
    input_state,
)

ROOT = Path(__file__).resolve().parents[2]


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


def test_every_retired_word_is_adopted_by_some_subject_and_means_a_core_state() -> None:
    adopted = {alias for rows in STATE_ALIASES.values() for alias in rows}
    assert adopted == set(StateAlias)
    for rows in STATE_ALIASES.values():
        for alias, meaning in rows.items():
            assert isinstance(meaning.state, LifecycleState), alias
            assert meaning.state.value != alias.value


def test_an_alias_is_never_a_core_word() -> None:
    core = {state.value for state in LifecycleState}
    assert core.isdisjoint(alias.value for alias in StateAlias)


@pytest.mark.parametrize("subject", list(LifecycleSubject))
def test_a_core_word_is_adopted_as_itself_by_every_subject(
    subject: LifecycleSubject,
) -> None:
    for state in LifecycleState:
        assert adopt_state(subject, state.value) == AdoptedState(state)


def test_old_rows_are_adopted_on_read() -> None:
    assert adopt_state(LifecycleSubject.JOB, "waiting-for-operator") == AdoptedState(
        LifecycleState.NEEDS_OPERATOR
    )
    assert adopt_state(LifecycleSubject.ARTIFACT_JOB, "cancelling") == AdoptedState(
        LifecycleState.OBSERVING, cancel_requested=True
    )
    # The same word, a different meaning per subject.
    assert adopt_state(
        LifecycleSubject.MODEL_CACHE_OPERATION, "partial"
    ) == AdoptedState(LifecycleState.BACKOFF)
    # An attempt is a record of one try, never a wait for a person.
    assert adopt_state(
        LifecycleSubject.AGENT_OPERATION_ATTEMPT, "waiting-for-operator"
    ) == AdoptedState(LifecycleState.OBSERVING)
    # An expired attempt was interrupted (observed); an expired job is over.
    assert adopt_state(
        LifecycleSubject.AGENT_OPERATION_ATTEMPT, "expired"
    ) == AdoptedState(LifecycleState.OBSERVING)
    assert adopt_state(LifecycleSubject.JOB, "expired") == AdoptedState(
        LifecycleState.FAILED
    )


def test_a_word_the_subject_keeps_outside_the_vocabulary_is_left_to_its_owner() -> None:
    assert adopt_state(LifecycleSubject.ARTIFACT_JOB, "draft") is None
    # A subject that never used a retired word does not adopt it silently.
    assert adopt_state(LifecycleSubject.JOB_ATTEMPT, "partial") is None
    # Its CHECK constraint never admitted these.
    for word in ("waiting", "cancelling", "expired"):
        assert adopt_state(LifecycleSubject.FLEET_PROFILE_APPLICATION, word) is None


def test_callers_may_still_send_a_retired_word_as_input() -> None:
    assert input_state("waiting-for-operator") is LifecycleState.NEEDS_OPERATOR
    assert input_state("needs-operator") is LifecycleState.NEEDS_OPERATOR
    assert input_state("cancelling") is LifecycleState.OBSERVING
    assert input_state("nonsense") is None
    assert set(INPUT_ALIASES) == set(StateAlias)


def test_the_generated_typescript_carries_the_same_aliases() -> None:
    source = (ROOT / "control/web/src/api/vocabulary.generated.ts").read_text()
    block = re.search(r"STATE_INPUT_ALIASES = \{(.*?)\} as const", source, re.DOTALL)
    assert block
    pairs = dict(re.findall(r'"([^"]+)": LifecycleState\.([A-Z_]+)', block.group(1)))
    assert pairs == {alias.value: state.name for alias, state in INPUT_ALIASES.items()}


def test_a_query_finds_old_and_new_rows_by_meaning() -> None:
    from vonk_agent_protocol import is_live, is_state, live_words, stored_words

    cache = LifecycleSubject.MODEL_CACHE_OPERATION
    assert "partial" in live_words(cache) and "backoff" in live_words(cache)
    assert "succeeded" not in live_words(cache)
    assert stored_words(cache, [LifecycleState.BACKOFF]) == ("backoff", "partial")
    assert is_live(cache, "partial") and is_live(cache, "backoff")
    assert not is_live(cache, "failed") and not is_live(cache, None)
    assert is_state(cache, "partial", LifecycleState.BACKOFF)
    assert not is_state(cache, "unheard-of", LifecycleState.BACKOFF)
    job = LifecycleSubject.JOB
    assert stored_words(job, [LifecycleState.OBSERVING]) == (
        "observing",
        "waiting",
        "cancelling",
    )


def test_a_model_validates_an_old_row_as_the_word_it_means_now() -> None:
    from typing import Annotated, Literal

    from pydantic import BaseModel, BeforeValidator, ConfigDict
    from vonk_agent_protocol import state_adopter

    class View(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        state: Annotated[
            Literal[LifecycleState.QUEUED, LifecycleState.BACKOFF],
            BeforeValidator(state_adopter(LifecycleSubject.MODEL_CACHE_OPERATION)),
        ]

    assert View(state="partial").state is LifecycleState.BACKOFF
    assert View.model_validate_json('{"state": "backoff"}').state is (
        LifecycleState.BACKOFF
    )
    with pytest.raises(ValueError):
        View(state="succeeded")


def test_an_old_attempt_word_also_yields_why_it_is_observed() -> None:
    from vonk_agent_protocol import ObservationCause, legacy_observation_cause

    assert legacy_observation_cause("waiting-for-operator") is (
        ObservationCause.REPORTED_UNKNOWN
    )
    assert legacy_observation_cause("expired") is ObservationCause.LEASE_LAPSED
    assert legacy_observation_cause("failed") is None
