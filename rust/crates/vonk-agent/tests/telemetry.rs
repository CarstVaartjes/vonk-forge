#![forbid(unsafe_code)]

use std::{
    fs,
    path::Path,
    sync::{Arc, Mutex},
    time::Duration,
};

use chrono::{TimeZone, Utc};
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::{
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    telemetry::{
        FileSystemCapacity, FileSystemProvider, GpuUnavailableReason, TelemetryCollector,
        TelemetryPaths, valid_report_batch,
    },
};

type ProcessCall = (Program, Vec<String>, Duration);

#[derive(Clone)]
struct FakeRunner {
    calls: Arc<Mutex<Vec<ProcessCall>>>,
    output: Vec<u8>,
    clock_output: Option<Vec<u8>>,
}

impl ProcessRunner for FakeRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.calls
            .lock()
            .unwrap()
            .push((program, arguments.to_vec(), timeout));
        let output =
            if arguments[0].contains("clocks.") || arguments[0].contains("clocks_event_reasons") {
                let fields = self.clock_output.as_ref().ok_or_else(|| {
                    ProcessError::Io(std::io::Error::other("clock query unsupported"))
                })?;
                let index = match arguments[0].as_str() {
                    "--query-gpu=clocks.current.graphics" => 0,
                    "--query-gpu=clocks.current.sm" => 1,
                    "--query-gpu=clocks.max.graphics" => 2,
                    "--query-gpu=clocks.max.sm" => 3,
                    "--query-gpu=clocks_event_reasons.active" => 4,
                    _ => panic!("unexpected query"),
                };
                String::from_utf8_lossy(fields)
                    .trim()
                    .split(',')
                    .nth(index)
                    .unwrap()
                    .trim()
                    .as_bytes()
                    .to_vec()
            } else {
                self.output.clone()
            };
        Ok(ProcessOutput {
            exit_code: None,
            success: true,
            stdout: output,
            stderr: Vec::new(),
        })
    }
}

#[derive(Clone, Copy)]
struct FakeFileSystem;

impl FileSystemProvider for FakeFileSystem {
    fn capacity(&self, _path: &Path) -> Result<FileSystemCapacity, rustix::io::Errno> {
        Ok(FileSystemCapacity {
            total_bytes: 10_000,
            free_bytes: 4_000,
        })
    }
}

fn runner(output: &[u8]) -> FakeRunner {
    FakeRunner {
        calls: Arc::new(Mutex::new(Vec::new())),
        output: output.to_vec(),
        clock_output: None,
    }
}

fn collector(
    directory: &Path,
    meminfo: &[u8],
    runner: FakeRunner,
) -> TelemetryCollector<FakeRunner, FakeFileSystem> {
    let path = directory.join("meminfo");
    fs::write(&path, meminfo).unwrap();
    TelemetryCollector::new(
        runner,
        FakeFileSystem,
        TelemetryPaths {
            meminfo: path,
            cpu_root: directory.join("cpu"),
            store: directory.to_path_buf(),
        },
        Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
    )
    .unwrap()
}

const MEMINFO: &[u8] = b"MemTotal:       1000 kB\nMemAvailable:    400 kB\n";

#[test]
fn unified_memory_accelerator_reports_host_memory_only() {
    let directory = tempdir().unwrap();
    let runner = runner(include_bytes!("fixtures/telemetry/gb10.csv"));
    let collector = collector(directory.path(), MEMINFO, runner.clone());

    let sample = collector.sample_at(Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap());

    assert_eq!(sample.memory_total_bytes, Some(1_024_000));
    assert_eq!(sample.memory_available_bytes, Some(409_600));
    assert_eq!(sample.disk_total_bytes, Some(10_000));
    assert_eq!(sample.disk_free_bytes, Some(4_000));
    assert_eq!(sample.gpu_utilization_percent, Some(25.0));
    assert_eq!(sample.gpu_temperature_c, Some(61));
    assert_eq!(sample.gpu_memory_total_bytes, None);
    assert_eq!(sample.gpu_memory_free_bytes, None);
    assert!(valid_report_batch(std::slice::from_ref(&sample)));
    let calls = runner.calls.lock().unwrap();
    assert!(calls.iter().all(|call| call.0 == Program::NvidiaSmi));
}

