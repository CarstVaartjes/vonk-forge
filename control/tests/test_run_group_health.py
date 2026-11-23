"""A group still starting is progress; a lost rank or a failed start is degraded."""

from vonk_agent_protocol.reason_codes.evidence import RunDegradedReason
from vonk_agent_protocol.state_machines import RunState
from vonk_control.run_readiness import GroupHealth, group_health


def _health(reason, run_state, *ranks, issue=False):
    return group_health(
        issue=issue, reason=reason, run_state=run_state, rank_states=ranks
    )


def test_a_group_within_its_start_is_starting_not_degraded():
    for run_state in (RunState.PLANNED, RunState.STARTING):
        assert (
            _health(
                RunDegradedReason.RUN_NOT_RUNNING,
                run_state,
                RunState.STARTING,
                RunState.RUNNING,
            )
            is GroupHealth.STARTING
        )


def test_a_rank_that_dropped_out_is_degraded():
    not_running = RunDegradedReason.RUN_NOT_RUNNING
    assert (
        _health(not_running, RunState.STARTING, RunState.STARTING, RunState.LOST)
        is GroupHealth.DEGRADED
    )
    assert (
        _health(
            RunDegradedReason.RANK_NOT_RUNNING,
            RunState.RUNNING,
            RunState.RUNNING,
            RunState.LOST,
        )
        is GroupHealth.DEGRADED
    )
    assert (
        _health(RunDegradedReason.MISSING_RANKS, RunState.STARTING, RunState.STARTING)
        is GroupHealth.DEGRADED
    )


def test_ready_and_unreadable_groups_keep_their_verdicts():
    assert _health(None, RunState.RUNNING, RunState.RUNNING) is GroupHealth.HEALTHY
    assert (
        _health(None, RunState.RUNNING, RunState.RUNNING, issue=True)
        is GroupHealth.UNAVAILABLE
    )


def test_a_failed_start_is_degraded():
    reason = RunDegradedReason.RUN_NOT_RUNNING
    assert _health(reason, RunState.FAILED, RunState.FAILED) is GroupHealth.DEGRADED
