//! Real checkpoint files and an injected driver prove restart, expiry and recovery.
use super::*;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use vonk_agent_protocol::generated::GpuClockRange;

#[derive(Clone, Default)]
struct Driver {
    fail: Arc<AtomicBool>,
    calls: Arc<Mutex<Vec<Vec<String>>>>,
}
impl CommandRunner for Driver {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new(NVIDIA_SMI));
        if arguments == [SUPPORT_QUERY, CSV_FORMAT] {
            return Ok(CommandOutput {
                success: true,
                stdout: b"300\n1800\n2200\n2300\n".to_vec(),
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
