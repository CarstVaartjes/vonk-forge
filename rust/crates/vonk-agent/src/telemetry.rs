use std::{
    collections::{BTreeMap, VecDeque},
    fs::{self, File, OpenOptions},
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use uuid::Uuid;

use crate::process::{ProcessRunner, Program};

const SOURCE_TEXT_LIMIT: u64 = 64 * 1024;
const MAX_CAPACITY_BYTES: u64 = 16 * 1024_u64.pow(4);
const MAX_QUEUE_SAMPLES: usize = 15;
const SEQUENCE_RESERVATION_SIZE: u64 = 64;
const SEQUENCE_STATE_KEY: &str = "telemetry_sequence_v1";
const SEQUENCE_LIMIT_EXCLUSIVE: u64 = i64::MAX as u64 + 1;
pub const TELEMETRY_STATE_FILENAME: &str = "telemetry-state.sqlite";
pub const MAX_REPORT_SAMPLES: usize = 16;
pub const COLLECTION_INTERVAL: Duration = Duration::from_secs(2);
const MAX_ACCELERATORS: usize = 16;
const MAX_STORAGE_DEVICES: usize = 32;
const MAX_NETWORK_INTERFACES: usize = 32;
const MAX_GPU_PROCESSES: usize = 5;
const MAX_METRIC_SERIES: usize = 512;
const MAX_CAPABILITIES: usize = 128;

#[derive(Debug, Error)]
pub enum TelemetryError {
    #[error("telemetry boot identity is invalid")]
    InvalidBootId,
    #[error("telemetry sequence is exhausted")]
    SequenceExhausted,
    #[error("telemetry sequence state is invalid")]
    InvalidSequenceState,
    #[error("telemetry sequence state database failed")]
    Database(#[from] rusqlite::Error),
    #[error("telemetry sequence state file is unsafe")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct TelemetryDetails {
    pub accelerator_name: Option<String>,
    pub accelerator_performance_state: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TelemetrySeries {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub node_id: Option<String>,
    pub key: String,
    pub scope: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub device_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub process_id: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub process_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub interface_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
    pub value: serde_json::Value,
    pub unit: String,
    pub source: String,
    pub measurement_kind: String,
    pub observed_at: DateTime<Utc>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub received_at: Option<DateTime<Utc>>,
    pub freshness: String,
    pub freshness_threshold_seconds: f64,
    pub support_status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    pub aggregation: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TelemetryCapability {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub node_id: Option<String>,
    pub key: String,
    pub scope: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub device_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub process_id: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub process_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub interface_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
    pub unit: String,
    pub source: String,
    pub measurement_kind: String,
    pub supported: bool,
    pub freshness_threshold_seconds: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TelemetryProvenance {
    pub collector: String,
    pub collector_version: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub host_uptime_seconds: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_observed_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TelemetryRuntime {
    pub run_id: String,
    pub engine_id: String,
    pub backend: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub endpoint: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model_version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recipe_revision: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context_limit_tokens: Option<u64>,
    pub serving_node_ids: Vec<String>,
    pub ranks: Vec<u32>,
    pub readiness: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub adapter: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub adapter_version: Option<String>,
    pub adapter_supported: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub adapter_reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TelemetryWorkload {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub job_id: Option<String>,
    pub run_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recipe_revision: Option<String>,
    pub engine_id: String,
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub origin_node_id: Option<String>,
    pub executor_node_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub started_at: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ended_at: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub elapsed_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failure: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub progress_value: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub progress_max: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub eta_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub eta_source: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TelemetryMetrics {
    pub schema_version: u8,
    pub series: Vec<TelemetrySeries>,
    pub capabilities: Vec<TelemetryCapability>,
    pub runtimes: Vec<TelemetryRuntime>,
    pub workloads: Vec<TelemetryWorkload>,
    pub provenance: TelemetryProvenance,
}

impl Default for TelemetryMetrics {
    fn default() -> Self {
        Self {
            schema_version: 2,
            series: Vec::new(),
            capabilities: Vec::new(),
            runtimes: Vec::new(),
            workloads: Vec::new(),
            provenance: TelemetryProvenance {
                collector: "legacy".to_owned(),
                collector_version: "1".to_owned(),
                host_uptime_seconds: None,
                source_observed_at: None,
            },
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TelemetrySample {
    pub boot_id: Uuid,
    pub sequence: i64,
    pub observed_at: DateTime<Utc>,
    pub cpu_utilization_percent: Option<f64>,
    pub load_average_1m: Option<f64>,
    pub memory_total_bytes: Option<u64>,
    pub memory_available_bytes: Option<u64>,
    pub disk_total_bytes: Option<u64>,
    pub disk_free_bytes: Option<u64>,
    pub gpu_utilization_percent: Option<f64>,
    pub gpu_memory_total_bytes: Option<u64>,
    pub gpu_memory_free_bytes: Option<u64>,
    pub temperature_c: Option<f64>,
    pub power_watts: Option<f64>,
    pub network_receive_bytes_per_second: Option<f64>,
    pub network_transmit_bytes_per_second: Option<f64>,
    pub gap_samples: i64,
    pub details: TelemetryDetails,
    #[serde(default, skip_serializing_if = "TelemetryMetrics::is_empty")]
    pub metrics: TelemetryMetrics,
    #[serde(skip)]
    cpu_counters: Option<CpuCounters>,
    #[serde(skip)]
    network_counters: Option<NetworkCounters>,
}

impl TelemetryMetrics {
    fn is_empty(value: &Self) -> bool {
        value.series.is_empty()
            && value.capabilities.is_empty()
            && value.runtimes.is_empty()
            && value.workloads.is_empty()
            && value.provenance.collector == "legacy"
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct CpuCounters {
    total: u64,
    idle: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct NetworkCounters {
    receive: u64,
    transmit: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct NetworkInterfaceCounters {
    receive: u64,
    transmit: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct DiskCounters {
    read_bytes: u64,
    write_bytes: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct EnergyCounter {
    microjoules: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FileSystemCapacity {
    pub total_bytes: u64,
    pub free_bytes: u64,
}

pub trait FileSystemProvider {
    fn capacity(&self, path: &Path) -> Result<FileSystemCapacity, rustix::io::Errno>;
}

#[derive(Debug, Clone, Copy, Default)]
pub struct SystemFileSystemProvider;

impl FileSystemProvider for SystemFileSystemProvider {
    fn capacity(&self, path: &Path) -> Result<FileSystemCapacity, rustix::io::Errno> {
        let filesystem = rustix::fs::statvfs(path)?;
        Ok(FileSystemCapacity {
            total_bytes: filesystem.f_blocks.saturating_mul(filesystem.f_frsize),
            free_bytes: filesystem.f_bavail.saturating_mul(filesystem.f_frsize),
        })
    }
}

#[derive(Debug, Clone)]
pub struct TelemetryPaths {
    pub stat: PathBuf,
    pub loadavg: PathBuf,
    pub uptime: PathBuf,
    pub meminfo: PathBuf,
    pub net_dev: PathBuf,
    pub store: PathBuf,
    pub sys_block: PathBuf,
    pub sys_class_net: PathBuf,
    pub thermal: PathBuf,
    pub powercap: PathBuf,
}

pub struct TelemetryCollector<R, F> {
    runner: R,
    filesystem: F,
    paths: TelemetryPaths,
    boot_id: Uuid,
    sequences: DurableSequenceAllocator,
    disk_counters: BTreeMap<String, DiskCounters>,
    interface_counters: BTreeMap<String, NetworkInterfaceCounters>,
    cpu_power_counter: Option<EnergyCounter>,
    runtime_counters: BTreeMap<String, f64>,
    last_observed_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StoredSequenceReservation {
    boot_id: String,
    next_unreserved_sequence: u64,
}

struct DurableSequenceAllocator {
    connection: Connection,
    boot_id: Uuid,
    next_sequence: u64,
    reserved_until: u64,
}

impl DurableSequenceAllocator {
    fn open(path: &Path, boot_id: Uuid) -> Result<Self, TelemetryError> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        match OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
        {
            Ok(file) => drop(file),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error.into()),
        }
        let metadata = fs::symlink_metadata(path)?;
        if metadata.file_type().is_symlink()
            || !metadata.file_type().is_file()
            || metadata.nlink() != 1
        {
            return Err(TelemetryError::InvalidSequenceState);
        }
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
        let mut connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
        connection.busy_timeout(Duration::from_secs(1))?;
        connection.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=FULL;
             PRAGMA foreign_keys=ON;
             PRAGMA trusted_schema=OFF;
             CREATE TABLE IF NOT EXISTS metadata (
               key TEXT PRIMARY KEY NOT NULL,
               value TEXT NOT NULL
             ) STRICT;",
        )?;
        let (next_sequence, reserved_until) = reserve_sequence_block(&mut connection, boot_id)?;
        Ok(Self {
            connection,
            boot_id,
            next_sequence,
            reserved_until,
        })
    }

    fn next(&mut self) -> Result<i64, TelemetryError> {
        if self.next_sequence >= self.reserved_until {
            (self.next_sequence, self.reserved_until) =
                reserve_sequence_block(&mut self.connection, self.boot_id)?;
        }
        let sequence =
            i64::try_from(self.next_sequence).map_err(|_| TelemetryError::SequenceExhausted)?;
        self.next_sequence = self
            .next_sequence
            .checked_add(1)
            .ok_or(TelemetryError::SequenceExhausted)?;
        Ok(sequence)
    }
}

fn reserve_sequence_block(
    connection: &mut Connection,
    boot_id: Uuid,
) -> Result<(u64, u64), TelemetryError> {
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let stored: Option<String> = transaction
        .query_row(
            "SELECT value FROM metadata WHERE key = ?1",
            [SEQUENCE_STATE_KEY],
            |row| row.get(0),
        )
        .optional()?;
    let next_sequence = match stored {
        None => 0,
        Some(value) => {
            let state: StoredSequenceReservation =
                serde_json::from_str(&value).map_err(|_| TelemetryError::InvalidSequenceState)?;
            let stored_boot_id = Uuid::parse_str(&state.boot_id)
                .map_err(|_| TelemetryError::InvalidSequenceState)?;
            if stored_boot_id.is_nil()
                || stored_boot_id.to_string() != state.boot_id
                || state.next_unreserved_sequence > SEQUENCE_LIMIT_EXCLUSIVE
            {
                return Err(TelemetryError::InvalidSequenceState);
            }
            if stored_boot_id == boot_id {
                state.next_unreserved_sequence
            } else {
                0
            }
        }
    };
    if next_sequence >= SEQUENCE_LIMIT_EXCLUSIVE {
        return Err(TelemetryError::SequenceExhausted);
    }
    let reserved_until = next_sequence
        .saturating_add(SEQUENCE_RESERVATION_SIZE)
        .min(SEQUENCE_LIMIT_EXCLUSIVE);
    let stored = serde_json::to_string(&StoredSequenceReservation {
        boot_id: boot_id.to_string(),
        next_unreserved_sequence: reserved_until,
    })
    .map_err(|_| TelemetryError::InvalidSequenceState)?;
    transaction.execute(
        "INSERT INTO metadata(key, value) VALUES (?1, ?2)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        params![SEQUENCE_STATE_KEY, stored],
    )?;
    transaction.commit()?;
    Ok((next_sequence, reserved_until))
}

impl<R: ProcessRunner, F: FileSystemProvider> TelemetryCollector<R, F> {
    pub fn new(
        runner: R,
        filesystem: F,
        paths: TelemetryPaths,
        boot_id: Uuid,
    ) -> Result<Self, TelemetryError> {
        if boot_id.is_nil() || boot_id.to_string() != boot_id.hyphenated().to_string() {
            return Err(TelemetryError::InvalidBootId);
        }
        let sequences =
            DurableSequenceAllocator::open(&paths.store.join(TELEMETRY_STATE_FILENAME), boot_id)?;
        Ok(Self {
            runner,
            filesystem,
            paths,
            boot_id,
            sequences,
            disk_counters: BTreeMap::new(),
            interface_counters: BTreeMap::new(),
            cpu_power_counter: None,
            runtime_counters: BTreeMap::new(),
            last_observed_at: None,
        })
    }

    pub fn sample(
        &mut self,
        previous: Option<&TelemetrySample>,
    ) -> Result<TelemetrySample, TelemetryError> {
        self.sample_at(previous, Utc::now())
    }

    pub fn sample_at(
        &mut self,
        previous: Option<&TelemetrySample>,
        observed_at: DateTime<Utc>,
    ) -> Result<TelemetrySample, TelemetryError> {
        let sequence = self.sequences.next()?;

        let cpu_counters = read_bounded_text(&self.paths.stat)
            .as_deref()
            .and_then(parse_cpu_counters);
        let (cpu_utilization_percent, cpu_counter_gap) = cpu_rate(
            previous.and_then(|sample| sample.cpu_counters),
            cpu_counters,
        );
        let (load_average_1m, load_average_5m, load_average_15m) =
            read_bounded_text(&self.paths.loadavg)
                .as_deref()
                .and_then(parse_load_average)
                .unwrap_or((None, None, None));
        let memory = read_bounded_text(&self.paths.meminfo)
            .as_deref()
            .and_then(parse_memory);
        let disk = self
            .filesystem
            .capacity(&self.paths.store)
            .ok()
            .filter(valid_capacity);
        let network_counters = read_bounded_text(&self.paths.net_dev)
            .as_deref()
            .and_then(parse_network_counters);
        let (network_receive_bytes_per_second, network_transmit_bytes_per_second) =
            network_rates(previous, network_counters, observed_at);
        let network_interfaces = read_bounded_text(&self.paths.net_dev)
            .as_deref()
            .map(parse_network_interfaces)
            .unwrap_or_default();
        let (interface_series, interface_capabilities, network_counter_gaps) =
            self.interface_series(&network_interfaces, self.last_observed_at, observed_at);
        let (disk_series, disk_capabilities, disk_counter_gaps) =
            self.disk_series(self.last_observed_at, observed_at);
        let (runtime_series, runtime_capabilities, runtimes, runtime_counter_gaps) =
            self.runtime_metrics(observed_at);
        let cpu_temperature = read_cpu_temperature(&self.paths.thermal);
        let (cpu_power, cpu_power_gap) = self.cpu_power(self.last_observed_at, observed_at);
        let accelerator_output = self
            .runner
            .run(
                Program::NvidiaSmi,
                &[
                    "--query-gpu=index,name,utilization.gpu,memory.total,memory.used,memory.free,temperature.gpu,power.draw,power.limit,clocks.current.sm,clocks.max.sm,clocks_throttle_reasons.hw_thermal_slowdown,clocks_throttle_reasons.sw_thermal_slowdown,clocks_throttle_reasons.hw_slowdown,clocks_throttle_reasons.sw_power_cap,pstate".to_owned(),
                    "--format=csv,noheader,nounits".to_owned(),
                ],
                Duration::from_secs(10),
            )
            .ok()
            .filter(|output| output.success && output.stdout.len() <= SOURCE_TEXT_LIMIT as usize)
            .map(|output| parse_accelerators(&output.stdout, memory))
            .unwrap_or_default();
        let gpu_processes = self
            .runner
            .run(
                Program::NvidiaSmi,
                &[
                    "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory".to_owned(),
                    "--format=csv,noheader,nounits".to_owned(),
                ],
                Duration::from_secs(10),
            )
            .ok()
            .filter(|output| output.success && output.stdout.len() <= SOURCE_TEXT_LIMIT as usize)
            .map(|output| parse_gpu_processes(&output.stdout))
            .unwrap_or_default();
        let memory_bandwidth = self
            .runner
            .run(
                Program::NvidiaSmi,
                &[
                    "dmon".to_owned(),
                    "-s".to_owned(),
                    "m".to_owned(),
                    "-c".to_owned(),
                    "1".to_owned(),
                ],
                Duration::from_secs(10),
            )
            .ok()
            .filter(|output| output.success && output.stdout.len() <= SOURCE_TEXT_LIMIT as usize)
            .and_then(|output| parse_memory_bandwidth(&output.stdout));
        let metrics = build_metrics(
            observed_at,
            read_uptime_seconds(&self.paths.uptime),
            cpu_utilization_percent,
            load_average_1m,
            load_average_5m,
            load_average_15m,
            memory,
            disk,
            cpu_temperature,
            cpu_power,
            memory_bandwidth,
            network_receive_bytes_per_second,
            network_transmit_bytes_per_second,
            &accelerator_output,
            &gpu_processes,
            &disk_series,
            &disk_capabilities,
            &interface_series,
            &interface_capabilities,
            &runtime_series,
            &runtime_capabilities,
            &runtimes,
        );
        let accelerator = accelerator_output.first();
        let dedicated_accelerator =
            accelerator.filter(|value| !shared_memory_pool(value.name.as_deref()));

        self.last_observed_at = Some(observed_at);
        Ok(TelemetrySample {
            boot_id: self.boot_id,
            sequence,
            observed_at,
            cpu_utilization_percent,
            load_average_1m,
            memory_total_bytes: memory.map(|value| value.0),
            memory_available_bytes: memory.map(|value| value.1),
            disk_total_bytes: disk.map(|value| value.total_bytes),
            disk_free_bytes: disk.map(|value| value.free_bytes),
            gpu_utilization_percent: accelerator.as_ref().and_then(|value| value.utilization),
            // GB10 exposes one physical unified pool.  Keep that capacity in
            // the memory fields and only retain GPU-attributed allocation in
            // the rich series so consumers cannot sum RAM and VRAM twice.
            gpu_memory_total_bytes: dedicated_accelerator.and_then(|value| value.memory_total),
            gpu_memory_free_bytes: dedicated_accelerator.and_then(|value| value.memory_free),
            temperature_c: accelerator.as_ref().and_then(|value| value.temperature),
            power_watts: accelerator.as_ref().and_then(|value| value.power),
            network_receive_bytes_per_second,
            network_transmit_bytes_per_second,
            gap_samples: network_counter_gaps
                .saturating_add(disk_counter_gaps)
                .saturating_add(cpu_power_gap)
                .saturating_add(runtime_counter_gaps)
                .saturating_add(i64::from(cpu_counter_gap)),
            details: accelerator
                .map(|value| TelemetryDetails {
                    accelerator_name: value.name.clone(),
                    accelerator_performance_state: value.performance_state.clone(),
                })
                .unwrap_or_default(),
            metrics,
            cpu_counters,
            network_counters,
        })
    }

    fn interface_series(
        &mut self,
        current: &BTreeMap<String, NetworkInterfaceCounters>,
        previous_at: Option<DateTime<Utc>>,
        observed_at: DateTime<Utc>,
    ) -> (Vec<TelemetrySeries>, Vec<TelemetryCapability>, i64) {
        let elapsed = elapsed_seconds(previous_at, observed_at);
        let mut series = Vec::new();
        let mut capabilities = Vec::new();
        let mut gaps = 0_i64;
        for (name, counters) in current.iter().take(MAX_NETWORK_INTERFACES) {
            let prior = self.interface_counters.insert(name.clone(), *counters);
            let (receive, receive_gap) =
                counter_rate(prior.map(|value| value.receive), counters.receive, elapsed);
            let (transmit, transmit_gap) = counter_rate(
                prior.map(|value| value.transmit),
                counters.transmit,
                elapsed,
            );
            gaps = gaps
                .saturating_add(i64::from(receive_gap))
                .saturating_add(i64::from(transmit_gap));
            let operstate =
                read_bounded_text(&self.paths.sys_class_net.join(name).join("operstate"))
                    .map(|value| value.trim().to_owned())
                    .filter(|value| matches!(value.as_str(), "up" | "down" | "unknown"));
            let link_speed = read_bounded_text(&self.paths.sys_class_net.join(name).join("speed"))
                .and_then(|value| value.trim().parse::<u64>().ok())
                .and_then(|value| value.checked_mul(1_000_000));
            capabilities.push(capability(
                metric_identity(
                    "network.receive_bytes_per_second",
                    "network",
                    None,
                    Some(name),
                    None,
                ),
                capability_context("bytes/s", "derived"),
                true,
                None,
            ));
            capabilities.push(capability(
                metric_identity(
                    "network.transmit_bytes_per_second",
                    "network",
                    None,
                    Some(name),
                    None,
                ),
                capability_context("bytes/s", "derived"),
                true,
                None,
            ));
            capabilities.push(capability(
                metric_identity("network.link_speed", "network", None, Some(name), None),
                capability_context("bps", "measured"),
                link_speed.is_some(),
                (!link_speed.is_some()).then_some("link speed unavailable"),
            ));
            capabilities.push(capability(
                metric_identity("network.operstate", "network", None, Some(name), None),
                capability_context("state", "measured"),
                operstate.is_some(),
                (!operstate.is_some()).then_some("interface state unavailable"),
            ));
            if let Some(value) = receive {
                series.push(series_number(
                    metric_identity(
                        "network.receive_bytes_per_second",
                        "network",
                        None,
                        Some(name),
                        None,
                    ),
                    value,
                    series_context("bytes/s", "derived", "counter_rate", observed_at),
                ));
            }
            if let Some(value) = transmit {
                series.push(series_number(
                    metric_identity(
                        "network.transmit_bytes_per_second",
                        "network",
                        None,
                        Some(name),
                        None,
                    ),
                    value,
                    series_context("bytes/s", "derived", "counter_rate", observed_at),
                ));
            }
            if let Some(value) = link_speed {
                series.push(series_number(
                    metric_identity("network.link_speed", "network", None, Some(name), None),
                    value as f64,
                    series_context("bps", "measured", "last", observed_at),
                ));
            }
            if let Some(value) = operstate {
                series.push(series_text(
                    metric_identity("network.operstate", "network", None, Some(name), None),
                    value,
                    series_context("state", "measured", "last", observed_at),
                ));
            }
        }
        (series, capabilities, gaps)
    }

    fn disk_series(
        &mut self,
        previous_at: Option<DateTime<Utc>>,
        observed_at: DateTime<Utc>,
    ) -> (Vec<TelemetrySeries>, Vec<TelemetryCapability>, i64) {
        let current = read_disk_counters(&self.paths.sys_block);
        let elapsed = elapsed_seconds(previous_at, observed_at);
        let mut series = Vec::new();
        let mut capabilities = Vec::new();
        let mut gaps = 0_i64;
        for (name, counters) in current.iter().take(MAX_STORAGE_DEVICES) {
            let prior = self.disk_counters.insert(name.clone(), *counters);
            let (read, read_gap) = counter_rate(
                prior.map(|value| value.read_bytes),
                counters.read_bytes,
                elapsed,
            );
            let (write, write_gap) = counter_rate(
                prior.map(|value| value.write_bytes),
                counters.write_bytes,
                elapsed,
            );
            capabilities.push(capability(
                metric_identity(
                    "storage.read_bytes_per_second",
                    "storage",
                    Some(name),
                    None,
                    None,
                ),
                capability_context("bytes/s", "derived"),
                true,
                None,
            ));
            capabilities.push(capability(
                metric_identity(
                    "storage.write_bytes_per_second",
                    "storage",
                    Some(name),
                    None,
                    None,
                ),
                capability_context("bytes/s", "derived"),
                true,
                None,
            ));
            gaps = gaps
                .saturating_add(i64::from(read_gap))
                .saturating_add(i64::from(write_gap));
            if let Some(value) = read {
                series.push(series_number(
                    metric_identity(
                        "storage.read_bytes_per_second",
                        "storage",
                        Some(name),
                        None,
                        None,
                    ),
                    value,
                    series_context("bytes/s", "derived", "counter_rate", observed_at),
                ));
            }
            if let Some(value) = write {
                series.push(series_number(
                    metric_identity(
                        "storage.write_bytes_per_second",
                        "storage",
                        Some(name),
                        None,
                        None,
                    ),
                    value,
                    series_context("bytes/s", "derived", "counter_rate", observed_at),
                ));
            }
        }
        (series, capabilities, gaps)
    }

    fn cpu_power(
        &mut self,
        previous_at: Option<DateTime<Utc>>,
        observed_at: DateTime<Utc>,
    ) -> (Option<f64>, i64) {
        let Some(current) = read_cpu_energy(&self.paths.powercap) else {
            return (None, 0);
        };
        let prior = self
            .cpu_power_counter
            .replace(current)
            .map(|value| value.microjoules);
        let elapsed = elapsed_seconds(previous_at, observed_at);
        let (value, gap) = counter_rate(prior, current.microjoules, elapsed);
        (value.map(|value| value / 1_000_000.0), i64::from(gap))
    }

    fn runtime_metrics(
        &mut self,
        observed_at: DateTime<Utc>,
    ) -> (
        Vec<TelemetrySeries>,
        Vec<TelemetryCapability>,
        Vec<TelemetryRuntime>,
        i64,
    ) {
        let root = self.paths.store.join("run-metadata");
        let Ok(metadata) = fs::symlink_metadata(&root) else {
            return (Vec::new(), Vec::new(), Vec::new(), 0);
        };
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return (Vec::new(), Vec::new(), Vec::new(), 0);
        }
        let Ok(entries) = fs::read_dir(root) else {
            return (Vec::new(), Vec::new(), Vec::new(), 0);
        };
        let mut run_ids = Vec::new();
        for entry in entries.flatten().take(MAX_MANAGED_RUNTIME_ENTRIES + 1) {
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            let Ok(run_id) = entry.file_name().into_string() else {
                continue;
            };
            if file_type.is_dir() && !file_type.is_symlink() && canonical_runtime_uuid(&run_id) {
                run_ids.push(run_id);
            }
        }
        run_ids.sort_unstable();
        run_ids.truncate(MAX_MANAGED_RUNTIME_ENTRIES);
        let elapsed = elapsed_seconds(self.last_observed_at, observed_at);
        let mut series = Vec::new();
        let mut capabilities = Vec::new();
        let mut runtimes = Vec::new();
        let mut gaps = 0_i64;
        for run_id in run_ids {
            let path = self
                .paths
                .store
                .join("run-metadata")
                .join(&run_id)
                .join("runtime.json");
            let Some(document) = read_bounded_text(&path) else {
                continue;
            };
            let Ok(contract) = serde_json::from_str::<RuntimeTelemetryContract>(&document) else {
                continue;
            };
            if contract.run_id != run_id {
                continue;
            }
            let adapter = contract
                .adapter
                .clone()
                .unwrap_or_else(|| "unknown".to_owned());
            let rank = contract.placement.as_ref().map_or(0, |value| value.rank);
            let endpoint = known_runtime_endpoint(&adapter, contract.endpoint.as_ref());
            let mut body = None;
            if let Some((port, path)) = endpoint.as_ref() {
                let url = format!("http://127.0.0.1:{port}{path}");
                let arguments = vec![
                    "--silent".to_owned(),
                    "--show-error".to_owned(),
                    "--connect-timeout".to_owned(),
                    "1".to_owned(),
                    "--max-time".to_owned(),
                    "2".to_owned(),
                    "--max-filesize".to_owned(),
                    (SOURCE_TEXT_LIMIT / 2).to_string(),
                    "--noproxy".to_owned(),
                    "*".to_owned(),
                    "--proto".to_owned(),
                    "=http".to_owned(),
                    url.as_str().to_owned(),
                ];
                body = self
                    .runner
                    .run(Program::Curl, &arguments, Duration::from_secs(3))
                    .ok()
                    .filter(|output| {
                        output.success && output.stdout.len() <= SOURCE_TEXT_LIMIT as usize
                    })
                    .map(|output| output.stdout);
            }
            let (runtime_series, runtime_caps, runtime_ok, runtime_gaps) =
                match (body.as_deref(), adapter.as_str()) {
                    (Some(body), "comfyui") => {
                        parse_comfy_runtime(body, &contract, rank, observed_at)
                    }
                    (Some(body), "vllm" | "sglang" | "llama-cpp" | "ds4" | "exl3") => {
                        parse_prometheus_runtime(
                            body,
                            &contract,
                            rank,
                            observed_at,
                            elapsed,
                            &mut self.runtime_counters,
                        )
                    }
                    _ => (
                        Vec::new(),
                        runtime_capabilities_for_run(
                            &contract.run_id,
                            Some("managed runtime metrics endpoint unavailable"),
                        ),
                        false,
                        0,
                    ),
                };
            runtimes.push(TelemetryRuntime {
                run_id: contract.run_id.clone(),
                engine_id: contract.run_id.clone(),
                backend: adapter.clone(),
                version: contract.adapter_version.map(|value| value.to_string()),
                // Endpoint and model identity are Controller-owned launch
                // metadata.  The agent uses the allowlisted loopback endpoint
                // internally, but does not echo it or recipe data upstream.
                endpoint: None,
                model: None,
                model_version: None,
                recipe_revision: None,
                context_limit_tokens: None,
                serving_node_ids: Vec::new(),
                ranks: vec![rank],
                readiness: if runtime_ok { "running" } else { "unknown" }.to_owned(),
                error: (!runtime_ok).then_some("managed runtime metrics unavailable".to_owned()),
                adapter: adapter.clone(),
                adapter_version: contract.adapter_version.map(|value| value.to_string()),
                adapter_supported: runtime_ok,
                adapter_reason: (!runtime_ok)
                    .then_some("known local managed-runtime metrics were not observed".to_owned()),
            });
            series.extend(runtime_series);
            capabilities.extend(runtime_caps);
            gaps = gaps.saturating_add(runtime_gaps);
        }
        (series, capabilities, runtimes, gaps)
    }
}

const MAX_MANAGED_RUNTIME_ENTRIES: usize = 64;

/// Only the agent-owned runtime contract is read. It is written when the
/// Controller-authorized run starts and never comes from an arbitrary URL.
#[derive(Debug, Deserialize)]
struct RuntimeTelemetryContract {
    run_id: String,
    #[serde(default)]
    adapter: Option<String>,
    #[serde(default)]
    adapter_version: Option<u32>,
    #[serde(default)]
    endpoint: Option<RuntimeTelemetryEndpoint>,
    #[serde(default)]
    placement: Option<RuntimeTelemetryPlacement>,
}

#[derive(Debug, Deserialize)]
struct RuntimeTelemetryEndpoint {
    listen_port: u16,
}

#[derive(Debug, Deserialize)]
struct RuntimeTelemetryPlacement {
    #[serde(default)]
    rank: u32,
}

#[derive(Debug, Clone)]
struct PrometheusSample {
    name: String,
    labels: BTreeMap<String, String>,
    value: f64,
}

fn canonical_runtime_uuid(value: &str) -> bool {
    Uuid::parse_str(value).is_ok_and(|uuid| !uuid.is_nil() && uuid.to_string() == value)
}

fn known_runtime_endpoint(
    adapter: &str,
    endpoint: Option<&RuntimeTelemetryEndpoint>,
) -> Option<(u16, &'static str)> {
    let path = match adapter {
        "vllm" | "sglang" | "llama-cpp" | "ds4" | "exl3" => "/metrics",
        // The managed Comfy adapter owns this endpoint and exposes queue
        // identity without requiring a broad runtime or port scan.
        "comfyui" => "/queue",
        _ => return None,
    };
    let port = endpoint?.listen_port;
    (1024..=65_535).contains(&port).then_some((port, path))
}

fn parse_prometheus_samples(value: &[u8]) -> Vec<PrometheusSample> {
    let Ok(value) = std::str::from_utf8(value) else {
        return Vec::new();
    };
    let mut samples = Vec::new();
    for line in value.lines().take(2048) {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut fields = line.split_ascii_whitespace();
        let Some(metric) = fields.next() else {
            continue;
        };
        let Some(raw_value) = fields.next() else {
            continue;
        };
        let Ok(number) = raw_value.parse::<f64>() else {
            continue;
        };
        if !number.is_finite() || number.abs() > 1_000_000_000_000_000.0 {
            continue;
        }
        let (name, raw_labels) = metric
            .split_once('{')
            .map_or((metric, None), |(name, labels)| {
                (name, labels.strip_suffix('}'))
            });
        if name.is_empty()
            || name.len() > 128
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b':' | b'.'))
        {
            continue;
        }
        let labels = raw_labels.map(parse_prometheus_labels).unwrap_or_default();
        samples.push(PrometheusSample {
            name: name.to_owned(),
            labels,
            value: number,
        });
    }
    samples
}

fn parse_prometheus_labels(value: &str) -> BTreeMap<String, String> {
    let mut labels = BTreeMap::new();
    for field in value.split(',').take(32) {
        let Some((key, raw_value)) = field.split_once('=') else {
            continue;
        };
        let key = key.trim();
        let raw_value = raw_value.trim();
        let value = raw_value
            .strip_prefix('"')
            .and_then(|value| value.strip_suffix('"'))
            .unwrap_or(raw_value);
        if !key.is_empty()
            && key.len() <= 64
            && value.len() <= 128
            && key
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
        {
            labels.insert(key.to_owned(), value.to_owned());
        }
    }
    labels
}

fn runtime_metric_name_matches(name: &str, adapter: &str, terms: &[&str]) -> bool {
    let name = name.to_ascii_lowercase();
    let namespaced = match adapter {
        "vllm" => name.starts_with("vllm_") || name.starts_with("vllm:"),
        "sglang" => name.starts_with("sglang_") || name.starts_with("sglang:"),
        // llama.cpp's metrics have been emitted with both spellings over its
        // supported server versions.  Keep the allowlist explicit so an
        // arbitrary /metrics response cannot make an adapter look complete.
        "llama-cpp" => {
            name.starts_with("llama_")
                || name.starts_with("llama:")
                || name.starts_with("llamacpp_")
                || name.starts_with("llamacpp:")
        }
        "ds4" => name.starts_with("ds4_") || name.starts_with("ds4:"),
        "exl3" => name.starts_with("exl3_") || name.starts_with("exl3:"),
        _ => false,
    };
    namespaced && terms.iter().any(|term| name.contains(term))
}

fn prometheus_value(samples: &[PrometheusSample], adapter: &str, terms: &[&str]) -> Option<f64> {
    samples
        .iter()
        .filter(|sample| {
            let name = sample.name.to_ascii_lowercase();
            !name.ends_with("_bucket")
                && !name.ends_with("_sum")
                && !name.ends_with("_count")
                && runtime_metric_name_matches(&name, adapter, terms)
        })
        .map(|sample| sample.value)
        .next()
}

fn prometheus_histogram_p95(
    samples: &[PrometheusSample],
    adapter: &str,
    terms: &[&str],
) -> Option<(f64, bool)> {
    let mut families = BTreeMap::<String, Vec<(f64, f64)>>::new();
    for sample in samples.iter().filter(|sample| {
        let name = sample.name.to_ascii_lowercase();
        name.ends_with("_bucket")
            && runtime_metric_name_matches(&name, adapter, terms)
            // A p95 for a labelled family needs an explicit aggregation
            // policy.  The agent currently owns one runtime scope, so only
            // the unlabelled Prometheus histogram is safe to consume.
            && sample.labels.keys().all(|key| key == "le")
    }) {
        let name = sample.name.to_ascii_lowercase();
        let upper = sample.labels.get("le").and_then(|value| {
            if value == "+Inf" {
                Some(f64::INFINITY)
            } else {
                value.parse::<f64>().ok()
            }
        })?;
        if !upper.is_nan() && upper >= 0.0 && sample.value.is_finite() && sample.value >= 0.0 {
            families
                .entry(name)
                .or_default()
                .push((upper, sample.value));
        }
    }
    // Multiple matching histogram families (for example an adapter's mean
    // and a current latency series beside its histogram) are ambiguous.
    if families.len() != 1 {
        return None;
    }
    let (family_name, mut buckets) = families.into_iter().next()?;
    buckets.sort_by(|left, right| left.0.total_cmp(&right.0));
    if buckets.windows(2).any(|pair| pair[1].1 < pair[0].1) {
        return None;
    }
    let total = buckets.last()?.1;
    if !total.is_finite() || total <= 0.0 {
        return None;
    }
    let target = total * 0.95;
    let (upper, _) = buckets.into_iter().find(|(_, count)| *count >= target)?;
    Some((
        upper,
        family_name.contains("_seconds") || family_name.contains("_second"),
    ))
}

#[derive(Debug, Clone, Copy)]
struct RuntimeMetricReading {
    value: Option<f64>,
    unit: &'static str,
    measurement_kind: &'static str,
    aggregation: &'static str,
    counter_key: Option<&'static str>,
}

#[derive(Debug, Clone, Copy)]
struct TelemetryMetricIdentity<'a> {
    key: &'a str,
    scope: &'a str,
    device_id: Option<&'a str>,
    interface_name: Option<&'a str>,
    run_id: Option<&'a str>,
}

#[derive(Debug, Clone, Copy)]
struct TelemetrySeriesContext<'a> {
    unit: &'a str,
    measurement_kind: &'a str,
    aggregation: &'a str,
    observed_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Copy)]
struct TelemetryCapabilityContext<'a> {
    unit: &'a str,
    measurement_kind: &'a str,
}

fn metric_identity<'a>(
    key: &'a str,
    scope: &'a str,
    device_id: Option<&'a str>,
    interface_name: Option<&'a str>,
    run_id: Option<&'a str>,
) -> TelemetryMetricIdentity<'a> {
    TelemetryMetricIdentity {
        key,
        scope,
        device_id,
        interface_name,
        run_id,
    }
}

fn series_context<'a>(
    unit: &'a str,
    measurement_kind: &'a str,
    aggregation: &'a str,
    observed_at: DateTime<Utc>,
) -> TelemetrySeriesContext<'a> {
    TelemetrySeriesContext {
        unit,
        measurement_kind,
        aggregation,
        observed_at,
    }
}

fn capability_context<'a>(
    unit: &'a str,
    measurement_kind: &'a str,
) -> TelemetryCapabilityContext<'a> {
    TelemetryCapabilityContext {
        unit,
        measurement_kind,
    }
}

fn runtime_capabilities_for_run(run_id: &str, reason: Option<&str>) -> Vec<TelemetryCapability> {
    runtime_metric_capabilities()
        .into_iter()
        .map(|(key, unit, kind)| {
            let mut item = capability(
                metric_identity(key, "runtime", None, None, Some(run_id)),
                capability_context(unit, kind),
                reason.is_none(),
                reason,
            );
            item.source = "runtime-adapter:managed-local".to_owned();
            item
        })
        .collect()
}

fn add_runtime_number(
    series: &mut Vec<TelemetrySeries>,
    run_id: &str,
    rank: u32,
    key: &str,
    value: f64,
    context: TelemetrySeriesContext<'_>,
) {
    let rank_id = format!("rank-{rank}");
    let mut item = series_number(
        metric_identity(key, "runtime", Some(&rank_id), None, Some(run_id)),
        value,
        context,
    );
    item.source = "runtime-adapter:managed-local".to_owned();
    series.push(item);
}

fn add_runtime_capability(
    capabilities: &mut Vec<TelemetryCapability>,
    run_id: &str,
    key: &str,
    unit: &str,
    measurement_kind: &str,
    supported: bool,
    reason: Option<&str>,
) {
    let mut item = capability(
        metric_identity(key, "runtime", None, None, Some(run_id)),
        capability_context(unit, measurement_kind),
        supported,
        reason,
    );
    item.source = "runtime-adapter:managed-local".to_owned();
    capabilities.push(item);
}

fn runtime_metric_reading(
    adapter: &str,
    key: &str,
    samples: &[PrometheusSample],
) -> RuntimeMetricReading {
    if matches!(adapter, "ds4" | "exl3") {
        return RuntimeMetricReading {
            value: None,
            unit: runtime_metric_capabilities()
                .into_iter()
                .find(|(candidate, _, _)| *candidate == key)
                .map_or("unknown", |(_, unit, _)| unit),
            measurement_kind: runtime_metric_capabilities()
                .into_iter()
                .find(|(candidate, _, _)| *candidate == key)
                .map_or("measured", |(_, _, kind)| kind),
            aggregation: "last",
            counter_key: None,
        };
    }
    if adapter == "llama-cpp" {
        let direct = match key {
            "runtime.decode_tokens_per_second" => {
                Some((&["predicted_tokens_seconds"][..], "tokens/s"))
            }
            "runtime.prefill_tokens_per_second" => {
                Some((&["prompt_tokens_seconds"][..], "tokens/s"))
            }
            "runtime.output_tokens_total" => Some((&["tokens_predicted_total"][..], "tokens")),
            "runtime.slots_active" => Some((&["total_slots"][..], "requests")),
            "runtime.requests_running" => Some((&["requests_processing"][..], "requests")),
            "runtime.requests_waiting" => Some((&["requests_deferred"][..], "requests")),
            _ => None,
        };
        if let Some((terms, unit)) = direct {
            return RuntimeMetricReading {
                value: prometheus_value(samples, adapter, terms),
                unit,
                measurement_kind: "measured",
                aggregation: "last",
                counter_key: None,
            };
        }
    }
    let (terms, unit, kind, aggregation, counter_key) = match key {
        "runtime.decode_tokens_per_second" => (
            &[
                "generation_tokens_total",
                "decode_tokens_total",
                "output_tokens_total",
            ][..],
            "tokens/s",
            "derived",
            "counter_rate",
            Some("decode"),
        ),
        "runtime.prefill_tokens_per_second" => (
            &["prefill_tokens_total", "prompt_tokens_total"][..],
            "tokens/s",
            "derived",
            "counter_rate",
            Some("prefill"),
        ),
        "runtime.prefill_cached_tokens_per_second" => (
            &["prefill_cached_tokens_total", "cached_prefill_tokens_total"][..],
            "tokens/s",
            "derived",
            "counter_rate",
            Some("prefill_cached"),
        ),
        "runtime.prefill_uncached_tokens_per_second" => (
            &[
                "prefill_uncached_tokens_total",
                "uncached_prefill_tokens_total",
            ][..],
            "tokens/s",
            "derived",
            "counter_rate",
            Some("prefill_uncached"),
        ),
        "runtime.output_tokens_total" => (
            &["generation_tokens_total", "output_tokens_total"][..],
            "tokens",
            "measured",
            "last",
            None,
        ),
        "runtime.slots_active" => (
            &["slots_active", "num_slots"][..],
            "requests",
            "measured",
            "last",
            None,
        ),
        "runtime.requests_running" => (
            &[
                "num_requests_running",
                "requests_running",
                "running_requests",
            ][..],
            "requests",
            "measured",
            "last",
            None,
        ),
        "runtime.requests_waiting" => (
            &[
                "num_requests_waiting",
                "requests_waiting",
                "waiting_requests",
            ][..],
            "requests",
            "measured",
            "last",
            None,
        ),
        "runtime.kv_cache_usage_percent" => (
            &[
                "kv_cache_usage_perc",
                "kv_cache_usage_percent",
                "kv_cache_utilization",
            ][..],
            "%",
            "measured",
            "last",
            None,
        ),
        "runtime.preemptions_total" => (
            &["num_preemptions_total", "preemptions_total"][..],
            "count",
            "measured",
            "last",
            None,
        ),
        "runtime.prefix_cache_hit_percent" => (
            &["prefix_cache_hit_rate", "prefix_cache_hit_percent"][..],
            "%",
            "derived",
            "last",
            None,
        ),
        "runtime.mtp_acceptance_percent" => (
            &["mtp_acceptance_rate", "mtp_acceptance_percent"][..],
            "%",
            "derived",
            "last",
            None,
        ),
        "runtime.ttft_p95_ms" => (
            &["time_to_first_token", "ttft"][..],
            "ms",
            "derived",
            "p95",
            None,
        ),
        "runtime.e2e_p95_ms" => (
            &["request_latency", "e2e_latency", "end_to_end_latency"][..],
            "ms",
            "derived",
            "p95",
            None,
        ),
        "runtime.itl_p95_ms" => (
            &["inter_token_latency", "time_per_output_token", "itl"][..],
            "ms",
            "derived",
            "p95",
            None,
        ),
        _ => {
            return RuntimeMetricReading {
                value: None,
                unit: "unknown",
                measurement_kind: "measured",
                aggregation: "last",
                counter_key: None,
            };
        }
    };
    let value = if aggregation == "p95" {
        prometheus_latency_p95_ms(samples, adapter, terms)
    } else {
        prometheus_value(samples, adapter, terms)
    };
    RuntimeMetricReading {
        value,
        unit,
        measurement_kind: kind,
        aggregation,
        counter_key,
    }
}

fn prometheus_latency_p95_ms(
    samples: &[PrometheusSample],
    adapter: &str,
    terms: &[&str],
) -> Option<f64> {
    let (value, seconds) = prometheus_histogram_p95(samples, adapter, terms)?;
    let value = if seconds { value * 1_000.0 } else { value };
    (value.is_finite() && (0.0..=1_000_000.0).contains(&value)).then_some(value)
}

fn prometheus_ratio(
    samples: &[PrometheusSample],
    adapter: &str,
    numerator_terms: &[&str],
    denominator_terms: &[&str],
) -> Option<f64> {
    let numerator = prometheus_value(samples, adapter, numerator_terms)?;
    let denominator = prometheus_value(samples, adapter, denominator_terms)?;
    if denominator <= 0.0 {
        return None;
    }
    let value = numerator * 100.0 / denominator;
    (value.is_finite() && (0.0..=100.0).contains(&value)).then_some(value)
}

fn normalize_percent(value: Option<f64>) -> Option<f64> {
    let value = value?;
    let value = if (0.0..=1.0).contains(&value) {
        value * 100.0
    } else {
        value
    };
    (value.is_finite() && (0.0..=100.0).contains(&value)).then_some(value)
}

fn parse_prometheus_runtime(
    value: &[u8],
    contract: &RuntimeTelemetryContract,
    rank: u32,
    observed_at: DateTime<Utc>,
    elapsed: Option<f64>,
    counters: &mut BTreeMap<String, f64>,
) -> (Vec<TelemetrySeries>, Vec<TelemetryCapability>, bool, i64) {
    let samples = parse_prometheus_samples(value);
    let mut series = Vec::new();
    let mut capabilities = Vec::new();
    let mut recognized = false;
    let mut gaps = 0_i64;
    let adapter = contract.adapter.as_deref().unwrap_or("");
    for (key, _, _) in runtime_metric_capabilities() {
        let reading = runtime_metric_reading(adapter, key, &samples);
        let mut metric = reading.value;
        if key == "runtime.prefix_cache_hit_percent" && adapter != "ds4" && adapter != "exl3" {
            metric = normalize_percent(metric.or_else(|| {
                prometheus_ratio(
                    &samples,
                    adapter,
                    &["prefix_cache_hits_total", "prefix_cache_hit_total"],
                    &["prefix_cache_queries_total", "prefix_cache_query_total"],
                )
            }));
        } else if key == "runtime.mtp_acceptance_percent" && adapter != "ds4" && adapter != "exl3" {
            metric = normalize_percent(metric.or_else(|| {
                prometheus_ratio(
                    &samples,
                    adapter,
                    &[
                        "mtp_accepted_total",
                        "mtp_accept_total",
                        "speculative_accepted_total",
                    ],
                    &[
                        "mtp_proposed_total",
                        "mtp_propose_total",
                        "speculative_proposed_total",
                    ],
                )
            }));
        } else if key == "runtime.kv_cache_usage_percent" {
            metric = normalize_percent(metric);
        }
        let raw_available = metric.is_some();
        if let Some(counter_key) = reading.counter_key {
            let state_key = format!("{}:{rank}:{counter_key}", contract.run_id);
            metric = metric.and_then(|current| {
                let previous = counters.insert(state_key, current);
                let previous = previous?;
                let elapsed = elapsed?;
                let delta = current - previous;
                if delta < 0.0 {
                    gaps = gaps.saturating_add(1);
                    return None;
                }
                let rate = delta / elapsed;
                (rate.is_finite() && (0.0..=1_000_000_000_000_000.0).contains(&rate))
                    .then_some(rate)
            });
        }
        recognized |= raw_available;
        let reason = (!raw_available).then_some("metric was not exposed by the managed runtime");
        add_runtime_capability(
            &mut capabilities,
            &contract.run_id,
            key,
            reading.unit,
            reading.measurement_kind,
            raw_available,
            reason,
        );
        if let Some(metric) = metric {
            add_runtime_number(
                &mut series,
                &contract.run_id,
                rank,
                key,
                metric,
                series_context(
                    reading.unit,
                    reading.measurement_kind,
                    reading.aggregation,
                    observed_at,
                ),
            );
        }
    }
    (series, capabilities, recognized, gaps)
}

fn comfy_queue_value(value: &serde_json::Value, names: &[&str]) -> Option<f64> {
    names.iter().find_map(|name| {
        let candidate = value
            .get(*name)
            .or_else(|| value.get("queue").and_then(|queue| queue.get(*name)))?;
        match candidate {
            serde_json::Value::Array(values) => Some(values.len() as f64),
            serde_json::Value::Number(number) => number
                .as_f64()
                .filter(|value| value.is_finite() && (0.0..=1_000_000.0).contains(value)),
            _ => None,
        }
    })
}

fn parse_comfy_runtime(
    value: &[u8],
    contract: &RuntimeTelemetryContract,
    rank: u32,
    observed_at: DateTime<Utc>,
) -> (Vec<TelemetrySeries>, Vec<TelemetryCapability>, bool, i64) {
    let Ok(document) = serde_json::from_slice::<serde_json::Value>(value) else {
        return (
            Vec::new(),
            runtime_capabilities_for_run(
                &contract.run_id,
                Some("ComfyUI queue response was invalid"),
            ),
            false,
            0,
        );
    };
    let running = comfy_queue_value(&document, &["queue_running", "running"]);
    let waiting = comfy_queue_value(
        &document,
        &["queue_pending", "queue_waiting", "pending", "waiting"],
    );
    let slots = running;
    let mut series = Vec::new();
    let mut capabilities = Vec::new();
    let values = [
        ("runtime.slots_active", slots),
        ("runtime.requests_running", running),
        ("runtime.requests_waiting", waiting),
    ];
    let recognized = values.iter().any(|(_, value)| value.is_some());
    for (key, value) in values {
        add_runtime_capability(
            &mut capabilities,
            &contract.run_id,
            key,
            "requests",
            "measured",
            value.is_some(),
            (!value.is_some()).then_some("metric was not exposed by the managed runtime"),
        );
        if let Some(value) = value {
            add_runtime_number(
                &mut series,
                &contract.run_id,
                rank,
                key,
                value,
                series_context("requests", "measured", "last", observed_at),
            );
        }
    }
    for (key, unit, kind) in runtime_metric_capabilities() {
        if matches!(
            key,
            "runtime.slots_active" | "runtime.requests_running" | "runtime.requests_waiting"
        ) {
            continue;
        }
        add_runtime_capability(
            &mut capabilities,
            &contract.run_id,
            key,
            unit,
            kind,
            false,
            Some("metric is not exposed by the managed ComfyUI queue adapter"),
        );
    }
    (series, capabilities, recognized, 0)
}

#[derive(Debug, Default)]
pub struct TelemetryQueue {
    samples: VecDeque<TelemetrySample>,
}

impl TelemetryQueue {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn len(&self) -> usize {
        self.samples.len()
    }

    pub fn is_empty(&self) -> bool {
        self.samples.is_empty()
    }

    pub fn push(&mut self, sample: TelemetrySample) {
        if self.samples.len() == MAX_QUEUE_SAMPLES
            && let Some(dropped) = self.samples.pop_front()
        {
            let lost = dropped.gap_samples.saturating_add(1);
            if let Some(oldest_retained) = self.samples.front_mut() {
                oldest_retained.gap_samples = oldest_retained.gap_samples.saturating_add(lost);
            }
        }
        self.samples.push_back(sample);
    }

    pub fn batch(&self) -> Vec<TelemetrySample> {
        self.samples
            .iter()
            .take(MAX_REPORT_SAMPLES)
            .cloned()
            .collect()
    }

    pub fn acknowledge_prefix(&mut self, count: usize) -> Result<(), TelemetryError> {
        if count > self.samples.len() {
            return Err(TelemetryError::SequenceExhausted);
        }
        self.samples.drain(..count);
        Ok(())
    }
}

#[derive(Debug, Clone, Copy)]
pub struct TelemetrySchedule {
    next_collection: tokio::time::Instant,
    next_send: tokio::time::Instant,
}

impl TelemetrySchedule {
    pub fn new(now: tokio::time::Instant) -> Self {
        Self {
            next_collection: now,
            next_send: now,
        }
    }

    pub fn collection_due(&self, now: tokio::time::Instant) -> bool {
        now >= self.next_collection
    }

    pub fn collected(
        &mut self,
        collection_started: tokio::time::Instant,
        collection_finished: tokio::time::Instant,
    ) {
        let mut next_collection = collection_started + COLLECTION_INTERVAL;
        while next_collection <= collection_finished {
            next_collection += COLLECTION_INTERVAL;
        }
        self.next_collection = next_collection;
    }

    pub fn send_due(&self, now: tokio::time::Instant, has_samples: bool) -> bool {
        has_samples && now >= self.next_send
    }

    pub fn send_failed(&mut self, now: tokio::time::Instant, retry_after: Duration) {
        self.next_send = now + retry_after;
    }

    pub fn send_succeeded(&mut self, now: tokio::time::Instant) {
        self.next_send = now;
    }

    pub fn next_collection(&self) -> tokio::time::Instant {
        self.next_collection
    }
}

pub fn read_boot_id(path: &Path) -> Result<Uuid, TelemetryError> {
    let value = read_bounded_text(path).ok_or(TelemetryError::InvalidBootId)?;
    let value = value.trim();
    let boot_id = Uuid::parse_str(value).map_err(|_| TelemetryError::InvalidBootId)?;
    if boot_id.is_nil() || boot_id.to_string() != value {
        return Err(TelemetryError::InvalidBootId);
    }
    Ok(boot_id)
}

pub(crate) fn valid_report_batch(samples: &[TelemetrySample]) -> bool {
    if samples.is_empty() || samples.len() > MAX_REPORT_SAMPLES {
        return false;
    }
    let mut previous_observed_at = None;
    let mut boot_heads = std::collections::BTreeMap::new();
    for sample in samples {
        if sample.boot_id.is_nil()
            || sample.sequence < 0
            || sample.gap_samples < 0
            || previous_observed_at.is_some_and(|previous| sample.observed_at <= previous)
            || !valid_optional_number(sample.cpu_utilization_percent, 0.0, 100.0)
            || !valid_optional_number(sample.load_average_1m, 0.0, 1_000_000.0)
            || !valid_optional_capacity_pair(
                sample.memory_total_bytes,
                sample.memory_available_bytes,
            )
            || !valid_optional_capacity_pair(sample.disk_total_bytes, sample.disk_free_bytes)
            || !valid_optional_number(sample.gpu_utilization_percent, 0.0, 100.0)
            || !valid_optional_capacity_pair(
                sample.gpu_memory_total_bytes,
                sample.gpu_memory_free_bytes,
            )
            || !valid_optional_number(sample.temperature_c, -100.0, 300.0)
            || !valid_optional_number(sample.power_watts, 0.0, 100_000.0)
            || !valid_optional_number(
                sample.network_receive_bytes_per_second,
                0.0,
                1_000_000_000_000_000.0,
            )
            || !valid_optional_number(
                sample.network_transmit_bytes_per_second,
                0.0,
                1_000_000_000_000_000.0,
            )
            || !valid_optional_text(sample.details.accelerator_name.as_deref(), 256)
            || !valid_optional_text(sample.details.accelerator_performance_state.as_deref(), 32)
            || !valid_metrics(&sample.metrics)
        {
            return false;
        }
        if boot_heads
            .insert(sample.boot_id, sample.sequence)
            .is_some_and(|previous| sample.sequence <= previous)
        {
            return false;
        }
        previous_observed_at = Some(sample.observed_at);
    }
    true
}

fn valid_optional_number(value: Option<f64>, minimum: f64, maximum: f64) -> bool {
    value.is_none_or(|value| value.is_finite() && (minimum..=maximum).contains(&value))
}

fn valid_optional_capacity_pair(total: Option<u64>, free: Option<u64>) -> bool {
    match (total, free) {
        (None, None) => true,
        (Some(total), Some(free)) => free <= total && total <= MAX_CAPACITY_BYTES,
        _ => false,
    }
}

fn valid_optional_text(value: Option<&str>, maximum_chars: usize) -> bool {
    value.is_none_or(|value| !value.is_empty() && value.chars().count() <= maximum_chars)
}

fn valid_metrics(metrics: &TelemetryMetrics) -> bool {
    if metrics.schema_version != 2
        || metrics.series.len() > MAX_METRIC_SERIES
        || metrics.capabilities.len() > MAX_CAPABILITIES
        || metrics.runtimes.len() > 32
        || metrics.workloads.len() > 128
    {
        return false;
    }
    let Ok(encoded) = serde_json::to_vec(metrics) else {
        return false;
    };
    if encoded.len() > 48 * 1024 {
        return false;
    }
    let mut series_identities = std::collections::BTreeSet::new();
    for series in &metrics.series {
        let identity = (
            series.key.as_str(),
            series.scope.as_str(),
            series.device_id.as_deref(),
            series.process_id,
            series.interface_name.as_deref(),
            series.run_id.as_deref(),
        );
        if !series_identities.insert(identity) {
            return false;
        }
        if !valid_metric_identity(
            &series.key,
            &series.scope,
            series.device_id.as_deref(),
            series.interface_name.as_deref(),
            series.run_id.as_deref(),
        ) || !valid_metric_text(&series.unit, 32)
            || !valid_metric_text(&series.source, 128)
            || !valid_metric_text(&series.measurement_kind, 16)
            || !valid_metric_text(&series.freshness, 16)
            || !valid_metric_text(&series.support_status, 16)
            || !valid_metric_text(&series.aggregation, 32)
            || !valid_optional_text(series.reason.as_deref(), 256)
            || !series.freshness_threshold_seconds.is_finite()
            || !(0.0..=86_400.0).contains(&series.freshness_threshold_seconds)
            || series.freshness_threshold_seconds <= 0.0
            || !valid_metric_value(&series.value)
        {
            return false;
        }
        let available = series.support_status == "available";
        if (available && series.reason.is_some()) || (!available && series.reason.is_none()) {
            return false;
        }
        if series.scope == "accelerator" && series.device_id.is_none()
            || series.scope == "storage" && series.device_id.is_none()
            || series.scope == "network" && series.interface_name.is_none()
            || matches!(series.scope.as_str(), "runtime" | "workload" | "benchmark")
                && series.run_id.is_none()
            || series.process_id == Some(0)
            || !valid_optional_text(series.process_name.as_deref(), 128)
        {
            return false;
        }
    }
    let mut capability_identities = std::collections::BTreeSet::new();
    for capability in &metrics.capabilities {
        let identity = (
            capability.key.as_str(),
            capability.scope.as_str(),
            capability.device_id.as_deref(),
            capability.process_id,
            capability.interface_name.as_deref(),
            capability.run_id.as_deref(),
        );
        if !capability_identities.insert(identity) {
            return false;
        }
        if !valid_metric_identity(
            &capability.key,
            &capability.scope,
            capability.device_id.as_deref(),
            capability.interface_name.as_deref(),
            capability.run_id.as_deref(),
        ) || !valid_metric_text(&capability.unit, 32)
            || !valid_metric_text(&capability.source, 128)
            || !valid_metric_text(&capability.measurement_kind, 16)
            || !valid_optional_text(capability.reason.as_deref(), 256)
            || capability.process_id == Some(0)
            || !valid_optional_text(capability.process_name.as_deref(), 128)
            || !capability.freshness_threshold_seconds.is_finite()
            || !(0.0..=86_400.0).contains(&capability.freshness_threshold_seconds)
            || capability.freshness_threshold_seconds <= 0.0
        {
            return false;
        }
        if (capability.supported && capability.reason.is_some())
            || (!capability.supported && capability.reason.is_none())
            || capability.scope == "accelerator" && capability.device_id.is_none()
            || capability.scope == "storage" && capability.device_id.is_none()
            || capability.scope == "network" && capability.interface_name.is_none()
        {
            return false;
        }
    }
    true
}

fn valid_metric_identity(
    key: &str,
    scope: &str,
    device_id: Option<&str>,
    interface_name: Option<&str>,
    run_id: Option<&str>,
) -> bool {
    valid_metric_text(key, 96)
        && key
            .bytes()
            .next()
            .is_some_and(|byte| byte.is_ascii_lowercase())
        && key.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
        && matches!(
            scope,
            "node"
                | "accelerator"
                | "memory"
                | "storage"
                | "network"
                | "runtime"
                | "workload"
                | "service"
                | "benchmark"
        )
        && valid_optional_text(device_id, 128)
        && valid_optional_text(interface_name, 64)
        && valid_optional_text(run_id, 128)
}

fn valid_metric_text(value: &str, maximum: usize) -> bool {
    !value.is_empty() && value.chars().count() <= maximum && !value.chars().any(char::is_control)
}

fn valid_metric_value(value: &serde_json::Value) -> bool {
    match value {
        serde_json::Value::Null | serde_json::Value::Bool(_) => true,
        serde_json::Value::Number(number) => number
            .as_f64()
            .is_some_and(|value| value.is_finite() && value.abs() <= 1e15),
        serde_json::Value::String(value) => valid_metric_text(value, 256),
        serde_json::Value::Array(_) | serde_json::Value::Object(_) => false,
    }
}

fn read_bounded_text(path: &Path) -> Option<String> {
    let file = File::open(path).ok()?;
    let mut bytes = Vec::new();
    file.take(SOURCE_TEXT_LIMIT + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() > SOURCE_TEXT_LIMIT as usize {
        return None;
    }
    String::from_utf8(bytes).ok()
}

fn parse_cpu_counters(value: &str) -> Option<CpuCounters> {
    let line = value.lines().find(|line| line.starts_with("cpu "))?;
    let fields = line
        .split_ascii_whitespace()
        .skip(1)
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    if fields.len() < 4 {
        return None;
    }
    // Linux reports guest and guest_nice inside user and nice already. Summing
    // only the first eight counters avoids counting guest CPU time twice.
    let total = fields
        .iter()
        .take(8)
        .try_fold(0_u64, |total, value| total.checked_add(*value))?;
    let idle = fields[3].checked_add(fields.get(4).copied().unwrap_or(0))?;
    (idle <= total).then_some(CpuCounters { total, idle })
}

fn cpu_rate(previous: Option<CpuCounters>, current: Option<CpuCounters>) -> (Option<f64>, bool) {
    let Some(previous) = previous else {
        return (None, false);
    };
    let Some(current) = current else {
        return (None, false);
    };
    let Some(total) = current.total.checked_sub(previous.total) else {
        return (None, true);
    };
    let Some(idle) = current.idle.checked_sub(previous.idle) else {
        return (None, true);
    };
    if total == 0 || idle > total {
        return (None, true);
    }
    (Some((total - idle) as f64 * 100.0 / total as f64), false)
}

fn parse_load_average(value: &str) -> Option<(Option<f64>, Option<f64>, Option<f64>)> {
    let mut values = value
        .split_ascii_whitespace()
        .take(3)
        .map(|field| field.parse::<f64>().ok())
        .collect::<Vec<_>>();
    if values.len() != 3 {
        return None;
    }
    for value in &mut values {
        if value.is_some_and(|value| !value.is_finite() || !(0.0..=1_000_000.0).contains(&value)) {
            *value = None;
        }
    }
    Some((values[0], values[1], values[2]))
}

fn parse_memory(value: &str) -> Option<(u64, u64)> {
    let mut total = None;
    let mut available = None;
    for line in value.lines() {
        let mut fields = line.split_ascii_whitespace();
        match (fields.next(), fields.next(), fields.next(), fields.next()) {
            (Some("MemTotal:"), Some(amount), Some("kB"), None) => {
                total = amount.parse::<u64>().ok()?.checked_mul(1024)
            }
            (Some("MemAvailable:"), Some(amount), Some("kB"), None) => {
                available = amount.parse::<u64>().ok()?.checked_mul(1024)
            }
            _ => {}
        }
    }
    match (total, available) {
        (Some(total), Some(available)) if available <= total && total <= MAX_CAPACITY_BYTES => {
            Some((total, available))
        }
        _ => None,
    }
}

fn valid_capacity(value: &FileSystemCapacity) -> bool {
    value.free_bytes <= value.total_bytes && value.total_bytes <= MAX_CAPACITY_BYTES
}

fn parse_network_interfaces(value: &str) -> BTreeMap<String, NetworkInterfaceCounters> {
    let mut interfaces = BTreeMap::new();
    for line in value.lines() {
        let Some((name, counters)) = line.split_once(':') else {
            continue;
        };
        let name = name.trim();
        if name.is_empty() || name == "lo" || name.len() > 64 {
            continue;
        }
        let fields = counters
            .split_ascii_whitespace()
            .map(str::parse::<u64>)
            .collect::<Result<Vec<_>, _>>();
        let Ok(fields) = fields else { continue };
        if fields.len() != 16
            || !name.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'.' || byte == b'-'
            })
        {
            continue;
        }
        interfaces.insert(
            name.to_owned(),
            NetworkInterfaceCounters {
                receive: fields[0],
                transmit: fields[8],
            },
        );
    }
    interfaces
}

fn parse_network_counters(value: &str) -> Option<NetworkCounters> {
    let interfaces = parse_network_interfaces(value);
    let receive = interfaces
        .values()
        .try_fold(0_u64, |total, value| total.checked_add(value.receive))?;
    let transmit = interfaces
        .values()
        .try_fold(0_u64, |total, value| total.checked_add(value.transmit))?;
    (!interfaces.is_empty()).then_some(NetworkCounters { receive, transmit })
}

fn network_rates(
    previous: Option<&TelemetrySample>,
    current: Option<NetworkCounters>,
    observed_at: DateTime<Utc>,
) -> (Option<f64>, Option<f64>) {
    let Some(previous) = previous else {
        return (None, None);
    };
    let Some(previous_counters) = previous.network_counters else {
        return (None, None);
    };
    let Some(current) = current else {
        return (None, None);
    };
    let Ok(elapsed) = (observed_at - previous.observed_at).to_std() else {
        return (None, None);
    };
    let elapsed = elapsed.as_secs_f64();
    if elapsed <= 0.0 {
        return (None, None);
    }
    let receive = current
        .receive
        .checked_sub(previous_counters.receive)
        .map(|value| value as f64 / elapsed)
        .filter(|value| *value <= 1_000_000_000_000_000.0);
    let transmit = current
        .transmit
        .checked_sub(previous_counters.transmit)
        .map(|value| value as f64 / elapsed)
        .filter(|value| *value <= 1_000_000_000_000_000.0);
    (receive, transmit)
}

fn elapsed_seconds(previous: Option<DateTime<Utc>>, current: DateTime<Utc>) -> Option<f64> {
    let previous = previous?;
    let elapsed = (current - previous).to_std().ok()?.as_secs_f64();
    (elapsed > 0.0).then_some(elapsed)
}

fn counter_rate(previous: Option<u64>, current: u64, elapsed: Option<f64>) -> (Option<f64>, bool) {
    let Some(previous) = previous else {
        return (None, false);
    };
    let Some(elapsed) = elapsed else {
        return (None, false);
    };
    let Some(delta) = current.checked_sub(previous) else {
        return (None, true);
    };
    let rate = delta as f64 / elapsed;
    if rate.is_finite() && rate <= 1_000_000_000_000_000.0 {
        (Some(rate), false)
    } else {
        (None, false)
    }
}

fn read_disk_counters(path: &Path) -> BTreeMap<String, DiskCounters> {
    let Ok(entries) = fs::read_dir(path) else {
        return BTreeMap::new();
    };
    let mut devices = BTreeMap::new();
    for entry in entries.flatten().take(MAX_STORAGE_DEVICES) {
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if !file_type.is_dir() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.is_empty()
            || name.len() > 64
            || !name.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'.' || byte == b'-'
            })
        {
            continue;
        }
        let Some(value) = read_bounded_text(&entry.path().join("stat")) else {
            continue;
        };
        let fields = value
            .split_ascii_whitespace()
            .map(str::parse::<u64>)
            .collect::<Result<Vec<_>, _>>();
        let Ok(fields) = fields else { continue };
        if fields.len() < 7 {
            continue;
        }
        let Some(read_bytes) = fields[2].checked_mul(512) else {
            continue;
        };
        let Some(write_bytes) = fields[6].checked_mul(512) else {
            continue;
        };
        devices.insert(
            name,
            DiskCounters {
                read_bytes,
                write_bytes,
            },
        );
    }
    devices
}

