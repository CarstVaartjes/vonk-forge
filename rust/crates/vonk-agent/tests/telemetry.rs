#![forbid(unsafe_code)]

use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, Instant as StdInstant},
};

use chrono::{DateTime, FixedOffset, TimeZone, Utc};
use serde_json::json;
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::{
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    state::{BeginDecision, StateStore},
    telemetry::{
        FileSystemCapacity, FileSystemProvider, TELEMETRY_STATE_FILENAME, TelemetryCollector,
        TelemetryError, TelemetryPaths, TelemetryQueue, TelemetrySchedule,
    },
};
use vonk_agent_protocol::{AgentClaim, canonical_json, hex_sha256};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";
type ProcessCall = (Program, Vec<String>, Duration);

#[derive(Clone)]
struct FakeRunner {
    calls: Arc<Mutex<Vec<ProcessCall>>>,
    output: Arc<Mutex<ProcessOutput>>,
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
        Ok(self.output.lock().unwrap().clone())
    }
}

#[derive(Clone, Copy)]
struct FakeFileSystem {
    capacity: FileSystemCapacity,
}

impl FileSystemProvider for FakeFileSystem {
    fn capacity(&self, _path: &Path) -> Result<FileSystemCapacity, rustix::io::Errno> {
        Ok(self.capacity)
    }
}

struct Fixtures {
    directory: tempfile::TempDir,
    stat: std::path::PathBuf,
    loadavg: std::path::PathBuf,
    meminfo: std::path::PathBuf,
    net_dev: std::path::PathBuf,
}

impl Fixtures {
    fn new() -> Self {
        let directory = tempdir().unwrap();
        StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
        let stat = directory.path().join("stat");
        let loadavg = directory.path().join("loadavg");
        let meminfo = directory.path().join("meminfo");
        let net_dev = directory.path().join("net-dev");
        fs::write(&stat, "cpu  100 0 50 850 0 0 0 0\n").unwrap();
        fs::write(&loadavg, "1.25 0.80 0.50 1/100 42\n").unwrap();
        fs::write(
            &meminfo,
            "MemTotal:       1000 kB\nMemAvailable:    400 kB\n",
        )
        .unwrap();
        fs::write(
            &net_dev,
            "Inter-| Receive | Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n  lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\neth0: 1000 1 0 0 0 0 0 0 2000 1 0 0 0 0 0 0\n",
        )
        .unwrap();
        Self {
            directory,
            stat,
            loadavg,
            meminfo,
            net_dev,
        }
    }

    fn paths(&self) -> TelemetryPaths {
        TelemetryPaths {
            stat: self.stat.clone(),
            loadavg: self.loadavg.clone(),
            meminfo: self.meminfo.clone(),
            net_dev: self.net_dev.clone(),
            store: self.directory.path().to_path_buf(),
        }
    }
}

fn runner(output: &[u8]) -> FakeRunner {
    FakeRunner {
        calls: Arc::new(Mutex::new(Vec::new())),
        output: Arc::new(Mutex::new(ProcessOutput {
            success: true,
            stdout: output.to_vec(),
            stderr: Vec::new(),
        })),
    }
}

fn boot_id() -> Uuid {
    Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap()
}

fn next_boot_id() -> Uuid {
    Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap()
}

fn claim() -> AgentClaim {
    let payload = json!({"run_id": "telemetry-lock-isolation"});
    AgentClaim {
        attempt: 1,
        authority_revision: "b".repeat(64),
        deadline: DateTime::<FixedOffset>::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
        node_id: NODE_ID.to_owned(),
        operation: "recipe.install".to_owned(),
        operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
        payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
        payload,
        schema_version: 1,
    }
}

fn basic_collector(
    fixtures: &Fixtures,
    boot_id: Uuid,
) -> TelemetryCollector<FakeRunner, FakeFileSystem> {
    TelemetryCollector::new(
        runner(b"NVIDIA H100, [N/A], 100, 90, [N/A], [N/A], [N/A]\n"),
        FakeFileSystem {
            capacity: FileSystemCapacity {
                total_bytes: 10_000,
                free_bytes: 4_000,
            },
        },
        fixtures.paths(),
        boot_id,
    )
    .unwrap()
}

