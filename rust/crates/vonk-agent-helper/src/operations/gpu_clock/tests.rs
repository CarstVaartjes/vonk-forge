//! Real checkpoint files and an injected driver prove restart, expiry and recovery.
use super::*;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};

#[derive(Clone, Default)]
struct Driver {
    fail: Arc<AtomicBool>,
    no_support: Arc<AtomicBool>,
    observation: Arc<Mutex<Option<Vec<u8>>>>,
    reject: Arc<Mutex<Option<i32>>>,
    calls: Arc<Mutex<Vec<Vec<String>>>>,
}
impl CommandRunner for Driver {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new(NVIDIA_SMI));
        if arguments == [vonk_agent_protocol::gpu_clock::SUPPORT_QUERY, CSV_FORMAT] {
            return Ok(CommandOutput {
                success: true,
                stdout: if self.no_support.load(Ordering::SeqCst) {
                    b"[N/A]\n".to_vec()
                } else {
                    b"300\n1800\n2200\n2300\n".to_vec()
                },
                stderr: vec![],
                exit_code: Some(0),
            });
        }
        if arguments == [OBSERVATION_QUERY, CSV_FORMAT] {
            return Ok(CommandOutput {
                success: true,
                stdout: self
                    .observation
                    .lock()
                    .unwrap()
                    .clone()
                    .unwrap_or_else(|| b"305, 305, 0x0\n".to_vec()),
                stderr: vec![],
                exit_code: Some(0),
            });
        }
        assert_eq!(arguments.len(), 2);
        assert_eq!(arguments[0], "-lgc");
        assert!(
            arguments[1]
                .bytes()
                .all(|b| b.is_ascii_digit() || b == b',')
        );
        self.calls.lock().unwrap().push(arguments.to_vec());
        if let Some(code) = *self.reject.lock().unwrap() {
            return Ok(CommandOutput {
                success: false,
                stdout: vec![],
                stderr: b"injected driver refusal".to_vec(),
                exit_code: Some(code),
            });
        }
        if self.fail.load(Ordering::SeqCst) {
            return Err("injected driver outage".to_owned());
        }
        Ok(CommandOutput {
            success: true,
            stdout: vec![],
            stderr: vec![],
            exit_code: Some(0),
        })
    }
}
fn helper(root: &Path, driver: &Driver) -> OperationExecutor<Driver> {
    OperationExecutor::new(ManagedRoots::under(root), &[0; 32], driver.clone(), None).unwrap()
}
fn intent(generation: u64, maximum: u32) -> GpuClockIntent {
    GpuClockIntent {
        generation,
        range: GpuClockRange {
            max_mhz: maximum,
            ..default_range()
        },
    }
}
fn checkpoint(root: &Path) -> GpuClockCheckpoint {
    parse_strict(&fs::read(root.join("gpu-clock").join(CHECKPOINT_FILE)).unwrap()).unwrap()
}
#[test]
fn failed_apply_survives_restart_without_extending_deadline_then_recovers() {
    let temp = tempfile::tempdir().unwrap();
    let driver = Driver::default();
    driver.fail.store(true, Ordering::SeqCst);
    let desired = intent(1, 1800);
    helper(temp.path(), &driver)
        .reconcile_gpu_clock_at(Some(&desired), false, 1000)
        .unwrap();
    let waiting = checkpoint(temp.path());
    assert_eq!(waiting.state, GpuClockApplyState::Waiting);
    assert_eq!(waiting.deadline, 1600);
    assert_eq!(waiting.next_attempt_at, Some(1060));
    assert!(waiting.applied_range.is_none());
    let restarted = helper(temp.path(), &driver);
    restarted
        .reconcile_gpu_clock_at(Some(&desired), false, 1020)
        .unwrap();
    assert_eq!(driver.calls.lock().unwrap().len(), 1);
    assert_eq!(checkpoint(temp.path()).deadline, 1600);
    driver.fail.store(false, Ordering::SeqCst);
    restarted
        .reconcile_gpu_clock_at(Some(&desired), false, 1060)
        .unwrap();
    assert_eq!(checkpoint(temp.path()).state, GpuClockApplyState::Applied);
    assert_eq!(
        driver.calls.lock().unwrap().last().unwrap(),
        &["-lgc", "300,1800"]
    );
}
#[test]
fn hourly_enforcement_keeps_active_range_and_newer_intent_restores_default() {
    let temp = tempfile::tempdir().unwrap();
    let driver = Driver::default();
    let executor = helper(temp.path(), &driver);
    executor
        .reconcile_gpu_clock_at(Some(&intent(2, 1800)), false, 1000)
        .unwrap();
    executor
        .reconcile_gpu_clock_at(Some(&intent(1, 2200)), false, 1001)
        .unwrap();
    assert_eq!(
        driver.calls.lock().unwrap().len(),
        1,
        "stale request changed latest intent"
    );
    // An external change cannot be read back; a normal hourly pass reasserts it.
    executor.reconcile_gpu_clock_at(None, true, 4600).unwrap();
    assert_eq!(
        driver.calls.lock().unwrap().last().unwrap(),
        &["-lgc", "300,1800"]
    );
    executor
        .reconcile_gpu_clock_at(Some(&intent(3, 2200)), false, 4601)
        .unwrap();
    assert_eq!(checkpoint(temp.path()).applied_range, Some(default_range()));
}
#[test]
fn expiry_is_visible_and_polls_do_not_renew_it_but_hourly_reconciliation_recovers() {
    let temp = tempfile::tempdir().unwrap();
    let driver = Driver::default();
    driver.fail.store(true, Ordering::SeqCst);
    let executor = helper(temp.path(), &driver);
    let desired = intent(1, 1800);
    executor
        .reconcile_gpu_clock_at(Some(&desired), false, 1000)
        .unwrap();
    executor
        .reconcile_gpu_clock_at(Some(&desired), false, 1601)
        .unwrap();
    assert_eq!(checkpoint(temp.path()).state, GpuClockApplyState::Expired);
    let expired_detail = checkpoint(temp.path()).detail;
    assert!(
        expired_detail
            .as_deref()
            .unwrap()
            .contains("injected driver outage")
    );
    executor
        .reconcile_gpu_clock_at(Some(&desired), false, 1700)
        .unwrap();
    assert_eq!(driver.calls.lock().unwrap().len(), 1);
    assert_eq!(checkpoint(temp.path()).detail, expired_detail);
    driver.fail.store(false, Ordering::SeqCst);
    executor.reconcile_gpu_clock_at(None, true, 4600).unwrap();
    assert_eq!(checkpoint(temp.path()).state, GpuClockApplyState::Applied);
}
#[test]
fn invalid_ranges_and_conflicting_generation_never_reach_the_command() {
    let temp = tempfile::tempdir().unwrap();
    let driver = Driver::default();
    let executor = helper(temp.path(), &driver);
    for desired in [
        intent(1, 2301),
        GpuClockIntent {
            generation: 1,
            range: GpuClockRange {
                min_mhz: 2000,
                max_mhz: 1800,
            },
        },
        intent(1, 0),
    ] {
        assert!(
            executor
                .reconcile_gpu_clock_at(Some(&desired), false, 1000)
                .is_err()
        );
    }
    assert!(driver.calls.lock().unwrap().is_empty());
    executor
        .reconcile_gpu_clock_at(Some(&intent(1, 1800)), false, 1000)
        .unwrap();
    assert!(
        executor
            .reconcile_gpu_clock_at(Some(&intent(1, 2200)), false, 1001)
            .is_err()
    );
    assert_eq!(driver.calls.lock().unwrap().len(), 1);
}

