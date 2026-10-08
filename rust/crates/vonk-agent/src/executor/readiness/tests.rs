#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn readiness_identity_uses_controller_digest_forms() {
    let value: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let (image_digest, model_identity) = readiness_identity(&plan);
    assert_eq!(image_digest, plan.runtime_image.image_digest);
    let artifact = &plan.artifacts[0];
    assert_eq!(
        model_identity,
        format!(
            "{}/{}@{}",
            artifact.model.publisher, artifact.model.slug, artifact.model.content_sha256
        )
    );
}

#[tokio::test]
async fn exited_runtime_ends_a_still_pending_readiness_probe() {
    let readiness = std::future::pending::<Result<(), crate::health::HealthError>>();

    let (_sender, cancellation) = tokio::sync::watch::channel(false);
    let outcome = wait_ready_with_runtime_guard_and_cancellation(
        readiness,
        async { Err(crate::host_runtime::HostRuntimeError::StopUncertain) },
        cancellation,
    )
    .await;
    assert!(matches!(outcome, ReadinessOutcome::GuardFailed(_)));
}

#[tokio::test]
async fn a_failed_runtime_inspection_names_the_inspection_not_a_readiness_deadline() {
    // Wrong implementation this catches: the guard collapsed every error into
    // `false`, so a failed privileged inspection was reported as "the workload
    // did not become ready before its deadline" and the Controller's existing
    // `runtime_observation_unavailable` retry could never fire.  Observed live
    // on 2026-09-17, a two-Spark GLM start ended 51 s in -- five ten-second
    // inspection ticks -- with an hour of start budget unused.
    let error = crate::host_runtime::HostRuntimeError::Io(std::io::Error::other(
        "privileged inspection failed",
    ));
    assert!(temporary_observation_error(&error));
    let (_sender, cancellation) = tokio::sync::watch::channel(false);
    let outcome = wait_ready_with_runtime_guard_and_cancellation(
        std::future::pending::<Result<(), crate::health::HealthError>>(),
        async { Err(error) },
        cancellation,
    )
    .await;
    assert!(matches!(outcome, ReadinessOutcome::GuardFailed(_)));
}

#[tokio::test]
async fn successful_readiness_ends_a_still_running_runtime_guard() {
    let runtime_guard = std::future::pending::<
        Result<std::convert::Infallible, crate::host_runtime::HostRuntimeError>,
    >();

    let (_sender, cancellation) = tokio::sync::watch::channel(false);
    let outcome = wait_ready_with_runtime_guard_and_cancellation(
        async { Ok(()) },
        runtime_guard,
        cancellation,
    )
    .await;
    assert!(matches!(outcome, ReadinessOutcome::Ready));
}

#[tokio::test]
async fn collective_readiness_exits_when_the_controller_cancels() {
    let (sender, cancellation) = tokio::sync::watch::channel(false);
    let wait = wait_ready_with_runtime_guard_and_cancellation(
        std::future::pending::<Result<(), crate::health::HealthError>>(),
        std::future::pending::<
            Result<std::convert::Infallible, crate::host_runtime::HostRuntimeError>,
        >(),
        cancellation,
    );
    let trigger = async {
        tokio::task::yield_now().await;
        sender.send_replace(true);
    };
    let (outcome, ()) = tokio::join!(wait, trigger);
    assert!(matches!(outcome, ReadinessOutcome::Cancelled));
}

#[tokio::test]
async fn rank_launch_stability_is_capped_by_the_signed_deadline() {
    let lease =
        (Utc::now() + ChronoDuration::minutes(5)).with_timezone(&FixedOffset::east_opt(0).unwrap());
    let immutable = (Utc::now() - ChronoDuration::milliseconds(1))
        .with_timezone(&FixedOffset::east_opt(0).unwrap());
    let (_lease_sender, lease_receiver) = tokio::sync::watch::channel(lease);
    let (_cancel_sender, cancellation) = tokio::sync::watch::channel(false);

    assert!(
        !wait_for_launch_stability(
            lease_receiver,
            cancellation,
            Some(immutable),
            Duration::from_secs(30),
        )
        .await
    );
}
