#![cfg(test)]
#[tokio::test(start_paused = true)]
async fn refused_control_reports_and_resumes_without_certificate_change() {
    // Wrong implementation: one control 403 permanently suppresses claims
    // and observations, or retries without backoff while the identity is unchanged.
    let mut authority = super::AuthorityRetry::default();
    let claims = Cell::new(0);
    let reports = Cell::new(0);
    let statuses = RefCell::new(Vec::new());
    let started = tokio::time::Instant::now();
    let lane = async {
        let deadline = started + Duration::from_secs(40);
        while tokio::time::Instant::now() < deadline {
            if authority
                .observe_before_claim(async { reports.set(reports.get() + 1) }, |s| {
                    statuses.borrow_mut().push(s.to_owned());
                })
                .await
                .is_none()
            {
                continue;
            }
            claims.set(claims.get() + 1);
            if claims.get() == 1 {
                authority.refuse(vonk_agent::executor::LoopError::Client(
                    ClientError::Controller(Box::new(ControllerError::from_status(403))),
                ));
            } else {
                assert!(started.elapsed() >= Duration::from_secs(30));
                authority = super::AuthorityRetry::default();
                break;
            }
        }
    };
    tokio::select! {
        () = lane => {},
        () = tokio::time::sleep(Duration::from_secs(40)) => panic!("control lane never recovered"),
    }
    assert_eq!(claims.get(), 2);
    assert!(reports.get() >= 3, "refusal suppressed observation reports");
    assert!(
        statuses
            .borrow()
            .iter()
            .all(|s| s.contains("HTTP 403") && s.contains("authority refused"))
    );
    assert!(authority.reason.is_none());
}

#[tokio::test(start_paused = true)]
async fn running_workloads_are_reported_while_a_long_claim_executes() {
    // Wrong implementation: run evidence is reported only between claims,
    // so a 40-minute download leaves every running rank stale.
    let started = tokio::time::Instant::now();
    let reports = RefCell::new(Vec::new());
    let output = super::observing_during(
        async {
            tokio::time::sleep(Duration::from_mins(40)).await;
            "downloaded"
        },
        super::RUN_OBSERVATION_INTERVAL,
        async || reports.borrow_mut().push(started.elapsed()),
    )
    .await;
    assert_eq!(output, "downloaded");
    let reports = reports.into_inner();
    // The Controller calls a rank stale after five minutes of silence.
    let mut last = Duration::ZERO;
    for at in reports.iter().copied().chain([Duration::from_mins(40)]) {
        assert!(
            at.checked_sub(last).unwrap() < Duration::from_secs(60),
            "silent from {last:?} to {at:?}"
        );
        last = at;
    }
}

#[tokio::test(start_paused = true)]
async fn claim_completion_does_not_disconnect_an_inflight_observation() {
    // Wrong implementation: dropping the observer at claim completion
    // resets its HTTP stream before the Controller can parse/acknowledge it.
    let acknowledged = Cell::new(false);
    let output = super::observing_during(
        async {
            tokio::time::sleep(Duration::from_secs(15)).await;
            42
        },
        super::RUN_OBSERVATION_INTERVAL,
        async || {
            tokio::time::sleep(Duration::from_secs(10)).await;
            acknowledged.set(true);
        },
    )
    .await;
    assert_eq!(output, 42);
    assert!(acknowledged.get(), "claim completion cancelled the upload");
}