fn read_cpu_temperature(path: &Path) -> Option<f64> {
    let entries = fs::read_dir(path).ok()?;
    for entry in entries.flatten().take(32) {
        if !entry.file_type().ok().is_some_and(|value| value.is_dir()) {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with("thermal_zone") {
            continue;
        }
        let kind = read_bounded_text(&entry.path().join("type"))
            .unwrap_or_default()
            .to_ascii_lowercase();
        if !(kind.contains("cpu") || kind.contains("package") || kind.contains("soc")) {
            continue;
        }
        let value = read_bounded_text(&entry.path().join("temp"))?
            .trim()
            .parse::<f64>()
            .ok()?;
        let celsius = if value.abs() > 1_000.0 {
            value / 1_000.0
        } else {
            value
        };
        if (-100.0..=300.0).contains(&celsius) {
            return Some(celsius);
        }
    }
    None
}

fn read_cpu_energy(path: &Path) -> Option<EnergyCounter> {
    let entries = fs::read_dir(path).ok()?;
    for entry in entries.flatten().take(16) {
        let file = entry.path().join("energy_uj");
        let Some(value) = read_bounded_text(&file) else {
            continue;
        };
        let Ok(value) = value.trim().parse::<u64>() else {
            continue;
        };
        return Some(EnergyCounter { microjoules: value });
    }
    None
}

fn series_base(
    identity: TelemetryMetricIdentity<'_>,
    value: serde_json::Value,
    context: TelemetrySeriesContext<'_>,
) -> TelemetrySeries {
    TelemetrySeries {
        node_id: None,
        key: identity.key.to_owned(),
        scope: identity.scope.to_owned(),
        device_id: identity.device_id.map(str::to_owned),
        process_id: None,
        process_name: None,
        interface_name: identity.interface_name.map(str::to_owned),
        run_id: identity.run_id.map(str::to_owned),
        value,
        unit: context.unit.to_owned(),
        source: metric_source(identity.key, identity.scope).to_owned(),
        measurement_kind: context.measurement_kind.to_owned(),
        observed_at: context.observed_at,
        received_at: None,
        freshness: "fresh".to_owned(),
        freshness_threshold_seconds: 6.0,
        support_status: "available".to_owned(),
        reason: None,
        aggregation: context.aggregation.to_owned(),
    }
}

fn series_number(
    identity: TelemetryMetricIdentity<'_>,
    value: f64,
    context: TelemetrySeriesContext<'_>,
) -> TelemetrySeries {
    series_base(identity, serde_json::json!(value), context)
}

fn series_text(
    identity: TelemetryMetricIdentity<'_>,
    value: String,
    context: TelemetrySeriesContext<'_>,
) -> TelemetrySeries {
    series_base(identity, serde_json::json!(value), context)
}

fn series_bool(
    identity: TelemetryMetricIdentity<'_>,
    value: bool,
    context: TelemetrySeriesContext<'_>,
) -> TelemetrySeries {
    series_base(identity, serde_json::json!(value), context)
}

fn capability(
    identity: TelemetryMetricIdentity<'_>,
    context: TelemetryCapabilityContext<'_>,
    supported: bool,
    reason: Option<&str>,
) -> TelemetryCapability {
    TelemetryCapability {
        node_id: None,
        key: identity.key.to_owned(),
        scope: identity.scope.to_owned(),
        device_id: identity.device_id.map(str::to_owned),
        process_id: None,
        process_name: None,
        interface_name: identity.interface_name.map(str::to_owned),
        run_id: identity.run_id.map(str::to_owned),
        unit: context.unit.to_owned(),
        source: metric_source(identity.key, identity.scope).to_owned(),
        measurement_kind: context.measurement_kind.to_owned(),
        supported,
        freshness_threshold_seconds: 6.0,
        reason: reason.map(str::to_owned),
    }
}

fn metric_source(key: &str, scope: &str) -> &'static str {
    match (scope, key) {
        ("accelerator", _) => "nvidia-smi",
        ("network", key) if key.starts_with("network.receive_") => "procfs:/proc/net/dev",
        ("network", key) if key.starts_with("network.transmit_") => "procfs:/proc/net/dev",
        ("network", _) => "sysfs:/sys/class/net",
        ("storage", key) if key.starts_with("storage.read_") => "sysfs:/sys/block",
        ("storage", key) if key.starts_with("storage.write_") => "sysfs:/sys/block",
        ("storage", _) => "statvfs",
        ("memory", key) if key.starts_with("memory.bandwidth_") => "nvidia-smi:dmon",
        ("memory", _) => "procfs:/proc/meminfo",
        ("node", "cpu.temperature_c") => "sysfs:/sys/class/thermal",
        ("node", "cpu.power_watts") => "sysfs:/sys/class/powercap",
        ("node", key) if key.starts_with("cpu.load_average") => "procfs:/proc/loadavg",
        ("node", key) if key.starts_with("cpu.") => "procfs:/proc/stat",
        ("runtime", _) => "runtime-adapter",
        ("workload", _) => "controller-correlation",
        _ => "vonk-native",
    }
}