#[test]
fn helper_receipt_remains_readable_under_service_umask() {
    // Change umask only in a dedicated child, never other concurrently run tests.
    const CHILD: &str = "VONK_GPU_CLOCK_UMASK_CHILD";
    if std::env::var_os(CHILD).is_some() {
        let _previous_mask =
            rustix::process::umask(rustix::fs::Mode::RWXG | rustix::fs::Mode::RWXO);
        let temp = tempfile::tempdir().unwrap();
        let driver = Driver::default();
        helper(temp.path(), &driver)
            .reconcile_gpu_clock_at(Some(&intent(1, 1800)), false, 1000)
            .unwrap();
        assert_eq!(
            fs::metadata(temp.path().join("gpu-clock-observation.json"))
                .unwrap()
                .mode()
                & 0o777,
            0o644
        );
        assert_eq!(
            fs::metadata(temp.path().join("gpu-clock").join(CHECKPOINT_FILE))
                .unwrap()
                .mode()
                & 0o777,
            0o600
        );
        return;
    }
    assert!(
        std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "operations::gpu_clock::tests::helper_receipt_remains_readable_under_service_umask",
                "--test-threads=1"
            ])
            .env(CHILD, "1")
            .status()
            .unwrap()
            .success()
    );
}

#[test]
fn process_runner_rejects_broader_nvidia_commands_before_spawn() {
    // This must refuse before looking for a real NVIDIA binary or GPU.
    for args in [
        vec!["-lgc", "300,2301"],
        vec!["-lgc", "2200,300"],
        vec!["-lgc", "-1,2200"],
        vec!["-lgc", "300.0,2200"],
        vec!["-lgc", "300,2200", "--reset-gpu-clocks"],
        vec!["--reset-gpu-clocks"],
        vec!["--query-gpu=uuid", CSV_FORMAT],
        vec!["-lgc", "300,2200;id"],
    ] {
        let args = args.into_iter().map(str::to_owned).collect::<Vec<_>>();
        assert_eq!(
            ProcessCommandRunner.run(Path::new(NVIDIA_SMI), &args),
            Err("GPU clock command is not compiled into the helper".to_owned()),
            "accepted broader NVIDIA arguments: {args:?}"
        );
    }
}