#[test]
fn dedicated_accelerator_reports_its_own_memory() {
    let directory = tempdir().unwrap();
    let collector = collector(
        directory.path(),
        MEMINFO,
        runner(b"NVIDIA H100, 50, 100, 90, [N/A]\n"),
    );

    let sample = collector.sample_at(Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap());

    assert_eq!(sample.gpu_utilization_percent, Some(50.0));
    assert_eq!(sample.gpu_temperature_c, None);
    assert_eq!(sample.gpu_memory_total_bytes, Some(100 * 1024 * 1024));
    assert_eq!(sample.gpu_memory_free_bytes, Some(90 * 1024 * 1024));
}

#[test]
fn malformed_or_oversized_sources_become_null_values() {
    let directory = tempdir().unwrap();
    let collector = collector(
        directory.path(),
        b"MemFree: 5 kB\n",
        runner(&vec![b'x'; 65 * 1024]),
    );

    let sample = collector.sample_at(Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap());

    assert_eq!(sample.memory_total_bytes, None);
    assert_eq!(sample.memory_available_bytes, None);
    assert_eq!(sample.gpu_utilization_percent, None);
    assert_eq!(sample.gpu_memory_total_bytes, None);
    assert_eq!(
        sample.gpu_unavailable_reason,
        Some(GpuUnavailableReason::GpuInvalidOutput)
    );
    assert!(valid_report_batch(std::slice::from_ref(&sample)));
}

#[test]
fn report_batches_must_be_ordered_and_bounded() {
    let directory = tempdir().unwrap();
    let collector = collector(directory.path(), MEMINFO, runner(b""));
    let first = collector.sample_at(Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap());
    let second = collector.sample_at(Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 2).unwrap());

    assert!(valid_report_batch(&[first.clone(), second.clone()]));
    assert!(!valid_report_batch(&[second, first.clone()]));
    assert!(!valid_report_batch(&[]));
    let mut inconsistent = first;
    inconsistent.memory_available_bytes = Some(u64::MAX);
    assert!(!valid_report_batch(&[inconsistent]));
}

fn cpu(directory: &Path, index: u32, current_khz: Option<&str>, max_khz: Option<&str>) {
    let cpufreq = directory
        .join("cpu")
        .join(format!("cpu{index}"))
        .join("cpufreq");
    fs::create_dir_all(&cpufreq).unwrap();
    if let Some(value) = current_khz {
        fs::write(cpufreq.join("scaling_cur_freq"), value).unwrap();
    }
    if let Some(value) = max_khz {
        fs::write(cpufreq.join("cpuinfo_max_freq"), value).unwrap();
    }
}

#[test]
fn cpu_frequency_is_summarized_from_sysfs_and_tolerates_missing_entries() {
    let directory = tempdir().unwrap();
    cpu(directory.path(), 0, Some("2000000\n"), Some("3900000\n"));
    cpu(directory.path(), 1, Some("2400000\n"), Some("3900000\n"));
    // No cpufreq readings, and unrelated entries, must not fail the sample.
    cpu(directory.path(), 2, None, None);
    cpu(directory.path(), 3, Some("garbage"), None);
    fs::create_dir_all(directory.path().join("cpu/cpufreq")).unwrap();
    let collector = collector(directory.path(), MEMINFO, runner(b""));

    let sample = collector.sample_at(Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap());

    assert_eq!(sample.cpu_frequency_avg_mhz, Some(2200));
    assert_eq!(sample.cpu_frequency_min_mhz, Some(2000));
    assert_eq!(sample.cpu_frequency_max_mhz, Some(3900));
    assert_eq!(sample.gpu_temperature_c, None);
    assert!(valid_report_batch(std::slice::from_ref(&sample)));
}

#[test]
fn cpu_frequency_is_null_without_cpufreq() {
    let directory = tempdir().unwrap();
    let collector = collector(directory.path(), MEMINFO, runner(b""));

    let sample = collector.sample_at(Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap());

    assert_eq!(sample.cpu_frequency_avg_mhz, None);
    assert_eq!(sample.cpu_frequency_min_mhz, None);
    assert_eq!(sample.cpu_frequency_max_mhz, None);
}

#[test]
fn unsupported_or_malformed_memory_preserves_gpu_sensors() {
    for fixture in [
        include_bytes!("fixtures/telemetry/gb10-memory-unsupported.csv").as_slice(),
        include_bytes!("fixtures/telemetry/dedicated-memory-malformed.csv").as_slice(),
    ] {
        let directory = tempdir().unwrap();
        let sample = collector(directory.path(), MEMINFO, runner(fixture)).sample();
        assert!(sample.gpu_utilization_percent.is_some());
        assert!(sample.gpu_temperature_c.is_some());
        assert_eq!(sample.gpu_memory_total_bytes, None);
        assert_eq!(sample.gpu_unavailable_reason, None);
        assert!(valid_report_batch(&[sample]));
    }
}

