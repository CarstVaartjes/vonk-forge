use std::{
    fs,
    io::Write,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::Path,
    time::Duration,
};

use serde::Serialize;
use thiserror::Error;
use vonk_agent_protocol::MemoryPool;

use crate::process::{ProcessError, ProcessOutput, ProcessRunner, Program};

/// Keep room on the state database filesystem for agent progress and recovery
/// records when model or image storage approaches exhaustion.
pub const STATE_DATABASE_DISK_RESERVE_BYTES: u64 = 64 * 1024 * 1024;

pub fn disk_reserve_degraded(available_bytes: u64) -> bool {
    available_bytes < STATE_DATABASE_DISK_RESERVE_BYTES
}

pub fn prepare_state_database_reserve(data_root: &Path) -> Result<bool, InventoryError> {
    let available = available_disk_bytes(data_root)?;
    maintain_state_database_reserve(data_root, available).map_err(InventoryError::Io)
}

fn maintain_state_database_reserve(
    data_root: &Path,
    available_bytes: u64,
) -> Result<bool, std::io::Error> {
    let reserve = data_root.join(".state-db-reserve");
    let existing = match fs::symlink_metadata(&reserve) {
        Ok(metadata)
            if metadata.file_type().is_file()
                && !metadata.file_type().is_symlink()
                && metadata.nlink() == 1
                && metadata.mode() & 0o777 == 0o600
                && metadata.uid() == fs::metadata(data_root)?.uid() =>
        {
            if metadata.len() == STATE_DATABASE_DISK_RESERVE_BYTES {
                true
            } else {
                // A process death during reserve allocation leaves only this
                // exact agent-owned file shape. Remove it and rebuild below.
                fs::remove_file(&reserve)?;
                false
            }
        }
        Ok(_) => return Err(std::io::Error::other("state database reserve is unsafe")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
        Err(error) => return Err(error),
    };
    if available_bytes < STATE_DATABASE_DISK_RESERVE_BYTES {
        if existing {
            fs::remove_file(&reserve)?;
        }
        return Ok(false);
    }
    if !existing && available_bytes >= STATE_DATABASE_DISK_RESERVE_BYTES.saturating_mul(2) {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&reserve)?;
        let block = [0_u8; 1024 * 1024];
        for _ in 0..(STATE_DATABASE_DISK_RESERVE_BYTES / block.len() as u64) {
            if let Err(error) = file.write_all(&block) {
                drop(file);
                let _ = fs::remove_file(&reserve);
                return Err(error);
            }
        }
        if let Err(error) = file.sync_all() {
            drop(file);
            let _ = fs::remove_file(&reserve);
            return Err(error);
        }
        drop(file);
        fs::File::open(data_root)?.sync_all()?;
        return Ok(true);
    }
    Ok(existing)
}

#[cfg(test)]
mod disk_reserve_tests {
    use super::{
        STATE_DATABASE_DISK_RESERVE_BYTES, disk_reserve_degraded, maintain_state_database_reserve,
    };
    use std::{fs, os::unix::fs::PermissionsExt};
    use tempfile::tempdir;

    #[test]
    fn low_disk_is_degraded_without_marking_the_agent_unavailable_at_the_reserve_boundary() {
        assert!(disk_reserve_degraded(0));
        assert!(disk_reserve_degraded(STATE_DATABASE_DISK_RESERVE_BYTES - 1));
        assert!(!disk_reserve_degraded(STATE_DATABASE_DISK_RESERVE_BYTES));
        assert!(!disk_reserve_degraded(
            STATE_DATABASE_DISK_RESERVE_BYTES + 1
        ));
    }

    #[test]
    fn reserve_is_created_when_space_allows_and_released_for_state_recovery() {
        let root = tempdir().unwrap();
        let reserve = root.path().join(".state-db-reserve");
        assert!(
            maintain_state_database_reserve(root.path(), STATE_DATABASE_DISK_RESERVE_BYTES * 2)
                .unwrap()
        );
        let metadata = fs::metadata(&reserve).unwrap();
        assert_eq!(metadata.len(), STATE_DATABASE_DISK_RESERVE_BYTES);
        assert_eq!(metadata.permissions().mode() & 0o777, 0o600);
        assert!(
            !maintain_state_database_reserve(root.path(), STATE_DATABASE_DISK_RESERVE_BYTES - 1)
                .unwrap()
        );
        assert!(!reserve.exists());
    }