fn optional_bool(value: &str) -> Option<bool> {
    if is_missing(value) {
        return None;
    }
    match value.to_ascii_lowercase().as_str() {
        "active" | "true" | "yes" | "1" => Some(true),
        "not active" | "inactive" | "false" | "no" | "0" => Some(false),
        _ => None,
    }
}

fn parse_gpu_processes(value: &[u8]) -> Vec<GpuProcessReading> {
    let Ok(value) = std::str::from_utf8(value) else {
        return Vec::new();
    };
    let mut processes = Vec::new();
    for line in value
        .lines()
        .filter(|line| !line.trim().is_empty())
        .take(MAX_GPU_PROCESSES)
    {
        let fields = line.split(',').map(str::trim).collect::<Vec<_>>();
        if fields.len() != 3 && fields.len() != 4 {
            continue;
        }
        let (device_id, pid_field, name_field, memory_field) = if fields.len() == 4 {
            (fields[0], fields[1], fields[2], fields[3])
        } else {
            ("unknown", fields[0], fields[1], fields[2])
        };
        let Ok(pid) = pid_field.parse::<u32>() else {
            continue;
        };
        if pid == 0
            || device_id.is_empty()
            || device_id.chars().count() > 128
            || name_field.is_empty()
            || name_field.chars().count() > 128
        {
            continue;
        }
        let Some(memory_bytes) = optional_mib(memory_field).flatten() else {
            continue;
        };
        processes.push(GpuProcessReading {
            device_id: device_id.to_owned(),
            pid,
            name: name_field.to_owned(),
            memory_bytes,
        });
    }
    processes
}