fn set_next_unreserved_sequence(fixtures: &Fixtures, next: u64) {
    drop(basic_collector(fixtures, boot_id()));
    let connection =
        rusqlite::Connection::open(fixtures.directory.path().join(TELEMETRY_STATE_FILENAME))
            .unwrap();
    connection
        .execute(
            "UPDATE metadata SET value = ?1 WHERE key = 'telemetry_sequence_v1'",
            [json!({
                "boot_id": boot_id().to_string(),
                "next_unreserved_sequence": next,
            })
            .to_string()],
        )
        .unwrap();
}

#[test]
fn collects_literal_cpu_memory_disk_network_and_accelerator_metrics() {
    let fixtures = Fixtures::new();
    let runner = runner(b"NVIDIA GB10, 25, [N/A], [N/A], 45.5, 17.25, P0\n");
    let filesystem = FakeFileSystem {
        capacity: FileSystemCapacity {
            total_bytes: 10_000,
            free_bytes: 4_000,
        },
    };
    let mut collector =
        TelemetryCollector::new(runner.clone(), filesystem, fixtures.paths(), boot_id()).unwrap();
    let first_at = Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap();

    let first = collector.sample_at(None, first_at).unwrap();
    assert_eq!(first.sequence, 0);
    assert_eq!(first.cpu_utilization_percent, None);
    assert_eq!(first.load_average_1m, Some(1.25));
    assert_eq!(first.memory_total_bytes, Some(1_024_000));
    assert_eq!(first.memory_available_bytes, Some(409_600));
    assert_eq!(first.disk_total_bytes, Some(10_000));
    assert_eq!(first.disk_free_bytes, Some(4_000));
    assert_eq!(first.gpu_utilization_percent, Some(25.0));
    assert_eq!(first.gpu_memory_total_bytes, Some(1_024_000));
    assert_eq!(first.gpu_memory_free_bytes, Some(409_600));
    assert_eq!(first.temperature_c, Some(45.5));
    assert_eq!(first.power_watts, Some(17.25));
    assert_eq!(first.network_receive_bytes_per_second, None);
    assert_eq!(first.network_transmit_bytes_per_second, None);
    assert_eq!(
        first.details.accelerator_name.as_deref(),
        Some("NVIDIA GB10")
    );
    assert_eq!(
        first.details.accelerator_performance_state.as_deref(),
        Some("P0")
    );

    fs::write(&fixtures.stat, "cpu  140 0 70 890 0 0 0 0\n").unwrap();
    fs::write(
        &fixtures.net_dev,
        "Inter-| Receive | Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\neth0: 3000 1 0 0 0 0 0 0 2600 1 0 0 0 0 0 0\n",
    )
    .unwrap();
    let second = collector
        .sample_at(Some(&first), first_at + chrono::Duration::seconds(2))
        .unwrap();
    assert_eq!(second.sequence, 1);
    assert_eq!(second.cpu_utilization_percent, Some(60.0));
    assert_eq!(second.network_receive_bytes_per_second, Some(1_000.0));
    assert_eq!(second.network_transmit_bytes_per_second, Some(300.0));

    let calls = runner.calls.lock().unwrap();
    assert_eq!(calls.len(), 2);
    assert!(calls.iter().all(|call| call.0 == Program::NvidiaSmi));
    assert!(calls.iter().all(|call| call.2 == Duration::from_secs(10)));
    assert_eq!(
        calls[0].1,
        [
            "--query-gpu=name,utilization.gpu,memory.total,memory.free,temperature.gpu,power.draw,pstate",
            "--format=csv,noheader,nounits",
        ]
    );
}

#[test]
fn counter_resets_and_missing_sources_remain_unknown() {
    let fixtures = Fixtures::new();
    let runner = runner(b"NVIDIA H100, [N/A], 100, 90, [N/A], [N/A], [N/A]\n");
    let filesystem = FakeFileSystem {
        capacity: FileSystemCapacity {
            total_bytes: 10_000,
            free_bytes: 4_000,
        },
    };
    let mut collector =
        TelemetryCollector::new(runner, filesystem, fixtures.paths(), boot_id()).unwrap();
    let first_at = Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap();
    let first = collector.sample_at(None, first_at).unwrap();

    fs::write(&fixtures.stat, "cpu  10 0 5 85 0 0 0 0\n").unwrap();
    fs::write(
        &fixtures.net_dev,
        "Inter-| Receive | Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\neth0: 10 1 0 0 0 0 0 0 20 1 0 0 0 0 0 0\n",
    )
    .unwrap();
    let second = collector
        .sample_at(Some(&first), first_at + chrono::Duration::seconds(2))
        .unwrap();

    assert_eq!(second.cpu_utilization_percent, None);
    assert_eq!(second.network_receive_bytes_per_second, None);
    assert_eq!(second.network_transmit_bytes_per_second, None);
    assert_eq!(second.gpu_utilization_percent, None);
    assert_eq!(second.temperature_c, None);
    assert_eq!(second.power_watts, None);
    assert_eq!(second.details.accelerator_performance_state, None);
}

