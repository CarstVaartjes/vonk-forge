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
    let runtime_guard = std::future::pending::<Result<(), crate::host_runtime::HostRuntimeError>>();

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
        std::future::pending::<Result<(), crate::host_runtime::HostRuntimeError>>(),
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

/// Inspections of a loading container: each answers with the next log tail.
fn load_inspections(
    tails: impl Fn(usize) -> String,
) -> impl FnMut() -> std::future::Ready<Result<Option<String>, crate::host_runtime::HostRuntimeError>>
{
    let mut calls = 0;
    move || {
        calls += 1;
        std::future::ready(Ok(Some(tails(calls))))
    }
}

#[tokio::test(start_paused = true)]
async fn a_load_whose_log_keeps_growing_outlives_several_stall_windows() {
    // Wrong implementation this catches: a fixed readiness budget (the old
    // `observation_budget_seconds` total, or a distributed start's accepted
    // deadline) ended a model load that was still writing its log.
    let window = Duration::from_secs(60);
    let started = tokio::time::Instant::now();
    let accepted = started + Duration::from_secs(30);
    let (stall_at, stall_deadline) = tokio::sync::watch::channel(Utc::now().fixed_offset());
    let mut reported = 0;
    let (_sender, cancellation) = tokio::sync::watch::channel(false);
    let outcome = wait_ready_with_runtime_guard_and_cancellation(
        async {
            tokio::time::sleep(5 * window).await;
            Ok(())
        },
        guard_load(
            window,
            Some(accepted),
            &stall_at,
            load_inspections(|call| format!("loading safetensors shard {call}")),
            || reported += 1,
        ),
        cancellation,
    )
    .await;
    assert!(matches!(outcome, ReadinessOutcome::Ready));
    assert!(started.elapsed() >= 5 * window);
    assert!(reported > 0, "progress was never reported");
    // A readiness probe bounded by the published stall instant outlives the
    // accepted deadline too.
    assert!(*stall_deadline.borrow() > Utc::now().fixed_offset());
}

#[tokio::test(start_paused = true)]
async fn a_load_whose_log_stops_growing_ends_after_one_window() {
    // Wrong implementation this catches: any inspection (a running container,
    // a renewed lease) counted as progress, so a hung load never ended.
    let window = Duration::from_secs(60);
    for accepted in [None, Some(Duration::from_secs(30))] {
        let started = tokio::time::Instant::now();
        let (stall_at, _stall_deadline) = tokio::sync::watch::channel(Utc::now().fixed_offset());
        let mut reported = 0;
        let (_sender, cancellation) = tokio::sync::watch::channel(false);
        let outcome = wait_ready_with_runtime_guard_and_cancellation(
            std::future::pending::<Result<(), crate::health::HealthError>>(),
            guard_load(
                window,
                accepted.map(|after| started + after),
                &stall_at,
                load_inspections(|_| "waiting for the compile".to_owned()),
                || reported += 1,
            ),
            cancellation,
        )
        .await;
        assert!(matches!(outcome, ReadinessOutcome::Stalled));
        assert_eq!(started.elapsed(), accepted.unwrap_or(window));
        assert_eq!(reported, 0);
    }
}

#[tokio::test(start_paused = true)]
async fn an_exited_load_ends_at_the_next_inspection_with_its_logs() {
    // Wrong implementation this catches: an exited container was only noticed
    // when the window ran out, or its output was dropped from the failure.
    let window = Duration::from_secs(3600);
    let started = tokio::time::Instant::now();
    let (stall_at, _stall_deadline) = tokio::sync::watch::channel(Utc::now().fixed_offset());
    let (_sender, cancellation) = tokio::sync::watch::channel(false);
    let outcome = wait_ready_with_runtime_guard_and_cancellation(
        std::future::pending::<Result<(), crate::health::HealthError>>(),
        guard_load(
            window,
            None,
            &stall_at,
            || {
                std::future::ready(Err(crate::host_runtime::HostRuntimeError::HelperRejected {
                    code: HelperErrorCode::RuntimeProcessExited,
                    diagnostic: Some("exit_code=1".to_owned()),
                    process_logs: Some(Box::new(crate::failure_evidence::detail_logs(
                        "CUDA out of memory",
                    ))),
                }))
            },
            || {},
        ),
        cancellation,
    )
    .await;
    let ReadinessOutcome::GuardFailed(error) = outcome else {
        panic!("an exited container must end the readiness wait");
    };
    assert!(!temporary_observation_error(&error));
    assert!(error.process_logs().is_some());
    assert!(started.elapsed() <= LOAD_INSPECTION_INTERVAL);
}

// P6: a probe that cannot run is named as such; before, every readiness error
// was reported as a deadline the workload never reached.
#[tokio::test]
async fn a_failed_readiness_probe_is_not_a_readiness_deadline() {
    let (_sender, cancellation) = tokio::sync::watch::channel(false);
    let outcome = wait_ready_with_runtime_guard_and_cancellation(
        async { Err(crate::health::HealthError::Path) },
        std::future::pending::<Result<(), crate::host_runtime::HostRuntimeError>>(),
        cancellation,
    )
    .await;
    assert!(matches!(
        outcome,
        ReadinessOutcome::Unprobed(crate::health::HealthError::Path)
    ));
    let (_sender, cancellation) = tokio::sync::watch::channel(false);
    let outcome = wait_ready_with_runtime_guard_and_cancellation(
        async { Err(crate::health::HealthError::Deadline) },
        std::future::pending::<Result<(), crate::host_runtime::HostRuntimeError>>(),
        cancellation,
    )
    .await;
    assert!(matches!(outcome, ReadinessOutcome::Deadline));
}