fn parse_memory_bandwidth(value: &[u8]) -> Option<f64> {
    let value = std::str::from_utf8(value).ok()?;
    let lines = value.lines().map(str::trim).filter(|line| !line.is_empty());
    let mut column = None;
    let mut data_lines = Vec::new();
    for line in lines {
        if line.starts_with('#') {
            continue;
        }
        let fields = line.split_ascii_whitespace().collect::<Vec<_>>();
        if fields.iter().any(|field| {
            field.eq_ignore_ascii_case("bw") || field.eq_ignore_ascii_case("bandwidth")
        }) {
            column = fields.iter().position(|field| {
                field.eq_ignore_ascii_case("bw") || field.eq_ignore_ascii_case("bandwidth")
            });
            continue;
        }
        if fields
            .first()
            .is_some_and(|field| field.parse::<u32>().is_ok())
        {
            data_lines.push(fields);
        }
    }
    if let Some(index) = column {
        let value = data_lines.first()?.get(index)?.parse::<f64>().ok()?;
        if value.is_finite() && value >= 0.0 {
            return Some(value * 1024.0 * 1024.0);
        }
    }
    None
}

#[derive(Debug, Clone)]
struct GpuProcessReading {
    device_id: String,
    pid: u32,
    name: String,
    memory_bytes: u64,
}