#[test]
fn unsupported_sensors_have_typed_reason_and_next_sample_recovers() {
    let directory = tempdir().unwrap();
    let sample = collector(
        directory.path(),
        MEMINFO,
        runner(include_bytes!("fixtures/telemetry/unsupported.csv")),
    )
    .sample();
    assert_eq!(
        sample.gpu_unavailable_reason,
        Some(GpuUnavailableReason::GpuUnsupportedMetrics)
    );
    let recovered = collector(
        directory.path(),
        MEMINFO,
        runner(include_bytes!("fixtures/telemetry/temperature-only.csv")),
    )
    .sample();
    assert_eq!(recovered.gpu_unavailable_reason, None);
    assert_eq!(recovered.gpu_temperature_c, Some(61));
    assert_eq!(recovered.gpu_utilization_percent, None);
    assert!(valid_report_batch(&[recovered]));
}

#[derive(Clone)]
struct RecoveringRunner {
    first: Arc<Mutex<bool>>,
    timeout: bool,
}

impl ProcessRunner for RecoveringRunner {
    fn run(
        &self,
        _: Program,
        _: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        assert_eq!(timeout, Duration::from_secs(10));
        let mut first = self.first.lock().unwrap();
        if *first {
            *first = false;
            if self.timeout {
                return Err(ProcessError::Timeout);
            }
            return Ok(ProcessOutput {
                exit_code: None,
                success: false,
                stdout: Vec::new(),
                stderr: b"Failed to initialize NVML: Insufficient Permissions".to_vec(),
            });
        }
        Ok(ProcessOutput {
            exit_code: None,
            success: true,
            stdout: include_bytes!("fixtures/telemetry/gb10.csv").to_vec(),
            stderr: Vec::new(),
        })
    }
}

#[test]
fn failed_query_is_typed_and_same_collector_recovers_without_latching() {
    for (timeout, reason) in [
        (false, GpuUnavailableReason::GpuCommandFailed),
        (true, GpuUnavailableReason::GpuCommandTimeout),
    ] {
        let directory = tempdir().unwrap();
        let meminfo = directory.path().join("meminfo");
        fs::write(&meminfo, MEMINFO).unwrap();
        let collector = TelemetryCollector::new(
            RecoveringRunner {
                first: Arc::new(Mutex::new(true)),
                timeout,
            },
            FakeFileSystem,
            TelemetryPaths {
                meminfo,
                cpu_root: directory.path().join("cpu"),
                store: directory.path().to_path_buf(),
            },
            Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
        )
        .unwrap();
        let failed = collector.sample();
        assert_eq!(failed.gpu_unavailable_reason, Some(reason));
        assert_eq!(failed.gpu_utilization_percent, None);
        assert_eq!(failed.memory_total_bytes, Some(1_024_000));
        let recovered = collector.sample();
        assert_eq!(recovered.gpu_unavailable_reason, None);
        assert_eq!(recovered.gpu_utilization_percent, Some(25.0));
        assert!(valid_report_batch(&[recovered]));
    }
}

#[test]
fn gpu_clocks_report_actual_maxima_and_preserve_the_throttle_mask_without_policy_clamping() {
    let directory = tempdir().unwrap();
    let mut runner = runner(include_bytes!("fixtures/telemetry/gb10.csv"));
    runner.clock_output = Some(b"1700, 1695, 2400, 2300, 0x0000000000000004\n".to_vec());
    let sample = collector(directory.path(), MEMINFO, runner)
        .sample_at(Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap());
    let clock = sample.gpu_clock.unwrap();
    assert_eq!(clock.graphics_mhz, Some(1700));
    assert_eq!(clock.sm_mhz, Some(1695));
    assert_eq!(clock.max_graphics_mhz, Some(2400));
    assert_eq!(clock.max_sm_mhz, Some(2300));
    assert_eq!(clock.throttle_reasons, Some(4));
    assert!(
        clock.locked_range.is_none(),
        "a hardware maximum must never be reported as a lock"
    );
}

#[test]
fn failed_gpu_clock_query_still_reports_an_observation() {
    // Catches losing the entire clock observation on command failure plus an absent receipt.
    let directory = tempdir().unwrap();
    let runner = runner(include_bytes!("fixtures/telemetry/gb10.csv"));
    let sample = collector(directory.path(), MEMINFO, runner).sample();
    assert!(
        sample.gpu_clock.is_some(),
        "the Controller must receive clock failure evidence even when every query fails"
    );
}