use super::{
    LaneExitWithRotation, PostStopInventory, claim_wait_seconds, collect_inventory_until_ready,
    inventory_refresh_due, inventory_retry_delay, loop_error_is_fatal,
    observation_snapshot_is_empty, report_ready_after_self_test, supervise_lanes_with_rotation,
};
use std::{
    cell::{Cell, RefCell},
    future,
    sync::{
        Arc, Barrier,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use vonk_agent::client::{ClientError, ControllerError};
use vonk_agent::{executor::RecipeObservationError, rotation::RotationError};
use vonk_agent::{inventory::Inventory, inventory::InventoryError};

#[tokio::test(start_paused = true)]
async fn inventory_lane_survives_refusal_and_reports_after_recovery() {
    // Wrong implementation: a refused upload permanently returns from inventory.
    let started = tokio::time::Instant::now();
    let recovered = Cell::new(false);
    let lane = super::inventory_lane(
        || {
            future::ready(Ok(
                test_inventory().to_request(chrono::Utc::now().fixed_offset())
            ))
        },
        |_| {
            future::ready(if started.elapsed() < Duration::from_secs(30) {
                Err(ClientError::Controller(Box::new(
                    ControllerError::from_status(403),
                )))
            } else {
                recovered.set(true);
                Ok(())
            })
        },
        PostStopInventory::default(),
    );
    tokio::pin!(lane);
    tokio::select! {
        () = &mut lane => panic!("inventory lane exited"),
        () = tokio::time::sleep(Duration::from_secs(180)) => {}
    }
    assert!(recovered.get());
}

/// Seconds (after start) at which the lane collected, over 130 s, when a
/// stop completes at `stop_at` seconds (never, if `None`).
async fn inventory_collections(stop_at: Option<u64>) -> Vec<u64> {
    let started = tokio::time::Instant::now();
    let collected = RefCell::new(Vec::new());
    let stops = PostStopInventory::default();
    {
        let lane = super::inventory_lane(
            || {
                collected.borrow_mut().push(started.elapsed().as_secs());
                future::ready(Ok(
                    test_inventory().to_request(chrono::Utc::now().fixed_offset())
                ))
            },
            |_| future::ready(Ok(())),
            stops.clone(),
        );
        tokio::pin!(lane);
        let stop = async {
            if let Some(at) = stop_at {
                tokio::time::sleep(Duration::from_secs(at)).await;
                stops.stopped();
            }
            future::pending::<()>().await;
        };
        tokio::select! {
            () = &mut lane => panic!("inventory lane exited"),
            () = stop => {}
            () = tokio::time::sleep(Duration::from_secs(130)) => {}
        }
    }
    collected.into_inner()
}

#[tokio::test(start_paused = true)]
async fn completed_stop_collects_on_a_bounded_schedule_and_leaves_the_cadence_alone() {
    // Wrong implementations: waiting for the 120 s cadence; a stop-triggered
    // report moving the cadence; the schedule repeating without end.
    let times = inventory_collections(Some(10)).await;
    assert_eq!(times.len(), 6, "{times:?}");
    for (collected, offset) in times[1..5].iter().zip([31, 46, 61, 91]) {
        assert!((10 + offset..=12 + offset).contains(collected), "{times:?}");
    }
    assert!((120..=122).contains(&times[5]), "{times:?}");
}

type GpuMemoryReader = fn() -> Option<u64>;

#[tokio::test(start_paused = true)]
async fn only_a_done_recipe_stop_arms_the_post_stop_schedule() {
    use vonk_agent::{executor::Executor as _, outcome::ExecutionResult};
    use vonk_agent_protocol::generated::{AgentOperation, RecipeStopResult};
    struct Fake(ExecutionResult);
    #[async_trait::async_trait(?Send)]
    impl vonk_agent::executor::Executor for Fake {
        async fn observe(
            &self,
            _: &vonk_agent_protocol::AgentClaim,
        ) -> Option<vonk_agent::executor::operation_observation::OperationObservation> {
            None
        }
        async fn execute(
            &self,
            _: &vonk_agent_protocol::AgentClaim,
            _: tokio::sync::watch::Receiver<chrono::DateTime<chrono::FixedOffset>>,
            _: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            self.0.clone()
        }
    }
    let template: vonk_agent_protocol::AgentClaim = serde_json::from_str(include_str!(
        "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    let (_, lease) = tokio::sync::watch::channel(chrono::Utc::now().fixed_offset());
    let (_, cancel) = tokio::sync::watch::channel(false);
    let done = || ExecutionResult::done(RecipeStopResult::default());
    let unknown = ExecutionResult::unknown(
        vonk_agent_protocol::generated::WaitReason::RuntimeEffectUnconfirmed,
        "unconfirmed",
        vonk_agent::outcome::UnknownEvidence::at(
            vonk_agent_protocol::generated::FailureStage::AgentRestart,
        ),
    );
    for (operation, result, armed) in [
        (AgentOperation::RecipeStop, done(), true),
        (
            AgentOperation::RecipeStop,
            ExecutionResult::failed("no"),
            false,
        ),
        (AgentOperation::RecipeStop, unknown, false),
        (AgentOperation::RecipeStart, done(), false),
    ] {
        let stops = PostStopInventory::default();
        let data = tempfile::tempdir().unwrap();
        let executor = super::CustodyExecutor {
            executor: &Fake(result),
            available: true,
            stops: stops.clone(),
            memory: Arc::new(vonk_agent::memory_readings::Ledger::open(
                std::path::Path::new("/nonexistent"),
                data.path(),
                4096,
            )),
            gpu_memory: || Some(0),
        };
        let mut claim = template.clone();
        claim.operation = operation;
        executor
            .execute(&claim, lease.clone(), cancel.clone())
            .await;
        let first = tokio::time::Instant::now() + Duration::from_secs(31);
        assert_eq!(stops.due(first), armed, "{:?}", claim.operation);
    }

    // A stop that never completed freezes nothing: the next completed stop
    // with no GPU process is a reading, and one with a GPU process is not.
    let host = tempfile::tempdir().unwrap();
    let data = tempfile::tempdir().unwrap();
    let proc = host.path().join("proc");
    std::fs::create_dir_all(proc.join("sys/kernel/random")).unwrap();
    std::fs::write(
        proc.join("sys/kernel/random/boot_id"),
        "6f1c1d8e-0000-4000-8000-000000000001\n",
    )
    .unwrap();
    std::fs::write(proc.join("meminfo"), "SecPageTables: 100 kB\n").unwrap();
    let memory = Arc::new(vonk_agent::memory_readings::Ledger::open(
        host.path(),
        data.path(),
        4096,
    ));
    let mut stop = template.clone();
    stop.operation = AgentOperation::RecipeStop;
    let cases: [(ExecutionResult, GpuMemoryReader, bool); 4] = [
        (ExecutionResult::failed("no"), || Some(0), false),
        (done(), || Some(1 << 30), false),
        (done(), || None, false),
        (done(), || Some(0), true),
    ];
    for (result, gpu_memory, recorded) in cases {
        let executor = super::CustodyExecutor {
            executor: &Fake(result),
            available: true,
            stops: PostStopInventory::default(),
            memory: memory.clone(),
            gpu_memory,
        };
        executor.execute(&stop, lease.clone(), cancel.clone()).await;
        assert_eq!(memory.snapshot().is_some(), recorded);
    }
}

#[tokio::test(start_paused = true)]
async fn without_a_stop_inventory_keeps_the_regular_cadence() {
    let times = inventory_collections(None).await;
    assert_eq!(times.len(), 2, "{times:?}");
    assert!((120..=122).contains(&times[1]), "{times:?}");
}

#[test]
fn startup_readiness_is_reported_only_after_self_test_succeeds() {
    let events = RefCell::new(Vec::new());
    let identity = report_ready_after_self_test(
        || {
            events.borrow_mut().push("self-test");
            Ok::<_, &'static str>("identity")
        },
        || events.borrow_mut().push("ready"),
    )
    .unwrap();
    assert_eq!(identity, "identity");
    assert_eq!(*events.borrow(), ["self-test", "ready"]);

    let error = report_ready_after_self_test(
        || Err::<(), _>("self-test failed"),
        || panic!("failed self-test must not report READY=1"),
    )
    .unwrap_err();
    assert_eq!(error, "self-test failed");
}

#[test]
fn idle_agent_refreshes_inventory_before_controller_admission_expires() {
    let reported_at = tokio::time::Instant::now();
    assert!(!inventory_refresh_due(
        Some(reported_at),
        reported_at + Duration::from_secs(119),
    ));
    // A successful idle claim must not leave the startup inventory as the
    // only report. The Controller refuses admission after five minutes.
    assert!(inventory_refresh_due(
        Some(reported_at),
        reported_at + Duration::from_secs(120),
    ));
    let refreshed_at = reported_at + Duration::from_secs(180);
    assert!(!inventory_refresh_due(Some(refreshed_at), refreshed_at));
    assert!(inventory_refresh_due(None, refreshed_at));
}

#[tokio::test]
async fn failed_local_state_refused_identity_and_inventory_admit_a_fresh_session() {
    let attempts = Cell::new(0);
    let root = tempfile::tempdir().unwrap();
    let config_path = root.path().join("agent.toml");
    std::fs::write(&config_path, "incomplete local configuration").unwrap();
    let repaired_config = r#"enrollment_url = "https://enroll.example.test/"
controller_url = "https://agents.example.test/"
ca_path = "/etc/vonk-forge-agent/controller-ca.pem"
ca_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
data_dir = "/var/lib/vonk-forge-agent"
node_id = "spk_0123456789abcdef0123456789abcdef"
"#;
    let result = tokio::time::timeout(
        Duration::from_secs(5),
        super::supervise_sessions(
            || {
                attempts.set(attempts.get() + 1);
                let outcome: Result<(), Box<dyn std::error::Error>> = match attempts.get() {
                    1 => vonk_agent::config::AgentConfig::load(&config_path)
                        .map(|_| ())
                        .map_err(Into::into),
                    2 => Err(RotationError::Client(ClientError::Controller(Box::new(
                        ControllerError::from_status(403),
                    )))
                    .into()),
                    3 => {
                        Err(InventoryError::PrerequisiteUnavailable("NVIDIA GPU discovery").into())
                    }
                    _ => vonk_agent::config::AgentConfig::load(&config_path)
                        .map(|_| ())
                        .map_err(Into::into),
                };
                future::ready(outcome)
            },
            |_| {
                std::fs::write(&config_path, repaired_config).unwrap();
                Duration::from_secs(1)
            },
        ),
    )
    .await
    .expect("repaired configuration did not admit a fresh session");
    assert!(result.is_ok());
    assert_eq!(attempts.get(), 4);
}

#[test]
fn prerequisite_restart_deadline_has_an_exact_boundary() {
    assert!(!super::prerequisite_restart_due(Duration::from_secs(299)));
    assert!(super::prerequisite_restart_due(Duration::from_secs(300)));
}

fn test_inventory() -> Inventory {
    Inventory {
        memory_total_bytes: 100,
        memory_available_bytes: 90,
        disk_total_bytes: 1000,
        disk_available_bytes: 900,
        state_database_reserve_held: true,
        gpu_count: 1,
        gpu_health: vonk_agent_protocol::generated::GpuHealth::Healthy,
        gpu_memory_total_bytes: 100,
        gpu_memory_free_bytes: 90,
        memory_pool: vonk_agent_protocol::MemoryPool::Separate,
        nvidia_driver_version: "test".to_owned(),
        container_runtime_version: "test".to_owned(),
        artifact_store_read_only: false,
        capabilities: vec![],
        fabric_address: None,
        fabric_bandwidth_mbps: None,
        network_interfaces: None,
        nas_route_interface: None,
        host_settings: None,
        installations: None,
        unused_model_bytes: None,
        model_objects: None,
        runs: None,
        installation_bytes: None,
    }
}

#[test]
fn inventory_oversized_model_scan_keeps_capacity_and_next_scan_recovers() {
    // Wrong implementation: a valid capacity observation is refused because
    // optional model evidence fills the collector/HTTP envelope.
    let mut inventory = test_inventory();
    inventory.model_objects = Some(
        (0..16_384)
            .map(|index| vonk_agent_protocol::generated::ModelStoreObject {
                sha256: format!("{index:064x}"),
                bytes: 1,
            })
            .collect(),
    );
    let observed = chrono::Utc::now().into();
    let request = inventory.to_request(observed);
    assert!(request.model_objects.is_none());
    assert_eq!(request.disk_free_bytes, inventory.disk_available_bytes);
    assert_eq!(
        request.host_memory_free_bytes,
        inventory.memory_available_bytes
    );
    request.validate().unwrap();
    inventory.model_objects.as_mut().unwrap().truncate(1024);
    let recovered = inventory.to_request(observed);
    assert_eq!(recovered.model_objects, inventory.model_objects);
    recovered.validate().unwrap();
}

#[tokio::test(start_paused = true)]
async fn unavailable_gpu_exits_then_a_fresh_namespace_can_collect() {
    let mut failures = 0;
    let result = collect_inventory_until_ready(
        || async {
            Err::<Inventory, _>(InventoryError::PrerequisiteUnavailable(
                "NVIDIA GPU discovery",
            ))
        },
        &mut failures,
        1,
        1,
    )
    .await;
    assert!(matches!(
        result,
        Err(InventoryError::PrerequisiteUnavailable(_))
    ));
    let recovered =
        collect_inventory_until_ready(|| async { Ok(test_inventory()) }, &mut failures, 1, 1)
            .await
            .unwrap();
    assert_eq!(recovered.gpu_count, 1);
}

#[tokio::test(start_paused = true)]
async fn failed_inventory_round_keeps_control_available_and_later_recovers() {
    let attempted = Cell::new(false);
    let mut failures = 0;
    let inventory = async {
        let _ = collect_inventory_until_ready(
            || async {
                attempted.set(true);
                Err::<Inventory, _>(InventoryError::PrerequisiteUnavailable("GPU observation"))
            },
            &mut failures,
            1,
            1,
        )
        .await;
    };
    let control = async {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
        while !attempted.get() {
            assert!(tokio::time::Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        // The claim lane completes while inventory is still retrying.
        true
    };
    let rotation = tokio::spawn(future::pending::<()>());
    let outcome = tokio::time::timeout(
        Duration::from_secs(1),
        supervise_lanes_with_rotation(control, inventory, rotation, future::pending::<()>()),
    )
    .await
    .unwrap();
    assert!(matches!(outcome, LaneExitWithRotation::Control(true)));
    let recovered =
        collect_inventory_until_ready(|| async { Ok(test_inventory()) }, &mut failures, 1, 1)
            .await
            .unwrap();
    assert_eq!(recovered.gpu_count, 1);
}

#[tokio::test(start_paused = true)]
async fn silent_inventory_probe_ends_within_budget_and_a_fresh_round_is_admitted() {
    let mut failures = 0;
    let ended = tokio::time::timeout(
        Duration::from_secs(301),
        collect_inventory_until_ready(
            future::pending::<Result<Inventory, InventoryError>>,
            &mut failures,
            1,
            1,
        ),
    )
    .await
    .unwrap();
    assert!(ended.is_err());
    let fresh =
        collect_inventory_until_ready(|| async { Ok(test_inventory()) }, &mut failures, 1, 1)
            .await
            .unwrap();
    assert_eq!(fresh.gpu_count, 1);
}

#[test]
fn startup_inventory_retry_backoff_is_bounded_by_agent_poll_limits() {
    for failure in [1, 2, 3, 20, u32::MAX] {
        let delay = inventory_retry_delay(failure, 2, 30);
        assert!(delay >= Duration::from_secs(2));
        assert!(delay <= Duration::from_secs(30));
    }
}

#[tokio::test(start_paused = true)]
async fn startup_inventory_retries_host_prerequisite_failures_until_recovery() {
    let attempts = Cell::new(0_u32);
    let mut failures = 0_u32;
    let inventory = collect_inventory_until_ready(
        || async {
            let attempt = attempts.get() + 1;
            attempts.set(attempt);
            if attempt <= 7 {
                Err(InventoryError::PrerequisiteUnavailable("Docker runtime"))
            } else {
                Ok(test_inventory())
            }
        },
        &mut failures,
        1,
        1,
    )
    .await
    .unwrap();

    assert_eq!(attempts.get(), 8);
    assert_eq!(failures, 7);
    assert_eq!(inventory.disk_available_bytes, 900);
}

#[test]
fn refused_observation_sweep_keeps_the_managed_run_cadence() {
    let refused = Err(RecipeObservationError::Report(ClientError::Protocol));
    assert!(!observation_snapshot_is_empty(&refused));
    assert_eq!(
        claim_wait_seconds(
            60,
            usize::from(!observation_snapshot_is_empty(&refused)),
            true
        ),
        10
    );
    let partial = Ok(vonk_agent::executor::RecipeObservationSweep {
        reported: 0,
        checkpoint: None,
        empty_snapshot_safe: false,
    });
    assert!(!observation_snapshot_is_empty(&partial));
    let complete = Ok(vonk_agent::executor::RecipeObservationSweep {
        reported: 0,
        checkpoint: None,
        empty_snapshot_safe: true,
    });
    assert!(observation_snapshot_is_empty(&complete));
}

#[test]
fn managed_runs_cap_claim_long_poll_at_ten_seconds() {
    assert_eq!(claim_wait_seconds(60, 1, true), 10);
    assert_eq!(claim_wait_seconds(7, 1, true), 7);
}

#[test]
fn no_managed_runs_retain_existing_claim_long_poll_behavior() {
    assert_eq!(claim_wait_seconds(300, 0, true), 60);
    assert_eq!(claim_wait_seconds(7, 0, true), 7);
}

#[test]
fn first_claim_returns_immediately_to_publish_controller_readiness() {
    assert_eq!(claim_wait_seconds(60, 0, false), 0);
    assert_eq!(claim_wait_seconds(60, 1, false), 0);
}

#[tokio::test]
async fn rotation_lane_failure_is_supervised_independently_of_control_lane() {
    let outcome = supervise_lanes_with_rotation(
        future::pending::<()>(),
        future::pending::<()>(),
        tokio::spawn(future::ready(Err::<(), _>("rotation failed"))),
        future::pending::<()>(),
    )
    .await;
    assert!(matches!(
        outcome,
        LaneExitWithRotation::Rotation(Ok(Err("rotation failed")))
    ));
}

#[test]
fn authenticated_identity_refusals_idle_control_without_replay() {
    use vonk_agent::executor::LoopError;
    for status in [401, 403] {
        assert!(loop_error_is_fatal(&LoopError::Client(
            ClientError::Controller(Box::new(ControllerError::from_status(status)))
        )));
    }
    assert!(loop_error_is_fatal(&LoopError::Client(ClientError::Pin)));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn spawned_rotation_progresses_while_control_thread_blocks() {
    let progressed = Arc::new(AtomicBool::new(false));
    let control_entered = Arc::new(AtomicBool::new(false));
    let barrier = Arc::new(Barrier::new(2));
    let rotation_progress = progressed.clone();
    let rotation_entered = control_entered.clone();
    let rotation_barrier = barrier.clone();
    let rotation = tokio::spawn(async move {
        while !rotation_entered.load(Ordering::SeqCst) {
            tokio::task::yield_now().await;
        }
        // The first barrier rendezvous proves control is blocked in its
        // synchronous section before rotation can make progress.
        rotation_barrier.wait();
        rotation_progress.store(true, Ordering::SeqCst);
        rotation_barrier.wait();
        future::pending::<Result<(), &'static str>>().await
    });
    let control_barrier = barrier.clone();
    let outcome = tokio::time::timeout(
        Duration::from_secs(1),
        supervise_lanes_with_rotation(
            async move {
                control_entered.store(true, Ordering::SeqCst);
                control_barrier.wait();
                control_barrier.wait();
                "control finished"
            },
            future::pending::<()>(),
            rotation,
            future::pending::<()>(),
        ),
    )
    .await
    .expect("independent rotation lane was starved by control");
    assert!(progressed.load(Ordering::SeqCst));
    assert!(matches!(
        outcome,
        LaneExitWithRotation::Control("control finished")
    ));
}