fn add_optional_series(
    series: &mut Vec<TelemetrySeries>,
    capabilities: &mut Vec<TelemetryCapability>,
    identity: TelemetryMetricIdentity<'_>,
    value: Option<f64>,
    context: TelemetrySeriesContext<'_>,
) {
    let supported = value.is_some();
    if let Some(value) = value {
        series.push(series_number(identity, value, context));
    }
    capabilities.push(capability(
        identity,
        capability_context(context.unit, context.measurement_kind),
        supported,
        (!supported).then_some("sensor unavailable from native interface"),
    ));
}

fn add_optional_text_series(
    series: &mut Vec<TelemetrySeries>,
    capabilities: &mut Vec<TelemetryCapability>,
    identity: TelemetryMetricIdentity<'_>,
    value: Option<String>,
    context: TelemetrySeriesContext<'_>,
) {
    let supported = value.is_some();
    if let Some(value) = value {
        series.push(series_text(identity, value, context));
    }
    capabilities.push(capability(
        identity,
        capability_context(context.unit, context.measurement_kind),
        supported,
        (!supported).then_some("sensor unavailable from native interface"),
    ));
}

struct AcceleratorReading {
    index: u32,
    name: Option<String>,
    utilization: Option<f64>,
    memory_total: Option<u64>,
    memory_used: Option<u64>,
    memory_free: Option<u64>,
    temperature: Option<f64>,
    power: Option<f64>,
    power_limit: Option<f64>,
    clock_sm: Option<f64>,
    clock_sm_max: Option<f64>,
    throttle_thermal: Option<bool>,
    throttle_hw: Option<bool>,
    throttle_power: Option<bool>,
    throttle_sw: Option<bool>,
    performance_state: Option<String>,
}

