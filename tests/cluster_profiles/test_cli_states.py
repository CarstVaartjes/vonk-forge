"""The CLI reads the core state vocabulary and the words older Controllers sent."""

from __future__ import annotations

from types import SimpleNamespace

from cluster_profiles import cli_states
from cluster_profiles.cli_outcome import CommandOutcome


def _outcome(document: dict[str, object]) -> CommandOutcome:
    args = SimpleNamespace(outcome_context="mutation")
    return CommandOutcome.from_command(args, document)  # type: ignore[arg-type]


def test_a_failed_update_with_some_children_done_exits_one_like_the_old_partial() -> (
    None
):
    assert _outcome({"state": "failed", "partial": True}).exit_code == 1
    assert _outcome({"state": "partial"}).exit_code == 1
    assert _outcome({"state": "failed"}).exit_code == 2


def test_a_wait_for_a_person_is_the_same_under_either_spelling() -> None:
    for word in cli_states.OPERATOR_WAIT_STATES:
        assert _outcome({"state": word}).exit_code == 2


def test_an_accepted_cancel_is_observing_or_the_old_cancelling_or_ended() -> None:
    assert cli_states.CANCEL_ACCEPTED_STATES == {"observing", "cancelling", "cancelled"}


def test_a_job_prepared_or_running_is_named_by_its_stage_or_state() -> None:
    assert cli_states.lifecycle_state({"state": None, "preparation": "draft"}) == (
        "draft"
    )
    assert cli_states.lifecycle_state({"state": "running"}) == "running"
    assert cli_states.cancel_pending({"state": "running", "cancel_requested_at": "x"})
    assert not cli_states.cancel_pending(
        {"state": "cancelled", "cancel_requested_at": "x"}
    )