#[test]
fn command_allowlist_accepts_only_the_fixed_query_and_canonical_safe_lock() {
    assert!(allowed_arguments(&[
        OBSERVATION_QUERY.to_owned(),
        CSV_FORMAT.to_owned()
    ]));
    for range in [
        default_range(),
        GpuClockRange {
            min_mhz: 0,
            max_mhz: 1800,
        },
        GpuClockRange {
            min_mhz: 1,
            max_mhz: 2300,
        },
        GpuClockRange {
            min_mhz: 2300,
            max_mhz: 2300,
        },
    ] {
        assert!(allowed_arguments(&lock_arguments(&range).unwrap()));
    }
    assert!(!allowed_arguments(&[
        "-lgc".to_owned(),
        "0300,2200".to_owned()
    ]));
    assert!(!allowed_arguments(&[
        "-lgc".to_owned(),
        "+300,2200".to_owned()
    ]));
    // A caller cannot substitute a PATH lookup or another NVIDIA executable.
    assert_eq!(
        ProcessCommandRunner.run(
            Path::new("nvidia-smi"),
            &["-lgc".to_owned(), "300,2200".to_owned()]
        ),
        Err("executable is not compiled into the helper".to_owned())
    );
}

#[test]
fn gb10_without_supported_levels_applies_default_and_custom_without_systemd_or_workload_effects() {
    // Fails if an empty driver capability list gates a valid clock command.
    let temp = tempfile::tempdir().unwrap();
    let driver = Driver::default();
    driver.no_support.store(true, Ordering::SeqCst);
    let executor = helper(temp.path(), &driver);
    executor.reconcile_gpu_clock_at(None, true, 1000).unwrap();
    assert_eq!(checkpoint(temp.path()).state, GpuClockApplyState::Applied);
    executor
        .reconcile_gpu_clock_at(Some(&intent(1, 1800)), false, 1001)
        .unwrap();
    assert_eq!(
        checkpoint(temp.path()).applied_range,
        Some(intent(1, 1800).range)
    );
    assert_eq!(
        *driver.calls.lock().unwrap(),
        vec![vec!["-lgc", "300,2200"], vec!["-lgc", "300,1800"]]
    );
}