fn parse_accelerators(value: &[u8], _host_memory: Option<(u64, u64)>) -> Vec<AcceleratorReading> {
    let Ok(value) = std::str::from_utf8(value) else {
        return Vec::new();
    };
    let mut readings = Vec::new();
    for line in value
        .lines()
        .filter(|line| !line.trim().is_empty())
        .take(MAX_ACCELERATORS)
    {
        let fields = line.split(',').map(str::trim).collect::<Vec<_>>();
        let Some(reading) = parse_accelerator_fields(&fields) else {
            continue;
        };
        readings.push(reading);
    }
    readings
}

fn parse_accelerator_fields(fields: &[&str]) -> Option<AcceleratorReading> {
    let extended = fields.len() == 16;
    if !extended && fields.len() != 7 {
        return None;
    }
    let (
        index,
        name,
        utilization,
        memory_total,
        memory_used,
        memory_free,
        temperature,
        power,
        power_limit,
        clock_sm,
        clock_sm_max,
        throttle_thermal,
        throttle_hw,
        throttle_power,
        throttle_sw,
        performance_state,
    ) = if extended {
        (
            fields[0].parse::<u32>().ok()?,
            fields[1],
            fields[2],
            fields[3],
            fields[4],
            fields[5],
            fields[6],
            fields[7],
            fields[8],
            fields[9],
            fields[10],
            fields[11],
            fields[12],
            fields[13],
            fields[14],
            fields[15],
        )
    } else {
        (
            0, fields[0], fields[1], fields[2], "N/A", fields[3], fields[4], fields[5], "N/A",
            "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", fields[6],
        )
    };
    if name.is_empty() || name.chars().count() > 256 {
        return None;
    }
    let memory_total = optional_mib(memory_total)?;
    let memory_used = optional_mib(memory_used)?;
    let memory_free = optional_mib(memory_free)?;
    if memory_total.is_some() != memory_free.is_some()
        || memory_total
            .zip(memory_free)
            .is_some_and(|(total, free)| free > total)
        || memory_used.is_some_and(|used| memory_total.is_some_and(|total| used > total))
    {
        return None;
    }
    Some(AcceleratorReading {
        index,
        name: Some(name.to_owned()),
        utilization: optional_number(utilization, 0.0, 100.0)?,
        memory_total,
        memory_used,
        memory_free,
        temperature: optional_number(temperature, -100.0, 300.0)?,
        power: optional_number(power, 0.0, 100_000.0)?,
        power_limit: optional_number(power_limit, 0.0, 100_000.0)?,
        clock_sm: optional_number(clock_sm, 0.0, 1_000_000.0)?,
        clock_sm_max: optional_number(clock_sm_max, 0.0, 1_000_000.0)?,
        throttle_thermal: optional_bool(throttle_thermal),
        throttle_hw: optional_bool(throttle_hw),
        throttle_power: optional_bool(throttle_power),
        throttle_sw: optional_bool(throttle_sw),
        performance_state: optional_text(performance_state, 32)?,
    })
}

