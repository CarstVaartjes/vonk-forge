"""A stale aggregate must age its retained members with it."""

from datetime import UTC, datetime, timedelta

from vonk_agent_protocol.wire_model import OperationMemberProgress, OperationProgress
from vonk_control.operations.progress import STALE_AFTER_SECONDS, project_progress


def _progress(observed: datetime) -> OperationProgress:
    stamp = observed.isoformat()
    member = OperationMemberProgress(
        member_id="spk_a",
        state="running",
        phase="transfer",
        completed_bytes=1,
        total_bytes=4,
        smoothed_bytes_per_second=1.0,
        eta_seconds=3.0,
        observed_at=stamp,
        activity="active",
    )
    return OperationProgress(
        phase="transfer",
        completed_bytes=1,
        total_bytes=4,
        total_bytes_known=True,
        observed_at=stamp,
        last_progress_at=stamp,
        members=[member],
    )


def test_aggregate_and_members_agree_once_the_sample_is_old():
    then = datetime(2026, 11, 23, tzinfo=UTC)
    fresh = project_progress(_progress(then), then + timedelta(seconds=1))
    assert fresh.activity == "active" and fresh.members[0].activity == "active"
    old = project_progress(
        _progress(then), then + timedelta(seconds=STALE_AFTER_SECONDS + 1)
    )
    assert old.activity == "waiting"
    assert old.members[0].activity == "waiting"
    assert old.members[0].smoothed_bytes_per_second is None
