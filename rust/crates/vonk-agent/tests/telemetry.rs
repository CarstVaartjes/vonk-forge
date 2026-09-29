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
        FileSystemCapacity, FileSystemProvider, TelemetryCollector, TelemetryPaths,
        valid_report_batch,
    },
};

type ProcessCall = (Program, Vec<String>, Duration);

#[derive(Clone)]
struct FakeRunner {
    calls: Arc<Mutex<Vec<ProcessCall>>>,
    output: Vec<u8>,
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
        Ok(ProcessOutput {
            success: true,
            stdout: self.output.clone(),
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
    let runner = runner(b"NVIDIA GB10, 25, [N/A], [N/A]\n");
    let collector = collector(directory.path(), MEMINFO, runner.clone());

    let sample = collector.sample_at(Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap());

    assert_eq!(sample.memory_total_bytes, Some(1_024_000));
    assert_eq!(sample.memory_available_bytes, Some(409_600));
    assert_eq!(sample.disk_total_bytes, Some(10_000));
    assert_eq!(sample.disk_free_bytes, Some(4_000));
    assert_eq!(sample.gpu_utilization_percent, Some(25.0));
    assert_eq!(sample.gpu_memory_total_bytes, None);
    assert_eq!(sample.gpu_memory_free_bytes, None);
    assert!(valid_report_batch(std::slice::from_ref(&sample)));
    let calls = runner.calls.lock().unwrap();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].0, Program::NvidiaSmi);
}

#[test]
fn dedicated_accelerator_reports_its_own_memory() {
    let directory = tempdir().unwrap();
    let collector = collector(
        directory.path(),
        MEMINFO,
        runner(b"NVIDIA H100, 50, 100, 90\n"),
    );

    let sample = collector.sample_at(Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap());

    assert_eq!(sample.gpu_utilization_percent, Some(50.0));
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
