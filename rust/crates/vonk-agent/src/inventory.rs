use std::{fs, path::Path, time::Duration};

use serde::Serialize;
use thiserror::Error;

use crate::process::{ProcessError, ProcessRunner, Program};

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Inventory {
    pub memory_total_bytes: u64,
    pub memory_available_bytes: u64,
    pub disk_total_bytes: u64,
    pub disk_available_bytes: u64,
    pub gpu_count: u32,
    pub gpu_memory_total_bytes: u64,
    pub gpu_memory_free_bytes: u64,
    pub nvidia_driver_version: String,
    pub container_runtime_version: String,
    pub artifact_store_read_only: bool,
    pub capabilities: Vec<String>,
    pub fabric_address: Option<std::net::IpAddr>,
    pub fabric_bandwidth_mbps: Option<u64>,
}

#[derive(Debug, Error)]
pub enum InventoryError {
    #[error("inventory source could not be read")]
    Io(#[from] std::io::Error),
    #[error("inventory command failed")]
    Process(#[from] ProcessError),
    #[error("inventory system query failed")]
    System(#[from] rustix::io::Errno),
    #[error("inventory evidence is invalid")]
    Parse,
}

pub struct InventoryCollector<'a, R> {
    pub runner: &'a R,
    pub meminfo_path: &'a Path,
    pub store_path: &'a Path,
    pub fabric_address: Option<std::net::IpAddr>,
    pub fabric_bandwidth_mbps: Option<u64>,
}

impl<R: ProcessRunner> InventoryCollector<'_, R> {
    pub fn collect(&self) -> Result<Inventory, InventoryError> {
        let (memory_total_bytes, memory_available_bytes) =
            parse_meminfo(&fs::read_to_string(self.meminfo_path)?)?;
        let filesystem = rustix::fs::statvfs(self.store_path)?;
        let fragment = filesystem.f_frsize;
        let disk_total_bytes = filesystem
            .f_blocks
            .checked_mul(fragment)
            .ok_or(InventoryError::Parse)?;
        let disk_available_bytes = filesystem
            .f_bavail
            .checked_mul(fragment)
            .ok_or(InventoryError::Parse)?;
        let gpu = self.runner.run(
            Program::NvidiaSmi,
            &[
                "--query-gpu=name,memory.total,memory.free,driver_version".to_owned(),
                "--format=csv,noheader,nounits".to_owned(),
            ],
            Duration::from_secs(10),
        )?;
        if !gpu.success {
            return Err(InventoryError::Parse);
        }
        let (gpu_count, gpu_memory_total_bytes, gpu_memory_free_bytes, nvidia_driver_version) =
            parse_gpus(&gpu.stdout, memory_total_bytes, memory_available_bytes)?;
        let podman = self.runner.run(
            Program::Podman,
            &[
                "version".to_owned(),
                "--format".to_owned(),
                "{{.Version}}".to_owned(),
            ],
            Duration::from_secs(10),
        )?;
        if !podman.success {
            return Err(InventoryError::Parse);
        }
        let mut capabilities = vec![
            "recipe.operations.v1".to_owned(),
            "runtime.rootless-podman.v1".to_owned(),
            "recipe.build.v1".to_owned(),
            "recipe.image.import.v1".to_owned(),
            "runtime.vonk.v1".to_owned(),
        ];
        if let Some(speed) = self.fabric_bandwidth_mbps {
            capabilities.push(format!("fabric.tcp.mbps.{speed}"));
        }
        Ok(Inventory {
            memory_total_bytes,
            memory_available_bytes,
            disk_total_bytes,
            disk_available_bytes,
            gpu_count,
            gpu_memory_total_bytes,
            gpu_memory_free_bytes,
            nvidia_driver_version,
            container_runtime_version: text(&podman.stdout)?,
            artifact_store_read_only: filesystem
                .f_flag
                .contains(rustix::fs::StatVfsMountFlags::RDONLY),
            capabilities,
            fabric_address: self.fabric_address,
            fabric_bandwidth_mbps: self.fabric_bandwidth_mbps,
        })
    }
}

pub fn available_memory_bytes<R: ProcessRunner>(
    runner: &R,
    meminfo_path: &Path,
) -> Result<u64, InventoryError> {
    let (host_total, host_available) = parse_meminfo(&fs::read_to_string(meminfo_path)?)?;
    let gpu = runner.run(
        Program::NvidiaSmi,
        &[
            "--query-gpu=name,memory.total,memory.free,driver_version".to_owned(),
            "--format=csv,noheader,nounits".to_owned(),
        ],
        Duration::from_secs(10),
    )?;
    if !gpu.success {
        return Err(InventoryError::Parse);
    }
    let (_, _, gpu_available, _) = parse_gpus(&gpu.stdout, host_total, host_available)?;
    Ok(host_available.min(gpu_available))
}

pub fn available_disk_bytes(path: &Path) -> Result<u64, InventoryError> {
    let filesystem = rustix::fs::statvfs(path)?;
    filesystem
        .f_bavail
        .checked_mul(filesystem.f_frsize)
        .ok_or(InventoryError::Parse)
}

fn parse_meminfo(value: &str) -> Result<(u64, u64), InventoryError> {
    let mut total = None;
    let mut available = None;
    for line in value.lines() {
        let mut fields = line.split_ascii_whitespace();
        match (fields.next(), fields.next(), fields.next(), fields.next()) {
            (Some("MemTotal:"), Some(amount), Some("kB"), None) => total = Some(kib(amount)?),
            (Some("MemAvailable:"), Some(amount), Some("kB"), None) => {
                available = Some(kib(amount)?);
            }
            _ => {}
        }
    }
    match (total, available) {
        (Some(total), Some(available)) if available <= total => Ok((total, available)),
        _ => Err(InventoryError::Parse),
    }
}

fn parse_gpus(
    value: &[u8],
    host_total: u64,
    host_available: u64,
) -> Result<(u32, u64, u64, String), InventoryError> {
    let value = std::str::from_utf8(value).map_err(|_| InventoryError::Parse)?;
    let lines = value
        .lines()
        .filter(|line| !line.trim().is_empty())
        .collect::<Vec<_>>();
    if lines.len() == 1 {
        let fields = lines[0].split(',').map(str::trim).collect::<Vec<_>>();
        if fields.len() == 4
            && fields[0] == "NVIDIA GB10"
            && fields[1] == "[N/A]"
            && fields[2] == "[N/A]"
            && !fields[3].is_empty()
            && host_available <= host_total
        {
            return Ok((1, host_total, host_available, fields[3].to_owned()));
        }
    }
    let mut count = 0_u32;
    let mut total = 0_u64;
    let mut free = 0_u64;
    let mut driver = None;
    for line in lines {
        let fields = line.split(',').map(str::trim).collect::<Vec<_>>();
        if fields.len() != 4 || fields[0].is_empty() || fields[3].is_empty() {
            return Err(InventoryError::Parse);
        }
        count = count.checked_add(1).ok_or(InventoryError::Parse)?;
        total = total
            .checked_add(mib(fields[1])?)
            .ok_or(InventoryError::Parse)?;
        free = free
            .checked_add(mib(fields[2])?)
            .ok_or(InventoryError::Parse)?;
        if driver.get_or_insert_with(|| fields[3].to_owned()) != fields[3] {
            return Err(InventoryError::Parse);
        }
    }
    if count == 0 || free > total {
        return Err(InventoryError::Parse);
    }
    Ok((count, total, free, driver.ok_or(InventoryError::Parse)?))
}

fn kib(value: &str) -> Result<u64, InventoryError> {
    value
        .parse::<u64>()
        .map_err(|_| InventoryError::Parse)?
        .checked_mul(1024)
        .ok_or(InventoryError::Parse)
}

fn mib(value: &str) -> Result<u64, InventoryError> {
    value
        .parse::<u64>()
        .map_err(|_| InventoryError::Parse)?
        .checked_mul(1024 * 1024)
        .ok_or(InventoryError::Parse)
}

fn text(value: &[u8]) -> Result<String, InventoryError> {
    let value = std::str::from_utf8(value)
        .map_err(|_| InventoryError::Parse)?
        .trim();
    if value.is_empty() || value.len() > 256 || !value.is_ascii() {
        return Err(InventoryError::Parse);
    }
    Ok(value.to_owned())
}