fn read_uptime_seconds(path: &Path) -> Option<u64> {
    let value = read_bounded_text(path)?
        .split_ascii_whitespace()
        .next()?
        .parse::<f64>()
        .ok()?;
    (value.is_finite() && value >= 0.0).then_some(value as u64)
}

#[allow(clippy::too_many_arguments)]
fn build_metrics(
    observed_at: DateTime<Utc>,
    uptime_seconds: Option<u64>,
    cpu_utilization: Option<f64>,
    load_average_1m: Option<f64>,
    load_average_5m: Option<f64>,
    load_average_15m: Option<f64>,
    memory: Option<(u64, u64)>,
    disk: Option<FileSystemCapacity>,
    cpu_temperature: Option<f64>,
    cpu_power: Option<f64>,
    memory_bandwidth: Option<f64>,
    network_receive: Option<f64>,
    network_transmit: Option<f64>,
    accelerators: &[AcceleratorReading],
    processes: &[GpuProcessReading],
    disk_series: &[TelemetrySeries],
    disk_capabilities: &[TelemetryCapability],
    interface_series: &[TelemetrySeries],
    interface_capabilities: &[TelemetryCapability],
    runtime_series: &[TelemetrySeries],
    runtime_capabilities: &[TelemetryCapability],
    runtimes: &[TelemetryRuntime],
) -> TelemetryMetrics {
    let mut series = Vec::new();
    let mut capabilities = Vec::new();
    let cpu_id = None;
    add_optional_series(
        &mut series,
        &mut capabilities,
        metric_identity("cpu.utilization_percent", "node", cpu_id, None, None),
        cpu_utilization,
        series_context("%", "derived", "last", observed_at),
    );
    add_optional_series(
        &mut series,
        &mut capabilities,
        metric_identity("cpu.load_average_1m", "node", cpu_id, None, None),
        load_average_1m,
        series_context("load", "measured", "last", observed_at),
    );
    add_optional_series(
        &mut series,
        &mut capabilities,
        metric_identity("cpu.load_average_5m", "node", cpu_id, None, None),
        load_average_5m,
        series_context("load", "measured", "last", observed_at),
    );
    add_optional_series(
        &mut series,
        &mut capabilities,
        metric_identity("cpu.load_average_15m", "node", cpu_id, None, None),
        load_average_15m,
        series_context("load", "measured", "last", observed_at),
    );
    add_optional_series(
        &mut series,
        &mut capabilities,
        metric_identity("cpu.temperature_c", "node", cpu_id, None, None),
        cpu_temperature,
        series_context("degC", "measured", "last", observed_at),
    );
    add_optional_series(
        &mut series,
        &mut capabilities,
        metric_identity("cpu.power_watts", "node", cpu_id, None, None),
        cpu_power,
        series_context("W", "measured", "last", observed_at),
    );
    add_optional_series(
        &mut series,
        &mut capabilities,
        metric_identity(
            "memory.bandwidth_bytes_per_second",
            "memory",
            None,
            None,
            None,
        ),
        memory_bandwidth,
        series_context("bytes/s", "measured", "last", observed_at),
    );
    let shared_unified_pool = accelerators
        .iter()
        .any(|accelerator| shared_memory_pool(accelerator.name.as_deref()));
    let pool_id = if shared_unified_pool {
        "unified-memory-pool"
    } else {
        "physical-memory-pool"
    };
    if let Some((total, available)) = memory {
        let total = total as f64;
        let available = available as f64;
        let used = (total - available).max(0.0);
        add_optional_series(
            &mut series,
            &mut capabilities,
            metric_identity("memory.total_bytes", "memory", None, None, None),
            Some(total),
            series_context("bytes", "measured", "last", observed_at),
        );
        add_optional_series(
            &mut series,
            &mut capabilities,
            metric_identity("memory.available_bytes", "memory", None, None, None),
            Some(available),
            series_context("bytes", "measured", "last", observed_at),
        );
        add_optional_series(
            &mut series,
            &mut capabilities,
            metric_identity("memory.used_bytes", "memory", None, None, None),
            Some(used),
            series_context("bytes", "derived", "last", observed_at),
        );
        add_optional_series(
            &mut series,
            &mut capabilities,
            metric_identity("memory.used_percent", "memory", None, None, None),
            (total > 0.0).then_some(used * 100.0 / total),
            series_context("%", "derived", "last", observed_at),
        );
        add_optional_text_series(
            &mut series,
            &mut capabilities,
            metric_identity("memory.pool", "memory", Some(pool_id), None, None),
            Some(if shared_unified_pool {
                "shared-unified".to_owned()
            } else {
                "physical".to_owned()
            }),
            series_context("kind", "measured", "last", observed_at),
        );
        add_optional_text_series(
            &mut series,
            &mut capabilities,
            metric_identity("memory.pool_id", "memory", Some(pool_id), None, None),
            Some(pool_id.to_owned()),
            series_context("identity", "configured", "last", observed_at),
        );
        // Used percent is a capacity measurement.  It is not an OOM signal,
        // so leave the named OOM metric unavailable until a producer exposes
        // reclaim/failure pressure with a documented basis.
        capabilities.push(capability(
            metric_identity("memory.oom_pressure_percent", "memory", None, None, None),
            capability_context("%", "derived"),
            false,
            Some("OOM pressure producer unavailable; memory.used_percent is not an OOM signal"),
        ));
    } else {
        for (key, unit, kind) in [
            ("memory.total_bytes", "bytes", "measured"),
            ("memory.available_bytes", "bytes", "measured"),
            ("memory.used_bytes", "bytes", "derived"),
            ("memory.used_percent", "%", "derived"),
            ("memory.oom_pressure_percent", "%", "derived"),
        ] {
            let reason = if key == "memory.oom_pressure_percent" {
                "OOM pressure producer unavailable; memory.used_percent is not an OOM signal"
            } else {
                "memory counters unavailable"
            };
            capabilities.push(capability(
                metric_identity(key, "memory", None, None, None),
                capability_context(unit, kind),
                false,
                Some(reason),
            ));
        }
        capabilities.push(capability(
            metric_identity("memory.pool", "memory", Some(pool_id), None, None),
            capability_context("kind", "measured"),
            false,
            Some("memory counters unavailable"),
        ));
        capabilities.push(capability(
            metric_identity("memory.pool_id", "memory", Some(pool_id), None, None),
            capability_context("identity", "configured"),
            false,
            Some("memory counters unavailable"),
        ));
    }

    add_optional_series(
        &mut series,
        &mut capabilities,
        metric_identity("network.receive_bytes_per_second", "node", None, None, None),
        network_receive,
        series_context("bytes/s", "derived", "counter_rate", observed_at),
    );
    add_optional_series(
        &mut series,
        &mut capabilities,
        metric_identity(
            "network.transmit_bytes_per_second",
            "node",
            None,
            None,
            None,
        ),
        network_transmit,
        series_context("bytes/s", "derived", "counter_rate", observed_at),
    );
    series.extend(interface_series.iter().cloned());
    series.extend(disk_series.iter().cloned());
    capabilities.extend(interface_capabilities.iter().cloned());
    capabilities.extend(disk_capabilities.iter().cloned());
    series.extend(runtime_series.iter().cloned());
    capabilities.extend(runtime_capabilities.iter().cloned());
    if let Some(disk) = disk {
        let total = disk.total_bytes as f64;
        let free = disk.free_bytes as f64;
        for (key, value, unit, kind) in [
            ("storage.total_bytes", total, "bytes", "measured"),
            ("storage.free_bytes", free, "bytes", "measured"),
            (
                "storage.used_bytes",
                (total - free).max(0.0),
                "bytes",
                "derived",
            ),
        ] {
            add_optional_series(
                &mut series,
                &mut capabilities,
                metric_identity(key, "storage", Some("configured-store"), None, None),
                Some(value),
                series_context(unit, kind, "last", observed_at),
            );
        }
        add_optional_series(
            &mut series,
            &mut capabilities,
            metric_identity(
                "storage.used_percent",
                "storage",
                Some("configured-store"),
                None,
                None,
            ),
            (total > 0.0).then_some((total - free).max(0.0) * 100.0 / total),
            series_context("%", "derived", "last", observed_at),
        );
    } else {
        for (key, unit, kind) in [
            ("storage.total_bytes", "bytes", "measured"),
            ("storage.free_bytes", "bytes", "measured"),
            ("storage.used_bytes", "bytes", "derived"),
            ("storage.used_percent", "%", "derived"),
        ] {
            capabilities.push(capability(
                metric_identity(key, "storage", Some("configured-store"), None, None),
                capability_context(unit, kind),
                false,
                Some("filesystem capacity unavailable"),
            ));
        }
    }

    if accelerators.is_empty() {
        for (key, unit, kind) in gpu_metric_capabilities() {
            capabilities.push(capability(
                metric_identity(key, "accelerator", Some("unknown"), None, None),
                capability_context(unit, kind),
                false,
                Some("NVIDIA native interface unavailable"),
            ));
        }
    }
    for accelerator in accelerators {
        let device_id = accelerator.index.to_string();
        let shared_memory = shared_memory_pool(accelerator.name.as_deref());
        for (key, value, unit, kind, aggregation) in [
            (
                "gpu.utilization_percent",
                accelerator.utilization,
                "%",
                "measured",
                "last",
            ),
            (
                "gpu.temperature_c",
                accelerator.temperature,
                "degC",
                "measured",
                "last",
            ),
            (
                "gpu.power_watts",
                accelerator.power,
                "W",
                "measured",
                "last",
            ),
            (
                "gpu.power_limit_watts",
                accelerator.power_limit,
                "W",
                "configured",
                "last",
            ),
            (
                "gpu.clock_sm_mhz",
                accelerator.clock_sm,
                "MHz",
                "measured",
                "last",
            ),
            (
                "gpu.clock_sm_max_mhz",
                accelerator.clock_sm_max,
                "MHz",
                "configured",
                "last",
            ),
            (
                "gpu.memory_total_bytes",
                (!shared_memory)
                    .then(|| accelerator.memory_total.map(|value| value as f64))
                    .flatten(),
                "bytes",
                "measured",
                "last",
            ),
            (
                "gpu.memory_used_bytes",
                accelerator.memory_used.map(|value| value as f64),
                "bytes",
                "measured",
                "last",
            ),
            (
                "gpu.memory_free_bytes",
                (!shared_memory)
                    .then(|| accelerator.memory_free.map(|value| value as f64))
                    .flatten(),
                "bytes",
                "measured",
                "last",
            ),
        ] {
            add_optional_series(
                &mut series,
                &mut capabilities,
                metric_identity(key, "accelerator", Some(&device_id), None, None),
                value,
                series_context(unit, kind, aggregation, observed_at),
            );
        }
        let memory_percent = accelerator
            .memory_total
            .zip(accelerator.memory_used)
            .and_then(|(total, used)| (total > 0).then_some(used as f64 * 100.0 / total as f64));
        add_optional_series(
            &mut series,
            &mut capabilities,
            metric_identity(
                "gpu.memory_used_percent",
                "accelerator",
                Some(&device_id),
                None,
                None,
            ),
            memory_percent,
            series_context("%", "derived", "last", observed_at),
        );
        add_optional_text_series(
            &mut series,
            &mut capabilities,
            metric_identity("gpu.name", "accelerator", Some(&device_id), None, None),
            accelerator.name.clone(),
            series_context("name", "measured", "last", observed_at),
        );
        add_optional_text_series(
            &mut series,
            &mut capabilities,
            metric_identity(
                "gpu.performance_state",
                "accelerator",
                Some(&device_id),
                None,
                None,
            ),
            accelerator.performance_state.clone(),
            series_context("state", "measured", "last", observed_at),
        );
        for (key, value) in [
            ("gpu.throttle_thermal", accelerator.throttle_thermal),
            ("gpu.throttle_hardware", accelerator.throttle_hw),
            ("gpu.throttle_power_cap", accelerator.throttle_power),
            ("gpu.throttle_software", accelerator.throttle_sw),
        ] {
            if let Some(value) = value {
                series.push(series_bool(
                    metric_identity(key, "accelerator", Some(&device_id), None, None),
                    value,
                    series_context("boolean", "measured", "last", observed_at),
                ));
            }
            capabilities.push(capability(
                metric_identity(key, "accelerator", Some(&device_id), None, None),
                capability_context("boolean", "measured"),
                value.is_some(),
                (!value.is_some()).then_some("throttle reason unavailable"),
            ));
        }
        let throttle_flags = [
            accelerator.throttle_thermal,
            accelerator.throttle_hw,
            accelerator.throttle_power,
            accelerator.throttle_sw,
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
        let active_throttle =
            (!throttle_flags.is_empty()).then(|| throttle_flags.into_iter().any(|value| value));
        if let Some(active_throttle) = active_throttle {
            series.push(series_bool(
                metric_identity(
                    "gpu.throttle_active",
                    "accelerator",
                    Some(&device_id),
                    None,
                    None,
                ),
                active_throttle,
                series_context("boolean", "derived", "last", observed_at),
            ));
        }
        capabilities.push(capability(
            metric_identity(
                "gpu.throttle_active",
                "accelerator",
                Some(&device_id),
                None,
                None,
            ),
            capability_context("boolean", "derived"),
            active_throttle.is_some(),
            (!active_throttle.is_some()).then_some("all throttle reason flags unavailable"),
        ));
    }
    for process in processes.iter().take(MAX_GPU_PROCESSES) {
        let mut item = series_number(
            metric_identity(
                "gpu.process_memory_bytes",
                "accelerator",
                Some(&process.device_id),
                None,
                None,
            ),
            process.memory_bytes as f64,
            series_context("bytes", "measured", "last", observed_at),
        );
        item.process_id = Some(process.pid);
        item.process_name = Some(process.name.clone());
        series.push(item);
    }
    if processes.is_empty() {
        capabilities.push(capability(
            metric_identity(
                "gpu.process_memory_bytes",
                "accelerator",
                Some("unknown"),
                None,
                None,
            ),
            capability_context("bytes", "measured"),
            false,
            Some("no GPU compute process data available"),
        ));
    }

    TelemetryMetrics {
        schema_version: 2,
        series: series.into_iter().take(MAX_METRIC_SERIES).collect(),
        capabilities: capabilities.into_iter().take(MAX_CAPABILITIES).collect(),
        runtimes: runtimes.iter().take(32).cloned().collect(),
        workloads: Vec::new(),
        provenance: TelemetryProvenance {
            collector: "vonk-native".to_owned(),
            collector_version: "2".to_owned(),
            host_uptime_seconds: uptime_seconds,
            source_observed_at: Some(observed_at),
        },
    }
}

fn gpu_metric_capabilities() -> Vec<(&'static str, &'static str, &'static str)> {
    vec![
        ("gpu.utilization_percent", "%", "measured"),
        ("gpu.temperature_c", "degC", "measured"),
        ("gpu.power_watts", "W", "measured"),
        ("gpu.power_limit_watts", "W", "configured"),
        ("gpu.clock_sm_mhz", "MHz", "measured"),
        ("gpu.clock_sm_max_mhz", "MHz", "configured"),
        ("gpu.memory_total_bytes", "bytes", "measured"),
        ("gpu.memory_used_bytes", "bytes", "measured"),
        ("gpu.memory_free_bytes", "bytes", "measured"),
        ("gpu.memory_used_percent", "%", "derived"),
        ("gpu.name", "name", "measured"),
        ("gpu.performance_state", "state", "measured"),
        ("gpu.throttle_active", "boolean", "derived"),
        ("gpu.throttle_thermal", "boolean", "measured"),
        ("gpu.throttle_hardware", "boolean", "measured"),
        ("gpu.throttle_power_cap", "boolean", "measured"),
        ("gpu.throttle_software", "boolean", "measured"),
        ("gpu.process_memory_bytes", "bytes", "measured"),
    ]
}

fn runtime_metric_capabilities() -> Vec<(&'static str, &'static str, &'static str)> {
    vec![
        ("runtime.decode_tokens_per_second", "tokens/s", "derived"),
        ("runtime.prefill_tokens_per_second", "tokens/s", "derived"),
        (
            "runtime.prefill_cached_tokens_per_second",
            "tokens/s",
            "derived",
        ),
        (
            "runtime.prefill_uncached_tokens_per_second",
            "tokens/s",
            "derived",
        ),
        ("runtime.output_tokens_total", "tokens", "measured"),
        ("runtime.slots_active", "requests", "measured"),
        ("runtime.requests_running", "requests", "measured"),
        ("runtime.requests_waiting", "requests", "measured"),
        ("runtime.kv_cache_usage_percent", "%", "measured"),
        ("runtime.preemptions_total", "count", "measured"),
        ("runtime.prefix_cache_hit_percent", "%", "derived"),
        ("runtime.mtp_acceptance_percent", "%", "derived"),
        ("runtime.ttft_p95_ms", "ms", "derived"),
        ("runtime.e2e_p95_ms", "ms", "derived"),
        ("runtime.itl_p95_ms", "ms", "derived"),
    ]
}

fn is_missing(value: &str) -> bool {
    value.is_empty() || matches!(value, "N/A" | "[N/A]")
}

fn shared_memory_pool(name: Option<&str>) -> bool {
    name.is_some_and(|value| value.to_ascii_lowercase().contains("gb10"))
}

fn optional_number(value: &str, minimum: f64, maximum: f64) -> Option<Option<f64>> {
    if is_missing(value) {
        return Some(None);
    }
    let value = value.parse::<f64>().ok()?;
    (value.is_finite() && (minimum..=maximum).contains(&value)).then_some(Some(value))
}

fn optional_mib(value: &str) -> Option<Option<u64>> {
    if is_missing(value) {
        return Some(None);
    }
    let value = value.parse::<u64>().ok()?.checked_mul(1024 * 1024)?;
    (value <= MAX_CAPACITY_BYTES).then_some(Some(value))
}

fn optional_text(value: &str, maximum_chars: usize) -> Option<Option<String>> {
    if is_missing(value) {
        return Some(None);
    }
    (value.chars().count() <= maximum_chars).then(|| Some(value.to_owned()))
}
