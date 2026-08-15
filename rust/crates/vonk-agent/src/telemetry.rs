use std::{
    collections::VecDeque,
    fs::{self, File},
    io::Read,
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
pub const MAX_REPORT_SAMPLES: usize = 16;
pub const COLLECTION_INTERVAL: Duration = Duration::from_secs(2);

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
    #[serde(skip)]
    cpu_counters: Option<CpuCounters>,
    #[serde(skip)]
    network_counters: Option<NetworkCounters>,
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
    pub meminfo: PathBuf,
    pub net_dev: PathBuf,
    pub store: PathBuf,
}

pub struct TelemetryCollector<R, F> {
    runner: R,
    filesystem: F,
    paths: TelemetryPaths,
    boot_id: Uuid,
    sequences: DurableSequenceAllocator,
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
        let metadata = fs::symlink_metadata(path)?;
        if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
            return Err(TelemetryError::InvalidSequenceState);
        }
        let mut connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
        connection.busy_timeout(Duration::from_secs(1))?;
        connection.execute_batch(
            "PRAGMA synchronous=FULL;
             PRAGMA foreign_keys=ON;
             PRAGMA trusted_schema=OFF;",
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
        let sequences = DurableSequenceAllocator::open(&paths.store.join("state.sqlite"), boot_id)?;
        Ok(Self {
            runner,
            filesystem,
            paths,
            boot_id,
            sequences,
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
        let cpu_utilization_percent = cpu_rate(
            previous.and_then(|sample| sample.cpu_counters),
            cpu_counters,
        );
        let load_average_1m = read_bounded_text(&self.paths.loadavg)
            .as_deref()
            .and_then(parse_load_average);
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
        let accelerator = self
            .runner
            .run(
                Program::NvidiaSmi,
                &[
                    "--query-gpu=name,utilization.gpu,memory.total,memory.free,temperature.gpu,power.draw,pstate".to_owned(),
                    "--format=csv,noheader,nounits".to_owned(),
                ],
                Duration::from_secs(10),
            )
            .ok()
            .filter(|output| output.success && output.stdout.len() <= SOURCE_TEXT_LIMIT as usize)
            .and_then(|output| parse_accelerator(&output.stdout, memory));

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
            gpu_memory_total_bytes: accelerator.as_ref().and_then(|value| value.memory_total),
            gpu_memory_free_bytes: accelerator.as_ref().and_then(|value| value.memory_free),
            temperature_c: accelerator.as_ref().and_then(|value| value.temperature),
            power_watts: accelerator.as_ref().and_then(|value| value.power),
            network_receive_bytes_per_second,
            network_transmit_bytes_per_second,
            gap_samples: 0,
            details: accelerator
                .map(|value| TelemetryDetails {
                    accelerator_name: value.name,
                    accelerator_performance_state: value.performance_state,
                })
                .unwrap_or_default(),
            cpu_counters,
            network_counters,
        })
    }
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
        if self.samples.len() == MAX_QUEUE_SAMPLES {
            if let Some(dropped) = self.samples.pop_front() {
                let lost = dropped.gap_samples.saturating_add(1);
                if let Some(oldest_retained) = self.samples.front_mut() {
                    oldest_retained.gap_samples = oldest_retained.gap_samples.saturating_add(lost);
                }
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

fn cpu_rate(previous: Option<CpuCounters>, current: Option<CpuCounters>) -> Option<f64> {
    let previous = previous?;
    let current = current?;
    let total = current.total.checked_sub(previous.total)?;
    let idle = current.idle.checked_sub(previous.idle)?;
    if total == 0 || idle > total {
        return None;
    }
    Some((total - idle) as f64 * 100.0 / total as f64)
}

fn parse_load_average(value: &str) -> Option<f64> {
    let load = value.split_ascii_whitespace().next()?.parse::<f64>().ok()?;
    (load.is_finite() && (0.0..=1_000_000.0).contains(&load)).then_some(load)
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

fn parse_network_counters(value: &str) -> Option<NetworkCounters> {
    let mut receive = 0_u64;
    let mut transmit = 0_u64;
    let mut found = false;
    for line in value.lines() {
        let Some((name, counters)) = line.split_once(':') else {
            continue;
        };
        if name.trim() == "lo" {
            continue;
        }
        let fields = counters
            .split_ascii_whitespace()
            .map(str::parse::<u64>)
            .collect::<Result<Vec<_>, _>>()
            .ok()?;
        if fields.len() != 16 {
            return None;
        }
        receive = receive.checked_add(fields[0])?;
        transmit = transmit.checked_add(fields[8])?;
        found = true;
    }
    found.then_some(NetworkCounters { receive, transmit })
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

struct AcceleratorReading {
    name: Option<String>,
    utilization: Option<f64>,
    memory_total: Option<u64>,
    memory_free: Option<u64>,
    temperature: Option<f64>,
    power: Option<f64>,
    performance_state: Option<String>,
}

fn parse_accelerator(value: &[u8], host_memory: Option<(u64, u64)>) -> Option<AcceleratorReading> {
    let value = std::str::from_utf8(value).ok()?;
    let lines = value
        .lines()
        .filter(|line| !line.trim().is_empty())
        .collect::<Vec<_>>();
    if lines.len() != 1 {
        return None;
    }
    let fields = lines[0].split(',').map(str::trim).collect::<Vec<_>>();
    if fields.len() != 7 || fields[0].is_empty() || fields[0].chars().count() > 256 {
        return None;
    }
    let name = fields[0].to_owned();
    let utilization = optional_number(fields[1], 0.0, 100.0)?;
    let mut memory_total = optional_mib(fields[2])?;
    let mut memory_free = optional_mib(fields[3])?;
    if name == "NVIDIA GB10" && memory_total.is_none() && memory_free.is_none() {
        (memory_total, memory_free) = host_memory
            .map(|(total, free)| (Some(total), Some(free)))
            .unwrap_or((None, None));
    }
    if memory_total.is_some() != memory_free.is_some()
        || memory_total
            .zip(memory_free)
            .is_some_and(|(total, free)| free > total)
    {
        return None;
    }
    let temperature = optional_number(fields[4], -100.0, 300.0)?;
    let power = optional_number(fields[5], 0.0, 100_000.0)?;
    let performance_state = optional_text(fields[6], 32)?;
    Some(AcceleratorReading {
        name: Some(name),
        utilization,
        memory_total,
        memory_free,
        temperature,
        power,
        performance_state,
    })
}

fn is_missing(value: &str) -> bool {
    value.is_empty() || matches!(value, "N/A" | "[N/A]")
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
