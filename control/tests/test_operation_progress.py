from datetime import UTC, datetime, timedelta

import pytest
from vonk_agent_protocol import OperationMemberProgress, OperationProgress
from vonk_control.operation_contract import validate_progress_update
from vonk_control.operation_progress import (
    aggregate_progress,
    observe_progress,
    progress_write_due,
    project_progress,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def sample(previous=None, *, seconds=0, **values):
    values.setdefault("phase", "copying")
    current = validate_progress_update(previous, values)
    return observe_progress(previous, current, NOW + timedelta(seconds=seconds))


def test_rates_unknown_totals_and_zero_throughput_do_not_invent_eta():
    first = sample(completed_bytes=10)
    assert "eta_seconds" not in first
    second = sample(first, seconds=2, completed_bytes=30)
    assert second["bytes_per_second"] == second["smoothed_bytes_per_second"] == 10.0
    assert "eta_seconds" not in second
    known = sample(
        second, seconds=4, completed_bytes=50, total_bytes=100, total_bytes_known=True
    )
    assert known["eta_seconds"] == 5.0
    stopped = sample(known, seconds=6, completed_bytes=50)
    assert stopped["bytes_per_second"] == 0
    assert "eta_seconds" not in stopped
    assert stopped["last_progress_at"] == known["last_progress_at"]


def test_partial_phase_change_retains_counters_and_verification_clears_eta():
    first = sample(completed_bytes=10, total_bytes=100, total_bytes_known=True)
    active = sample(first, seconds=2, completed_bytes=30)
    verified = sample(active, seconds=4, phase="verifying")
    assert verified["completed_bytes"] == 30
    assert "eta_seconds" not in verified
    assert "bytes_per_second" not in verified
    assert verified["activity"] == "active"


def test_reconnect_preserves_counts_elapsed_and_rejects_regressing_retry():
    first = sample(completed_bytes=40, completed_items=1)
    resumed = sample(dict(first), seconds=20, completed_bytes=60, completed_items=2)
    assert resumed["elapsed_seconds"] == 20.0
    assert resumed["bytes_per_second"] == 1.0
    for values in ({"completed_bytes": 30}, {"completed_items": 0}):
        with pytest.raises(ValueError, match="backwards"):
            sample(resumed, seconds=21, **values)


def test_stalled_is_advisory_phase_aware_and_stale_rate_is_hidden():
    progress = OperationProgress.model_validate(sample(completed_bytes=10))
    stale = project_progress(progress, NOW + timedelta(seconds=50))
    assert stale.activity == "waiting"
    stalled = project_progress(progress, NOW + timedelta(seconds=121))
    assert stalled.activity == "possibly_stalled"
    for phase in ("verifying", "extracting", "building", "starting"):
        assert (
            project_progress(
                progress.model_copy(update={"phase": phase}),
                NOW + timedelta(seconds=121),
            ).activity
            == "waiting"
        )
    assert progress.activity == "active"  # read projection does not mutate stored work


def test_write_frequency_coalesces_and_retains_one_snapshot():
    progress = sample(completed_bytes=0)
    for tick in range(1, 1001):
        now = NOW + timedelta(milliseconds=tick)
        next_progress = validate_progress_update(
            progress, {"phase": "copying", "completed_bytes": tick}
        )
        if progress_write_due(progress, next_progress, now):
            progress = observe_progress(progress, next_progress, now)
            assert tick == 1000
    assert progress["completed_bytes"] == 1000
    assert "history" not in progress
    assert progress_write_due(
        progress, {"phase": "verifying"}, NOW + timedelta(seconds=1.1)
    )


def test_parallel_aggregate_eta_uses_slowest_and_unknown_member_stays_unknown():
    a = OperationMemberProgress(
        member_id="a",
        phase="copying",
        completed_bytes=10,
        total_bytes=100,
        bytes_per_second=10.0,
        eta_seconds=9.0,
    )
    b = OperationMemberProgress(
        member_id="b",
        phase="copying",
        completed_bytes=20,
        total_bytes=200,
        bytes_per_second=20.0,
        eta_seconds=9.0,
    )
    result = aggregate_progress([a, b])
    assert (
        result.completed_bytes,
        result.total_bytes,
        result.bytes_per_second,
        result.eta_seconds,
    ) == (30, 300, 30.0, 9.0)
    unknown = aggregate_progress(
        [a, b.model_copy(update={"total_bytes": None, "eta_seconds": None})]
    )
    assert unknown.total_bytes is None and unknown.eta_seconds is None


@pytest.mark.parametrize("value", ["nonsense", "2026-09-08T12:00:00"])
def test_progress_rejects_invalid_or_unzoned_timestamps(value):
    with pytest.raises(ValueError):
        OperationProgress(phase="copying", observed_at=value)


def test_terminal_operation_never_claims_work_is_stalled_or_running():
    from vonk_control.operation_api import _progress_projection
    document = sample(completed_bytes=10, total_bytes=20, total_bytes_known=True)
    for state in ("succeeded", "failed", "cancelled", "waiting-for-operator"):
        projected = _progress_projection(document, state)
        assert projected.activity is None
        assert projected.eta_seconds is None


def test_total_knowledge_can_be_explicitly_withdrawn_without_losing_bytes():
    first = sample(completed_bytes=10, total_bytes=20, total_bytes_known=True)
    next_sample = sample(first, seconds=2, total_bytes_known=False)
    assert next_sample["completed_bytes"] == 10
    assert "total_bytes" not in next_sample and "eta_seconds" not in next_sample


def test_image_availability_boundary_preserves_canonical_measurements():
    from vonk_control.recipe_image_availability_api import _progress
    original = sample(completed_bytes=10, total_bytes=20, total_bytes_known=True, completed_items=1, total_items=2)
    original = sample(original, seconds=2, completed_bytes=15)
    projected = _progress(original)
    assert projected.model_dump(mode="json", exclude_none=True) == original
