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
    process::{ProcessError, ProcessRunner, Program},
};

const SOURCE_TEXT_LIMIT: u64 = 64 * 1024;
const MAX_CAPACITY_BYTES: u64 = 16 * 1024_u64.pow(4);
pub const MAX_REPORT_SAMPLES: usize = 16;

pub use vonk_agent_protocol::generated::{GpuUnavailableReason, TelemetryRequest, TelemetrySample};

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
    /// `/sys/devices/system/cpu`: per-CPU `cpufreq` clocks live below it.
    pub cpu_root: PathBuf,
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
        let accelerator_result = self
            .runner
            .run(
                Program::NvidiaSmi,
                &[
                    "--query-gpu=name,utilization.gpu,memory.total,memory.free,temperature.gpu"
                        .to_owned(),
                    "--format=csv,noheader,nounits".to_owned(),
                ],
                Duration::from_secs(10),
            )
            .map_err(|error| match error {
                ProcessError::Timeout => GpuUnavailableReason::GpuCommandTimeout,
                ProcessError::OutputLimit => GpuUnavailableReason::GpuInvalidOutput,
                _ => GpuUnavailableReason::GpuCommandFailed,
            })
            .and_then(|output| {
                if !output.success {
                    return Err(GpuUnavailableReason::GpuCommandFailed);
                }
                if output.stdout.len() > SOURCE_TEXT_LIMIT as usize {
                    return Err(GpuUnavailableReason::GpuInvalidOutput);
                }
                parse_first_accelerator(&output.stdout)
                    .ok_or(GpuUnavailableReason::GpuInvalidOutput)
            });
        let gpu_unavailable_reason = match &accelerator_result {
            Err(reason) => Some(*reason),
            Ok(value)
                if value.utilization.is_none()
                    && value.temperature_c.is_none()
                    && value.memory.is_none() =>
            {
                Some(GpuUnavailableReason::GpuUnsupportedMetrics)
            }
            Ok(_) => None,
        };
        let accelerator = accelerator_result.ok();
        let cpu_frequency = read_cpu_frequency(&self.paths.cpu_root);
        // GB10 exposes one physical unified pool. Keep that capacity in the
        // memory fields so consumers cannot sum RAM and VRAM twice.
        let dedicated = accelerator
            .as_ref()
            .filter(|value| !shared_memory_pool(Some(value.name.as_str())))
            .and_then(|value| value.memory);
        let health =
            crate::identity::renewal_health(&self.paths.store.join("credentials"), observed_at);
        TelemetrySample {
            renewal_failed: health.and_then(|(failed, _)| failed),
            credential_remaining_fraction: health.map(|(_, remaining)| remaining),
            boot_id: self.boot_id,
            observed_at: observed_at.fixed_offset(),
            memory_total_bytes: memory.map(|value| value.0),
            memory_available_bytes: memory.map(|value| value.1),
            disk_total_bytes: disk.map(|value| value.total_bytes),
            disk_free_bytes: disk.map(|value| value.free_bytes),
            gpu_unavailable_reason,
            gpu_utilization_percent: accelerator.as_ref().and_then(|value| value.utilization),
            gpu_memory_total_bytes: dedicated.map(|value| value.0),
            gpu_memory_free_bytes: dedicated.map(|value| value.1),
            gpu_temperature_c: accelerator.as_ref().and_then(|value| value.temperature_c),
            cpu_frequency_avg_mhz: cpu_frequency.map(|value| value.avg_mhz),
            cpu_frequency_min_mhz: cpu_frequency.map(|value| value.min_mhz),
            cpu_frequency_max_mhz: cpu_frequency.and_then(|value| value.max_mhz),
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct CpuFrequency {
    avg_mhz: u32,
    min_mhz: u32,
    /// Hardware limit (`cpuinfo_max_freq`), not the current cap.
    max_mhz: Option<u32>,
}

const MAX_CPUS: usize = 1024;
const MAX_CPU_MHZ: u32 = 20_000;

/// Summarize `cpu*/cpufreq/scaling_cur_freq` (kHz). Any missing or malformed
/// entry is skipped; without a single current reading the result is `None`.
fn read_cpu_frequency(root: &Path) -> Option<CpuFrequency> {
    let mut current = Vec::new();
    let mut maximum: Option<u32> = None;
    let mut seen = 0;
    for entry in std::fs::read_dir(root).ok()?.flatten() {
        let name = entry.file_name();
        let Some(index) = name.to_str().and_then(|value| value.strip_prefix("cpu")) else {
            continue;
        };
        if index.is_empty() || !index.bytes().all(|byte| byte.is_ascii_digit()) {
            continue;
        }
        seen += 1;
        if seen > MAX_CPUS {
            break;
        }
        let cpufreq = entry.path().join("cpufreq");
        if let Some(mhz) = read_khz_as_mhz(&cpufreq.join("scaling_cur_freq")) {
            current.push(mhz);
        }
        if let Some(mhz) = read_khz_as_mhz(&cpufreq.join("cpuinfo_max_freq")) {
            maximum = maximum.max(Some(mhz));
        }
    }
    let min_mhz = *current.iter().min()?;
    let sum: u64 = current.iter().map(|value| u64::from(*value)).sum();
    let avg_mhz = u32::try_from((sum + current.len() as u64 / 2) / current.len() as u64).ok()?;
    Some(CpuFrequency {
        avg_mhz,
        min_mhz,
        max_mhz: maximum,
    })
}

fn read_khz_as_mhz(path: &Path) -> Option<u32> {
    let khz = read_bounded_text(path)?.trim().parse::<u32>().ok()?;
    let mhz = (khz + 500) / 1000;
    (1..=MAX_CPU_MHZ).contains(&mhz).then_some(mhz)
}

struct AcceleratorReading {
    name: String,
    temperature_c: Option<u32>,
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
    let [name, utilization, memory_total, memory_free, temperature] = fields.as_slice() else {
        return None;
    };
    let temperature_c = temperature
        .parse::<u32>()
        .ok()
        .filter(|value| *value <= 150);
    if name.is_empty() || name.chars().count() > 256 {
        return None;
    }
    // Unsupported or malformed memory is independent of load and temperature.
    // GB10 has no dedicated VRAM: /proc/meminfo owns its unified capacity.
    let memory = if shared_memory_pool(Some(name)) {
        None
    } else {
        match (optional_mib(memory_total), optional_mib(memory_free)) {
            (Some(Some(total)), Some(Some(free))) if free <= total => Some((total, free)),
            _ => None,
        }
    };
    let utilization = utilization
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite() && (0.0..=100.0).contains(value));
    Some(AcceleratorReading {
        name: (*name).to_owned(),
        temperature_c,
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
