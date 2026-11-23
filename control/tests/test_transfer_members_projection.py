"""Transfer members that carry their sample time keep rate and ETA while fresh."""

from datetime import UTC, datetime, timedelta

from vonk_agent_protocol.wire_model import OperationMemberProgress
from vonk_control.operations.progress import aggregate_progress, project_progress

NOW = datetime(2026, 11, 23, tzinfo=UTC)


def _member(observed: datetime | None) -> OperationMemberProgress:
    stamp = None if observed is None else observed.isoformat()
    return OperationMemberProgress(
        member_id="spk_a",
        phase="transfer",
        state="running",
        completed_bytes=1,
        total_bytes=4,
        bytes_per_second=2.0,
        smoothed_bytes_per_second=2.0,
        eta_seconds=3.0,
        activity="active",
        observed_at=stamp,
        last_progress_at=stamp,
    )


def test_fresh_members_keep_the_servers_rate_and_eta():
    projected = project_progress(
        aggregate_progress([_member(NOW)]), NOW + timedelta(seconds=1)
    )
    assert projected.activity == "active"
    assert projected.smoothed_bytes_per_second == 2.0
    assert projected.eta_seconds == 3.0


def test_a_member_without_a_sample_time_cannot_carry_an_estimate():
    projected = project_progress(aggregate_progress([_member(None)]), NOW)
    assert projected.smoothed_bytes_per_second is None
