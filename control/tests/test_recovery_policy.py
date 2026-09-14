from datetime import UTC, datetime, timedelta

from vonk_control.recovery_policy import (
    FailureKind,
    RecoveryDecision,
    RecoveryPolicy,
    classify,
    kind_for_agent_error,
)


def test_retry_budget_is_stable_and_honors_dependency_cooldown() -> None:
    policy = RecoveryPolicy(max_failures=3, base_delay_seconds=2, max_delay_seconds=16)
    now = datetime(2026, 9, 14, tzinfo=UTC)
    first = policy.next_attempt("exact-transfer", 1, now)
    assert first is not None and now < first <= now + timedelta(seconds=3)
    assert policy.next_attempt("exact-transfer", 1, now) == first
    cooldown = now + timedelta(seconds=30)
    assert policy.next_attempt("exact-transfer", 2, now, retry_after=cooldown) == cooldown
    assert policy.next_attempt("exact-transfer", 3, now) is None


def test_unclassified_distribution_failure_does_not_become_automatic_retry() -> None:
    old_failure = {"error_code": "artifact_distribution_failed"}
    assert classify(kind_for_agent_error(old_failure)) is RecoveryDecision.BLOCK
    assert classify(kind_for_agent_error({
        **old_failure,
        "failure_kind": FailureKind.TEMPORARY_DEPENDENCY.value,
    })) is RecoveryDecision.RETRY
    assert classify(FailureKind.UNCERTAIN_EFFECT) is RecoveryDecision.OBSERVE