#[test]
fn cpu_total_excludes_guest_counters_already_in_user_and_nice() {
    let fixtures = Fixtures::new();
    fs::write(&fixtures.stat, "cpu  100 20 50 800 10 5 5 10 40 10\n").unwrap();
    let mut collector = TelemetryCollector::new(
        runner(b"NVIDIA H100, [N/A], 100, 90, [N/A], [N/A], [N/A]\n"),
        FakeFileSystem {
            capacity: FileSystemCapacity {
                total_bytes: 10_000,
                free_bytes: 4_000,
            },
        },
        fixtures.paths(),
        boot_id(),
    )
    .unwrap();
    let first_at = Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap();
    let first = collector.sample_at(None, first_at).unwrap();

    fs::write(&fixtures.stat, "cpu  140 30 70 840 10 5 5 10 90 40\n").unwrap();
    let second = collector
        .sample_at(Some(&first), first_at + chrono::Duration::seconds(2))
        .unwrap();

    let utilization = second.cpu_utilization_percent.unwrap();
    assert!((utilization - 63.636_363).abs() < 0.000_001);
}

#[test]
fn sequence_reservation_survives_same_boot_process_restart() {
    let fixtures = Fixtures::new();
    let filesystem = FakeFileSystem {
        capacity: FileSystemCapacity {
            total_bytes: 10_000,
            free_bytes: 4_000,
        },
    };
    let mut first_collector = TelemetryCollector::new(
        runner(b"NVIDIA H100, [N/A], 100, 90, [N/A], [N/A], [N/A]\n"),
        filesystem,
        fixtures.paths(),
        boot_id(),
    )
    .unwrap();
    let first = first_collector
        .sample_at(None, Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap())
        .unwrap();
    assert_eq!(first.sequence, 0);
    drop(first_collector);

    let mut restarted = TelemetryCollector::new(
        runner(b"NVIDIA H100, [N/A], 100, 90, [N/A], [N/A], [N/A]\n"),
        filesystem,
        fixtures.paths(),
        boot_id(),
    )
    .unwrap();
    let after_restart = restarted
        .sample_at(None, Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 2).unwrap())
        .unwrap();

    assert_eq!(after_restart.sequence, 64);
}

#[test]
fn sequence_reservation_crosses_from_63_to_64() {
    let fixtures = Fixtures::new();
    let mut collector = basic_collector(&fixtures, boot_id());
    let observed_at = Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap();

    for expected in 0..=64 {
        let sample = collector
            .sample_at(None, observed_at + chrono::Duration::milliseconds(expected))
            .unwrap();
        assert_eq!(sample.sequence, expected);
    }
}

#[test]
fn final_sequence_reservation_uses_a_partial_block() {
    let fixtures = Fixtures::new();
    let first = i64::MAX as u64 - 31;
    set_next_unreserved_sequence(&fixtures, first);
    let mut collector = basic_collector(&fixtures, boot_id());
    let observed_at = Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap();

    for offset in 0..32_u64 {
        let sample = collector
            .sample_at(
                None,
                observed_at + chrono::Duration::milliseconds(offset as i64),
            )
            .unwrap();
        assert_eq!(sample.sequence, (first + offset) as i64);
    }
    assert!(matches!(
        collector.sample_at(None, observed_at),
        Err(TelemetryError::SequenceExhausted)
    ));
}

#[test]
fn maximum_signed_sequence_is_emitted_once_then_exhausted() {
    let fixtures = Fixtures::new();
    set_next_unreserved_sequence(&fixtures, i64::MAX as u64);
    let mut collector = basic_collector(&fixtures, boot_id());
    let observed_at = Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap();

    assert_eq!(
        collector.sample_at(None, observed_at).unwrap().sequence,
        i64::MAX
    );
    assert!(matches!(
        collector.sample_at(None, observed_at),
        Err(TelemetryError::SequenceExhausted)
    ));
}

