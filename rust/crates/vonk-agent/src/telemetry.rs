use std::{
    fs::File,
    io::Read,
    path::{Path, PathBuf},
    time::Duration,
};

use chrono::{DateTime, Utc};
use thiserror::Error;
use uuid::Uuid;

use crate::{
    inventory::shared_memory_pool,
    process::{ProcessRunner, Program},
};

const SOURCE_TEXT_LIMIT: u64 = 64 * 1024;
const MAX_CAPACITY_BYTES: u64 = 16 * 1024_u64.pow(4);
pub const MAX_REPORT_SAMPLES: usize = 16;

pub use vonk_agent_protocol::generated::{TelemetryRequest, TelemetrySample};

#[derive(Debug, Error)]
pub enum TelemetryError {
    #[error("telemetry boot identity is invalid")]
    InvalidBootId,
    #[error("telemetry filesystem access failed")]
    Io(#[from] std::io::Error),
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
    pub meminfo: PathBuf,
    pub store: PathBuf,
}

pub struct TelemetryCollector<R, F> {
    runner: R,
    filesystem: F,
    paths: TelemetryPaths,
    boot_id: Uuid,
}

impl<R: ProcessRunner, F: FileSystemProvider> TelemetryCollector<R, F> {
    pub fn new(
        runner: R,
        filesystem: F,
        paths: TelemetryPaths,
        boot_id: Uuid,
    ) -> Result<Self, TelemetryError> {
        if boot_id.is_nil() {
            return Err(TelemetryError::InvalidBootId);
        }
        Ok(Self {
            runner,
            filesystem,
            paths,
            boot_id,
        })
    }

    pub fn sample(&self) -> TelemetrySample {
        self.sample_at(Utc::now())
    }

    pub fn sample_at(&self, observed_at: DateTime<Utc>) -> TelemetrySample {
        let memory = read_bounded_text(&self.paths.meminfo)
            .as_deref()
            .and_then(parse_memory);
        let disk = self
            .filesystem
            .capacity(&self.paths.store)
            .ok()
            .filter(|value| {
                value.free_bytes <= value.total_bytes && value.total_bytes <= MAX_CAPACITY_BYTES
            });
        let accelerator = self
            .runner
            .run(
                Program::NvidiaSmi,
                &[
                    "--query-gpu=name,utilization.gpu,memory.total,memory.free".to_owned(),
                    "--format=csv,noheader,nounits".to_owned(),
                ],
                Duration::from_secs(10),
            )
            .ok()
            .filter(|output| output.success && output.stdout.len() <= SOURCE_TEXT_LIMIT as usize)
            .and_then(|output| parse_first_accelerator(&output.stdout));
        // GB10 exposes one physical unified pool. Keep that capacity in the
        // memory fields so consumers cannot sum RAM and VRAM twice.
        let dedicated = accelerator
            .as_ref()
            .filter(|value| !shared_memory_pool(Some(value.name.as_str())))
            .and_then(|value| value.memory);
        TelemetrySample {
            boot_id: self.boot_id,
            observed_at: observed_at.fixed_offset(),
            memory_total_bytes: memory.map(|value| value.0),
            memory_available_bytes: memory.map(|value| value.1),
            disk_total_bytes: disk.map(|value| value.total_bytes),
            disk_free_bytes: disk.map(|value| value.free_bytes),
            gpu_utilization_percent: accelerator.and_then(|value| value.utilization),
            gpu_memory_total_bytes: dedicated.map(|value| value.0),
            gpu_memory_free_bytes: dedicated.map(|value| value.1),
        }
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

pub fn valid_report_batch(samples: &[TelemetrySample]) -> bool {
    if samples.is_empty() || samples.len() > MAX_REPORT_SAMPLES {
        return false;
    }
    let mut previous_observed_at = None;
    for sample in samples {
        let observed_at = sample.observed_at;
        if sample.boot_id.is_nil()
            || previous_observed_at.is_some_and(|previous| observed_at <= previous)
            || !valid_capacity_pair(sample.memory_total_bytes, sample.memory_available_bytes)
            || !valid_capacity_pair(sample.disk_total_bytes, sample.disk_free_bytes)
            || !valid_capacity_pair(sample.gpu_memory_total_bytes, sample.gpu_memory_free_bytes)
            || sample
                .gpu_utilization_percent
                .is_some_and(|value| !(0.0..=100.0).contains(&value))
        {
            return false;
        }
        previous_observed_at = Some(observed_at);
    }
    true
}

fn valid_capacity_pair(total: Option<u64>, free: Option<u64>) -> bool {
    match (total, free) {
        (None, None) => true,
        (Some(total), Some(free)) => free <= total && total <= MAX_CAPACITY_BYTES,
        _ => false,
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

struct AcceleratorReading {
    name: String,
    utilization: Option<f64>,
    /// (total, free) bytes, when the device reports dedicated memory.
    memory: Option<(u64, u64)>,
}

fn parse_first_accelerator(value: &[u8]) -> Option<AcceleratorReading> {
    let line = std::str::from_utf8(value)
        .ok()?
        .lines()
        .find(|line| !line.trim().is_empty())?;
    let fields = line.split(',').map(str::trim).collect::<Vec<_>>();
    let [name, utilization, memory_total, memory_free] = fields.as_slice() else {
        return None;
    };
    if name.is_empty() || name.chars().count() > 256 {
        return None;
    }
    let memory = match (optional_mib(memory_total)?, optional_mib(memory_free)?) {
        (Some(total), Some(free)) if free <= total => Some((total, free)),
        (None, None) => None,
        _ => return None,
    };
    let utilization = if is_missing(utilization) {
        None
    } else {
        let value = utilization.parse::<f64>().ok()?;
        (value.is_finite() && (0.0..=100.0).contains(&value)).then_some(value)
    };
    Some(AcceleratorReading {
        name: (*name).to_owned(),
        utilization,
        memory,
    })
}

fn is_missing(value: &str) -> bool {
    value.is_empty() || matches!(value, "N/A" | "[N/A]")
}

fn optional_mib(value: &str) -> Option<Option<u64>> {
    if is_missing(value) {
        return Some(None);
    }
    let value = value.parse::<u64>().ok()?.checked_mul(1024 * 1024)?;
    (value <= MAX_CAPACITY_BYTES).then_some(Some(value))
}
