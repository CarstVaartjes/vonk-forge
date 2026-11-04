"""The CLI reads the core state vocabulary, and only that."""

from __future__ import annotations

from types import SimpleNamespace

from cluster_profiles import cli_states
from cluster_profiles.cli_outcome import CommandOutcome


def _outcome(document: dict[str, object]) -> CommandOutcome:
    args = SimpleNamespace(outcome_context="mutation")
    return CommandOutcome.from_command(args, document)  # type: ignore[arg-type]


def test_a_failed_update_with_some_children_done_exits_one() -> None:
    assert _outcome({"state": "failed", "partial": True}).exit_code == 1
    assert _outcome({"state": "failed"}).exit_code == 2


def test_a_wait_for_a_person_exits_two() -> None:
    assert _outcome({"state": cli_states.NEEDS_OPERATOR}).exit_code == 2


def test_a_retired_word_is_not_a_state_the_cli_knows() -> None:
    for word in ("waiting-for-operator", "cancelling", "partial", "expired"):
        assert word not in cli_states.ARTIFACT_JOB_STATES
        assert word not in cli_states.CANCEL_ACCEPTED_STATES
        assert word not in cli_states.FAILED_JOB_STATES


def test_an_accepted_cancel_is_observing_or_ended() -> None:
    assert cli_states.CANCEL_ACCEPTED_STATES == {"observing", "cancelled"}


def test_a_job_prepared_or_running_is_named_by_its_stage_or_state() -> None:
    assert cli_states.lifecycle_state({"state": None, "preparation": "draft"}) == (
        "draft"
    )
    assert cli_states.lifecycle_state({"state": "running"}) == "running"
    assert cli_states.lifecycle_state({"state": "draft"}) == "draft"
    assert cli_states.preparation({"state": "draft"}) is None
    assert cli_states.cancel_pending({"state": "running", "cancel_requested_at": "x"})
    assert not cli_states.cancel_pending(
        {"state": "cancelled", "cancel_requested_at": "x"}
    )