#[test]
fn sequence_reservation_resets_only_for_a_new_boot() {
    let fixtures = Fixtures::new();
    let filesystem = FakeFileSystem {
        capacity: FileSystemCapacity {
            total_bytes: 10_000,
            free_bytes: 4_000,
        },
    };
    let mut first_collector = TelemetryCollector::new(
        runner(b"NVIDIA H100, [N/A], 100, 90, [N/A], [N/A], [N/A]\n"),
        filesystem,
        fixtures.paths(),
        boot_id(),
    )
    .unwrap();
    first_collector
        .sample_at(None, Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap())
        .unwrap();
    drop(first_collector);

    let mut rebooted = TelemetryCollector::new(
        runner(b"NVIDIA H100, [N/A], 100, 90, [N/A], [N/A], [N/A]\n"),
        filesystem,
        fixtures.paths(),
        next_boot_id(),
    )
    .unwrap();
    let after_reboot = rebooted
        .sample_at(None, Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 2).unwrap())
        .unwrap();

    assert_eq!(after_reboot.sequence, 0);
}

#[test]
fn corrupt_sequence_metadata_fails_closed() {
    let fixtures = Fixtures::new();
    drop(basic_collector(&fixtures, boot_id()));
    let connection =
        rusqlite::Connection::open(fixtures.directory.path().join(TELEMETRY_STATE_FILENAME))
            .unwrap();
    connection
        .execute(
            "UPDATE metadata SET value = 'corrupt' WHERE key = 'telemetry_sequence_v1'",
            [],
        )
        .unwrap();
    drop(connection);

    let result = TelemetryCollector::new(
        runner(b"NVIDIA H100, [N/A], 100, 90, [N/A], [N/A], [N/A]\n"),
        FakeFileSystem {
            capacity: FileSystemCapacity {
                total_bytes: 10_000,
                free_bytes: 4_000,
            },
        },
        fixtures.paths(),
        boot_id(),
    );

    assert!(result.is_err());
}

#[test]
fn symlinked_sequence_database_fails_closed() {
    let target = Fixtures::new();
    drop(basic_collector(&target, boot_id()));
    let target_path = target.directory.path().join(TELEMETRY_STATE_FILENAME);
    let store = Fixtures::new();
    symlink(
        &target_path,
        store.directory.path().join(TELEMETRY_STATE_FILENAME),
    )
    .unwrap();

    let result = TelemetryCollector::new(
        runner(b"NVIDIA H100, [N/A], 100, 90, [N/A], [N/A], [N/A]\n"),
        FakeFileSystem {
            capacity: FileSystemCapacity {
                total_bytes: 10_000,
                free_bytes: 4_000,
            },
        },
        TelemetryPaths {
            stat: store.stat.clone(),
            loadavg: store.loadavg.clone(),
            meminfo: store.meminfo.clone(),
            net_dev: store.net_dev.clone(),
            store: store.directory.path().to_path_buf(),
        },
        boot_id(),
    );

    assert!(result.is_err());
}

#[test]
fn hardlinked_control_database_fails_closed() {
    let fixtures = Fixtures::new();
    fs::hard_link(
        fixtures.directory.path().join("state.sqlite"),
        fixtures.directory.path().join(TELEMETRY_STATE_FILENAME),
    )
    .unwrap();

    let result = TelemetryCollector::new(
        runner(b"NVIDIA H100, [N/A], 100, 90, [N/A], [N/A], [N/A]\n"),
        FakeFileSystem {
            capacity: FileSystemCapacity {
                total_bytes: 10_000,
                free_bytes: 4_000,
            },
        },
        fixtures.paths(),
        boot_id(),
    );

    assert!(result.is_err());
}

#[test]
fn telemetry_state_database_is_owner_only() {
    let fixtures = Fixtures::new();
    drop(basic_collector(&fixtures, boot_id()));

    let metadata = fs::metadata(fixtures.directory.path().join(TELEMETRY_STATE_FILENAME)).unwrap();
    assert_eq!(metadata.permissions().mode() & 0o777, 0o600);
}

