from datetime import UTC, datetime, timedelta

from vonk_control.recovery_policy import (
    FailureKind,
    RecoveryDecision,
    RecoveryPolicy,
    classify,
)


def test_retry_budget_is_stable_and_honors_dependency_cooldown() -> None:
    policy = RecoveryPolicy(max_failures=3, base_delay_seconds=2, max_delay_seconds=16)
    now = datetime(2026, 9, 14, tzinfo=UTC)
    first = policy.next_attempt("exact-transfer", 1, now)
    assert first is not None and now < first <= now + timedelta(seconds=3)
    assert policy.next_attempt("exact-transfer", 1, now) == first
    cooldown = now + timedelta(seconds=30)
    assert (
        policy.next_attempt("exact-transfer", 2, now, retry_after=cooldown) == cooldown
    )
    assert policy.next_attempt("exact-transfer", 3, now) is None
    fresh = policy.next_attempt("fresh-transfer", 1, now)
    assert fresh is not None and now < fresh <= now + timedelta(seconds=3)


def test_unclassified_distribution_ends_without_poisoning_fresh_work() -> None:
    from vonk_agent_protocol import FailureCode, LifecycleState
    from vonk_control.lifecycle.image_availability import ImageAvailabilityAdapter
    from vonk_control.recovery_policy import kind_for_failure_fields

    from .test_image_availability_lifecycle import NOW, _job

    adapter = ImageAvailabilityAdapter(clock=lambda: NOW)
    failed = _job(state=LifecycleState.RUNNING, attempt=1)
    decision = classify(
        kind_for_failure_fields(None, FailureCode.ARTIFACT_DISTRIBUTION_FAILED)
    )
    ended = adapter.fail(
        failed,
        NOW,
        retryable=decision is RecoveryDecision.RETRY,
        reason=FailureCode.ARTIFACT_DISTRIBUTION_FAILED,
    )
    assert ended.terminal
    assert ended.next_action_at is None
    fresh = _job(state=LifecycleState.QUEUED)
    fresh.id = "fresh-distribution"
    fresh.request_id = "fresh-request"
    admitted = adapter.claim(fresh, "fresh-worker", NOW + timedelta(seconds=30), NOW)
    assert admitted.state is LifecycleState.RUNNING
    assert admitted.id != ended.id
    assert adapter.succeed(fresh, NOW).state is LifecycleState.SUCCEEDED


def test_uncertain_distribution_observation_never_becomes_unverified_success() -> None:
    from vonk_agent_protocol import FailureCode, LifecycleState
    from vonk_control.lifecycle import Effect, Outcome, Reported, transition
    from vonk_control.lifecycle.image_availability import ImageAvailabilityAdapter
    from vonk_control.recovery_policy import kind_for_failure_fields

    from .test_image_availability_lifecycle import NOW, _job

    adapter = ImageAvailabilityAdapter(clock=lambda: NOW)
    job = _job(state=LifecycleState.RUNNING, attempt=1)
    decision = classify(
        kind_for_failure_fields(
            FailureKind.UNCERTAIN_EFFECT, FailureCode.ARTIFACT_DISTRIBUTION_FAILED
        )
    )
    event = Reported(
        Outcome.UNKNOWN if decision is RecoveryDecision.OBSERVE else Outcome.DONE
    )
    observed = transition(adapter.adopt(job), event, adapter, NOW).row
    assert observed.state is not LifecycleState.SUCCEEDED
    assert observed.effect is not Effect.ESTABLISHED
    assert observed.next_action_at is not None
    fresh = _job(state=LifecycleState.QUEUED)
    assert (
        adapter.claim(fresh, "fresh-owner", NOW + timedelta(seconds=30), NOW).state
        is LifecycleState.RUNNING
    )