#[test]
fn reboot_invalidates_the_receipt_and_reapplies_the_active_range() {
    // Fails if an applied receipt from a previous boot suppresses the command.
    let temp = tempfile::tempdir().unwrap();
    let driver = Driver::default();
    let executor = helper(temp.path(), &driver);
    let desired = intent(1, 1800);
    executor
        .reconcile_gpu_clock_at(Some(&desired), false, 1000)
        .unwrap();
    let mut old = checkpoint(temp.path());
    old.applied_boot_id = Some("previous-boot".to_owned());
    let root = temp.path().join("gpu-clock");
    executor
        .write_gpu_clock(&root, &root.join(CHECKPOINT_FILE), &old)
        .unwrap();
    helper(temp.path(), &driver)
        .reconcile_gpu_clock_at(Some(&desired), false, 2000)
        .unwrap();
    let reapplied = checkpoint(temp.path());
    assert_eq!(reapplied.state, GpuClockApplyState::Applied);
    assert_ne!(reapplied.applied_boot_id, old.applied_boot_id);
    assert_eq!(reapplied.applied_range, Some(desired.range));
    assert_eq!(driver.calls.lock().unwrap().len(), 2);
}

#[test]
fn successful_command_with_missing_or_excessive_observation_waits_and_recovers() {
    // A zero exit status without corroborating evidence must not become Applied.
    for reading in [
        b"[N/A], 305, 0x0\n".as_slice(),
        b"2200, 2200, 0x0\n",
        b"305, 305, [N/A]\n",
    ] {
        let temp = tempfile::tempdir().unwrap();
        let driver = Driver::default();
        *driver.observation.lock().unwrap() = Some(reading.to_vec());
        let desired = intent(1, 1800);
        let executor = helper(temp.path(), &driver);
        executor
            .reconcile_gpu_clock_at(Some(&desired), false, 1000)
            .unwrap();
        let waiting = checkpoint(temp.path());
        assert_eq!(waiting.state, GpuClockApplyState::Waiting);
        assert!(waiting.applied_range.is_none());
        assert_eq!(waiting.next_attempt_at, Some(1060));
        assert!(
            waiting
                .detail
                .unwrap()
                .contains("observation is unconfirmed")
        );
        *driver.observation.lock().unwrap() = None;
        executor
            .reconcile_gpu_clock_at(Some(&desired), false, 1060)
            .unwrap();
        assert_eq!(checkpoint(temp.path()).state, GpuClockApplyState::Applied);
    }
}

#[test]
fn deterministic_driver_refusals_do_not_loop_but_new_intent_can_retry() {
    // A denied/unsupported command is not a recoverable driver outage.
    for code in [2, 3, 4] {
        let temp = tempfile::tempdir().unwrap();
        let driver = Driver::default();
        *driver.reject.lock().unwrap() = Some(code);
        let executor = helper(temp.path(), &driver);
        let desired = intent(1, 1800);
        executor
            .reconcile_gpu_clock_at(Some(&desired), false, 1000)
            .unwrap();
        let refused = checkpoint(temp.path());
        assert_eq!(refused.state, GpuClockApplyState::Refused);
        assert!(refused.next_attempt_at.is_none());
        assert!(refused.detail.unwrap().contains("injected driver refusal"));
        executor
            .reconcile_gpu_clock_at(Some(&desired), false, 1100)
            .unwrap();
        assert_eq!(driver.calls.lock().unwrap().len(), 1);
        *driver.reject.lock().unwrap() = None;
        executor
            .reconcile_gpu_clock_at(Some(&intent(2, 1800)), false, 1101)
            .unwrap();
        assert_eq!(checkpoint(temp.path()).state, GpuClockApplyState::Applied);
    }
}

#[test]
fn oversized_observation_cannot_poison_the_durable_retry_record() {
    // Raw driver output in a parse error must not exceed the record read bound.
    let temp = tempfile::tempdir().unwrap();
    let driver = Driver::default();
    *driver.observation.lock().unwrap() = Some(vec![b'9'; RECORD_BYTES as usize + 1]);
    let desired = intent(1, 1800);
    helper(temp.path(), &driver)
        .reconcile_gpu_clock_at(Some(&desired), false, 1000)
        .unwrap();
    let waiting = checkpoint(temp.path());
    assert_eq!(waiting.state, GpuClockApplyState::Waiting);
    assert!(waiting.detail.unwrap().contains("observation exceeds"));
    *driver.observation.lock().unwrap() = None;
    helper(temp.path(), &driver)
        .reconcile_gpu_clock_at(Some(&desired), false, 1060)
        .unwrap();
    assert_eq!(checkpoint(temp.path()).state, GpuClockApplyState::Applied);
}