#[test]
fn held_telemetry_write_lock_does_not_delay_control_state_transaction() {
    let fixtures = Fixtures::new();
    drop(basic_collector(&fixtures, boot_id()));
    let telemetry =
        rusqlite::Connection::open(fixtures.directory.path().join(TELEMETRY_STATE_FILENAME))
            .unwrap();
    telemetry.execute_batch("BEGIN IMMEDIATE").unwrap();

    let mut control =
        StateStore::open(&fixtures.directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let started = StdInstant::now();
    assert_eq!(
        control.begin(&claim(), Utc::now()).unwrap(),
        BeginDecision::Execute
    );
    assert!(started.elapsed() < Duration::from_millis(500));

    telemetry.execute_batch("ROLLBACK").unwrap();
}

#[test]
fn malformed_or_oversized_optional_evidence_is_bounded_to_null_metrics() {
    let fixtures = Fixtures::new();
    fs::write(&fixtures.stat, vec![b'x'; 65 * 1024]).unwrap();
    fs::write(&fixtures.loadavg, b"not-a-number\n").unwrap();
    fs::write(&fixtures.meminfo, b"MemFree: 5 kB\n").unwrap();
    fs::write(&fixtures.net_dev, b"malformed\n").unwrap();
    let runner = runner(&vec![b'x'; 65 * 1024]);
    let filesystem = FakeFileSystem {
        capacity: FileSystemCapacity {
            total_bytes: 10_000,
            free_bytes: 4_000,
        },
    };
    let mut collector =
        TelemetryCollector::new(runner, filesystem, fixtures.paths(), boot_id()).unwrap();

    let sample = collector
        .sample_at(None, Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap())
        .unwrap();

    assert_eq!(sample.cpu_utilization_percent, None);
    assert_eq!(sample.load_average_1m, None);
    assert_eq!(sample.memory_total_bytes, None);
    assert_eq!(sample.memory_available_bytes, None);
    assert_eq!(sample.network_receive_bytes_per_second, None);
    assert_eq!(sample.gpu_utilization_percent, None);
    assert_eq!(sample.details.accelerator_name, None);
}

#[test]
fn queue_keeps_fifteen_samples_and_preserves_exact_gap_on_the_oldest_retained() {
    let fixtures = Fixtures::new();
    let runner = runner(b"NVIDIA GB10, 25, [N/A], [N/A], 45.5, 17.25, P0\n");
    let filesystem = FakeFileSystem {
        capacity: FileSystemCapacity {
            total_bytes: 10_000,
            free_bytes: 4_000,
        },
    };
    let mut collector =
        TelemetryCollector::new(runner, filesystem, fixtures.paths(), boot_id()).unwrap();
    let started = Utc.with_ymd_and_hms(2026, 8, 15, 12, 0, 0).unwrap();
    let mut queue = TelemetryQueue::new();

    for offset in 0..17 {
        queue.push(
            collector
                .sample_at(None, started + chrono::Duration::seconds(offset * 2))
                .unwrap(),
        );
    }

    assert_eq!(queue.len(), 15);
    assert_eq!(queue.batch().first().unwrap().sequence, 2);
    assert_eq!(queue.batch().first().unwrap().gap_samples, 2);
    let retry_batch = queue.batch().to_vec();
    assert_eq!(queue.batch(), retry_batch);
    queue.acknowledge_prefix(4).unwrap();
    assert_eq!(queue.len(), 11);
    assert_eq!(queue.batch().first().unwrap().sequence, 6);
}

#[test]
fn scheduler_targets_two_seconds_without_artificial_catch_up() {
    let started = tokio::time::Instant::now();
    let mut schedule = TelemetrySchedule::new(started);
    assert!(schedule.collection_due(started));
    schedule.collected(started, started + Duration::from_millis(500));
    assert!(!schedule.collection_due(started + Duration::from_millis(1_999)));
    assert!(schedule.collection_due(started + Duration::from_secs(2)));

    let slow_started = started + Duration::from_secs(2);
    let slow_finished = slow_started + Duration::from_secs(5);
    schedule.collected(slow_started, slow_finished);
    assert!(!schedule.collection_due(slow_finished));
    assert!(!schedule.collection_due(started + Duration::from_millis(7_999)));
    assert!(schedule.collection_due(started + Duration::from_secs(8)));
}

#[test]
fn reporting_backoff_is_independent_and_success_removes_only_acknowledged_prefix() {
    let started = tokio::time::Instant::now();
    let mut schedule = TelemetrySchedule::new(started);
    schedule.collected(started, started);
    schedule.send_failed(started, Duration::from_secs(30));

    assert!(schedule.collection_due(started + Duration::from_secs(2)));
    assert!(!schedule.send_due(started + Duration::from_secs(29), true));
    assert!(schedule.send_due(started + Duration::from_secs(30), true));
    schedule.send_succeeded(started + Duration::from_secs(30));
    assert!(schedule.send_due(started + Duration::from_secs(30), true));
    assert!(!schedule.send_due(started + Duration::from_secs(30), false));
}
