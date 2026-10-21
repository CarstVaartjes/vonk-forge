#![cfg(test)]

use super::*;
use std::sync::Mutex;
use vonk_agent_protocol::generated::{
    GpuClockApplyState, GpuClockCheckpoint, GpuClockUnavailableReason,
};

struct Driver {
    readings: Vec<String>,
    calls: Mutex<usize>,
    fail: bool,
}
impl ProcessRunner for Driver {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _: Duration,
    ) -> Result<crate::process::ProcessOutput, ProcessError> {
        let mut calls = self.calls.lock().unwrap();
        assert_eq!(program, Program::NvidiaSmi);
        let index = match arguments[0].as_str() {
            "--query-gpu=clocks.current.graphics" => 0,
            "--query-gpu=clocks.current.sm" => 1,
            "--query-gpu=clocks.max.graphics" => 2,
            "--query-gpu=clocks.max.sm" => 3,
            "--query-gpu=clocks_event_reasons.active" => 4,
            _ => panic!("unexpected clock query: {arguments:?}"),
        };
        *calls += 1;
        Ok(crate::process::ProcessOutput {
            exit_code: Some(if self.fail { 1 } else { 0 }),
            success: !self.fail,
            stdout: self.readings[index].as_bytes().to_vec(),
            stderr: if self.fail {
                b"injected driver failure".to_vec()
            } else {
                vec![]
            },
        })
    }
}
fn vector() -> serde_json::Value {
    serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/gpu-clock-v1.json"
    ))
    .unwrap()
}
fn driver() -> Driver {
    Driver {
        readings: serde_json::from_value(vector()["driver_readings"].clone()).unwrap(),
        calls: Mutex::new(0),
        fail: false,
    }
}
fn checkpoint(boot: &Uuid) -> GpuClockCheckpoint {
    GpuClockCheckpoint {
        desired: serde_json::from_value(vector()["observation"]["desired"].clone()).unwrap(),
        state: GpuClockApplyState::Applied,
        detail: None,
        next_attempt_at: None,
        deadline: 1600,
        applied_boot_id: Some(boot.to_string()),
        applied_range: Some(vonk_agent_protocol::gpu_clock::default_range()),
    }
}
#[test]
fn driver_and_helper_receipt_produce_the_shared_controller_cli_observation() {
    // Catches dropping the receipt, substituting maxima for a lock, or not sampling each tick.
    let root = tempfile::tempdir().unwrap();
    let boot = Uuid::new_v4();
    fs_write_receipt(root.path(), &checkpoint(&boot));
    let driver = driver();
    for _ in 0..2 {
        let actual = collect_gpu_clock(&driver, &boot, root.path());
        assert_eq!(
            serde_json::to_value(actual).unwrap(),
            vector()["observation"]
        );
    }
    assert_eq!(*driver.calls.lock().unwrap(), 10);
}
fn fs_write_receipt(root: &Path, receipt: &GpuClockCheckpoint) {
    std::fs::write(
        root.join("gpu-clock-observation.json"),
        vonk_agent_protocol::canonical_json(receipt).unwrap(),
    )
    .unwrap();
}
#[test]
fn failed_query_and_missing_receipt_are_reported_then_recover_after_apply() {
    // Main returns None for this fault, silently hiding both causes from the Controller.
    let root = tempfile::tempdir().unwrap();
    let boot = Uuid::new_v4();
    let mut driver = driver();
    driver.fail = true;
    let failed = collect_gpu_clock(&driver, &boot, root.path());
    assert!(failed.receipt_detail.unwrap().contains("No such file"));
    assert!(
        failed
            .unavailable
            .unwrap()
            .iter()
            .all(
                |failure| failure.reason == GpuClockUnavailableReason::CommandFailed
                    && failure.detail.contains("injected driver failure")
            )
    );
    fs_write_receipt(root.path(), &checkpoint(&boot));
    driver.fail = false;
    let recovered = collect_gpu_clock(&driver, &boot, root.path());
    assert_eq!(
        serde_json::to_value(recovered).unwrap(),
        vector()["observation"]
    );
    let rebooted = collect_gpu_clock(&driver, &Uuid::new_v4(), root.path());
    assert!(rebooted.locked_range.is_none());
    assert!(rebooted.receipt_detail.unwrap().contains("this boot"));
}
#[test]
fn unsupported_or_invalid_reading_does_not_hide_other_clocks_or_apply_receipt() {
    // Main turns N/A into an unexplained None and one failed query loses all fields.
    let root = tempfile::tempdir().unwrap();
    let boot = Uuid::new_v4();
    fs_write_receipt(root.path(), &checkpoint(&boot));
    let mut driver = driver();
    driver.readings[1] = "[N/A]".to_owned();
    driver.readings[4] = "broken".to_owned();
    let actual = collect_gpu_clock(&driver, &boot, root.path());
    assert_eq!(actual.graphics_mhz, Some(1700));
    assert_eq!(
        actual.locked_range,
        Some(vonk_agent_protocol::gpu_clock::default_range())
    );
    let failures = actual.unavailable.unwrap();
    assert_eq!(failures[0].reason, GpuClockUnavailableReason::NotSupported);
    assert_eq!(failures[1].reason, GpuClockUnavailableReason::InvalidOutput);
}
