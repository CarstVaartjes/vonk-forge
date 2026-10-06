"""The generic job table speaks the core vocabulary through one module."""

from __future__ import annotations

from vonk_agent_protocol import LifecycleState
from vonk_control import job_states


def test_a_selection_finds_old_and_new_spellings_of_a_state() -> None:
    wait = job_states.words(LifecycleState.NEEDS_OPERATOR)
    assert set(wait) == {"needs-operator", "waiting-for-operator"}
    observing = job_states.words(LifecycleState.OBSERVING)
    assert {"observing", "waiting", "cancelling"} <= set(observing)
    assert {"backoff", "partial"} <= set(job_states.words(LifecycleState.BACKOFF))


def test_a_batch_that_ended_partial_is_failed_not_retrying() -> None:
    from vonk_control.recipe_update_batches import UPDATE_KIND

    assert job_states.means("partial", LifecycleState.BACKOFF)
    assert job_states.means("partial", LifecycleState.FAILED, kind=UPDATE_KIND)
    assert not job_states.means("partial", LifecycleState.BACKOFF, kind=UPDATE_KIND)


def test_an_attempt_that_lapsed_was_interrupted_but_a_job_that_lapsed_is_over() -> None:
    assert job_states.attempt_means("expired", LifecycleState.OBSERVING)
    assert job_states.means("expired", LifecycleState.FAILED)
    assert job_states.core("cancelling") is LifecycleState.OBSERVING
    assert job_states.cancel_requested_word("cancelling")
    assert not job_states.cancel_requested_word("waiting")