    #[test]
    fn interrupted_reserve_allocation_is_repaired_on_the_next_inventory() {
        let root = tempdir().unwrap();
        let reserve = root.path().join(".state-db-reserve");
        fs::write(&reserve, b"partial allocation").unwrap();
        fs::set_permissions(&reserve, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(
            maintain_state_database_reserve(root.path(), STATE_DATABASE_DISK_RESERVE_BYTES * 2)
                .unwrap()
        );
        assert_eq!(
            fs::metadata(reserve).unwrap().len(),
            STATE_DATABASE_DISK_RESERVE_BYTES
        );
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Inventory {
    #[serde(rename = "host_memory_total_bytes")]
    pub memory_total_bytes: u64,
    #[serde(rename = "host_memory_free_bytes")]
    pub memory_available_bytes: u64,
    pub disk_total_bytes: u64,
    #[serde(rename = "disk_free_bytes")]
    pub disk_available_bytes: u64,
    #[serde(skip)]
    pub state_database_reserve_held: bool,
    pub gpu_count: u32,
    pub gpu_memory_total_bytes: u64,
    pub gpu_memory_free_bytes: u64,
    pub memory_pool: MemoryPool,
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
    #[error("host prerequisite is not ready: {0}")]
    PrerequisiteUnavailable(&'static str),
    #[error("inventory evidence is invalid")]
    Parse,
}

pub struct InventoryCollector<'a, R> {
    pub runner: &'a R,
    pub meminfo_path: &'a Path,
    pub store_path: &'a Path,
    pub egress_binary_path: &'a Path,
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
        let initial_disk_available_bytes = filesystem
            .f_bavail
            .checked_mul(fragment)
            .ok_or(InventoryError::Parse)?;
        let state_database_reserve_held =
            maintain_state_database_reserve(self.store_path, initial_disk_available_bytes)?;
        let filesystem = rustix::fs::statvfs(self.store_path)?;
        let disk_available_bytes = filesystem
            .f_bavail
            .checked_mul(filesystem.f_frsize)
            .ok_or(InventoryError::Parse)?;
        let gpu = startup_probe(
            self.runner,
            Program::NvidiaSmi,
            &[
                "--query-gpu=name,memory.total,memory.free,driver_version".to_owned(),
                "--format=csv,noheader,nounits".to_owned(),
            ],
            Duration::from_secs(10),
            "NVIDIA GPU discovery",
        )?;
        if gpu.stdout.iter().all(u8::is_ascii_whitespace) {
            return Err(InventoryError::PrerequisiteUnavailable(
                "NVIDIA GPU discovery",
            ));
        }
        let (
            gpu_count,
            gpu_memory_total_bytes,
            gpu_memory_free_bytes,
            nvidia_driver_version,
            memory_pool,
        ) = parse_gpus(&gpu.stdout, memory_total_bytes, memory_available_bytes)?;
        let podman = startup_probe(
            self.runner,
            Program::Podman,
            &[
                "version".to_owned(),
                "--format".to_owned(),
                "{{.Version}}".to_owned(),
            ],
            Duration::from_secs(10),
            "Podman",
        )?;
        if podman.stdout.iter().all(u8::is_ascii_whitespace) {
            return Err(InventoryError::PrerequisiteUnavailable("Podman"));
        }
        let docker = startup_probe(
            self.runner,
            Program::Docker,
            &["--version".to_owned()],
            Duration::from_secs(10),
            "Docker runtime",
        )?;
        let cdi = startup_probe(
            self.runner,
            Program::NvidiaCtk,
            &["cdi".to_owned(), "list".to_owned()],
            Duration::from_secs(10),
            "NVIDIA CDI device list",
        )?;
        let cdi_entries = std::str::from_utf8(&cdi.stdout).map_err(|_| InventoryError::Parse)?;
        if !cdi_entries
            .lines()
            .any(|line| line.trim() == "nvidia.com/gpu=all")
        {
            return Err(InventoryError::PrerequisiteUnavailable(
                "NVIDIA CDI device list",
            ));
        }
        let mut capabilities = vec![
            "recipe.operations.v1".to_owned(),
            "build.rootless-podman.v1".to_owned(),
            "runtime.spark-docker-nvidia.v1".to_owned(),
            "recipe.build.v1".to_owned(),
            "recipe.build.cleanup.v1".to_owned(),
            "recipe.image.import.v1".to_owned(),
            "recipe.job.run.v1".to_owned(),
            "runtime.vonk.v1".to_owned(),
        ];
        if egress_boundary_available(self.egress_binary_path) {
            capabilities.push("recipe.build.egress-proxy.v1".to_owned());
        }
        if let Some(speed) = self.fabric_bandwidth_mbps {
            capabilities.push(format!("fabric.connected.mbps.{speed}"));
        }
        Ok(Inventory {
            memory_total_bytes,
            memory_available_bytes,
            disk_total_bytes,
            disk_available_bytes,
            state_database_reserve_held,
            gpu_count,
            gpu_memory_total_bytes,
            gpu_memory_free_bytes,
            memory_pool,
            nvidia_driver_version,
            container_runtime_version: text(&docker.stdout)?,
            artifact_store_read_only: filesystem
                .f_flag
                .contains(rustix::fs::StatVfsMountFlags::RDONLY),
            capabilities,
            fabric_address: self.fabric_address,
            fabric_bandwidth_mbps: self.fabric_bandwidth_mbps,
        })
    }
}

fn startup_probe<R: ProcessRunner>(
    runner: &R,
    program: Program,
    arguments: &[String],
    timeout: Duration,
    dependency: &'static str,
) -> Result<ProcessOutput, InventoryError> {
    let output = runner
        .run(program, arguments, timeout)
        .map_err(|error| match error {
            ProcessError::Timeout => InventoryError::PrerequisiteUnavailable(dependency),
            ProcessError::Io(error)
                if !matches!(
                    error.kind(),
                    std::io::ErrorKind::NotFound
                        | std::io::ErrorKind::PermissionDenied
                        | std::io::ErrorKind::InvalidInput
                        | std::io::ErrorKind::InvalidData
                ) =>
            {
                InventoryError::PrerequisiteUnavailable(dependency)
            }
            error => InventoryError::Process(error),
        })?;
    if !output.success {
        return Err(InventoryError::PrerequisiteUnavailable(dependency));
    }
    Ok(output)
}

fn egress_boundary_available(path: &Path) -> bool {
    fs::symlink_metadata(path).ok().is_some_and(|metadata| {
        metadata.is_file()
            && !metadata.file_type().is_symlink()
            && metadata.uid() == 0
            && metadata.nlink() == 1
            && (64..=16 * 1024 * 1024).contains(&metadata.len())
            && metadata.permissions().mode() & 0o022 == 0
            && metadata.permissions().mode() & 0o111 != 0
    })
}

pub fn available_memory_bytes<R: ProcessRunner>(
    runner: &R,
    meminfo_path: &Path,
    memory_kind: &str,
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
    let (_, _, gpu_available, _, memory_pool) =
        parse_gpus(&gpu.stdout, host_total, host_available)?;
    match (memory_pool, memory_kind) {
        (MemoryPool::Shared, "host" | "accelerator" | "unified") => Ok(host_available),
        (MemoryPool::Separate, "host") => Ok(host_available),
        (MemoryPool::Separate, "accelerator") => Ok(gpu_available),
        (MemoryPool::Separate, "unified") => Ok(host_available.min(gpu_available)),
        _ => Err(InventoryError::Parse),
    }
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

pub(crate) fn shared_memory_pool(name: Option<&str>) -> bool {
    name.is_some_and(|value| {
        value
            .split_ascii_whitespace()
            .any(|part| part.eq_ignore_ascii_case("GB10"))
    })
}

fn parse_gpus(
    value: &[u8],
    host_total: u64,
    host_available: u64,
) -> Result<(u32, u64, u64, String, MemoryPool), InventoryError> {
    let value = std::str::from_utf8(value).map_err(|_| InventoryError::Parse)?;
    let lines = value
        .lines()
        .filter(|line| !line.trim().is_empty())
        .collect::<Vec<_>>();
    if lines.len() == 1 {
        let fields = lines[0].split(',').map(str::trim).collect::<Vec<_>>();
        if fields.len() == 4
            && shared_memory_pool(Some(fields[0]))
            && !fields[3].is_empty()
            && host_available <= host_total
        {
            // Numeric GPU-attributed allocation, when available, does not create
            // dedicated VRAM. Host readings own this single physical pool.
            if (fields[1], fields[2]) != ("[N/A]", "[N/A]") && mib(fields[2])? > mib(fields[1])? {
                return Err(InventoryError::Parse);
            }
            return Ok((
                1,
                host_total,
                host_available,
                fields[3].to_owned(),
                MemoryPool::Shared,
            ));
        }
    }
    let mut count = 0_u32;
    let mut total = 0_u64;
    let mut free = 0_u64;
    let mut driver = None;
    for line in lines {
        let fields = line.split(',').map(str::trim).collect::<Vec<_>>();
        if fields.len() != 4
            || fields[0].is_empty()
            || fields[3].is_empty()
            || shared_memory_pool(Some(fields[0]))
        {
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
    Ok((
        count,
        total,
        free,
        driver.ok_or(InventoryError::Parse)?,
        MemoryPool::Separate,
    ))
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
