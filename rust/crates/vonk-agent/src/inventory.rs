use std::{
    fs,
    io::Write,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::Path,
    time::Duration,
};

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

/// What one collection pass observed. The wire document is `InventoryRequest`,
/// built by the client; this value is never serialized itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Inventory {
    pub memory_total_bytes: u64,
    pub memory_available_bytes: u64,
    pub disk_total_bytes: u64,
    pub disk_available_bytes: u64,
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
    /// Filled by the caller from [`crate::network`]; `None` means unknown.
    pub network_interfaces: Option<Vec<vonk_agent_protocol::generated::NetworkInterface>>,
    pub nas_route_interface: Option<String>,
}

impl Inventory {
    /// The wire document that reports this observation to the Controller.
    pub fn to_request(
        &self,
        observed_at: chrono::DateTime<chrono::FixedOffset>,
    ) -> vonk_agent_protocol::generated::InventoryRequest {
        vonk_agent_protocol::generated::InventoryRequest {
            schema_version: 1,
            observed_at,
            disk_total_bytes: self.disk_total_bytes,
            disk_free_bytes: self.disk_available_bytes,
            host_memory_total_bytes: self.memory_total_bytes,
            host_memory_free_bytes: self.memory_available_bytes,
            gpu_memory_total_bytes: self.gpu_memory_total_bytes,
            gpu_memory_free_bytes: self.gpu_memory_free_bytes,
            gpu_count: self.gpu_count,
            memory_pool: self.memory_pool,
            artifact_store_read_only: self.artifact_store_read_only,
            capabilities: self.capabilities.clone(),
            fabric_address: self.fabric_address.map(|value| value.to_string()),
            fabric_bandwidth_mbps: self
                .fabric_bandwidth_mbps
                .and_then(|value| u32::try_from(value).ok()),
            nvidia_driver_version: self.nvidia_driver_version.clone(),
            container_runtime_version: self.container_runtime_version.clone(),
            network_interfaces: self.network_interfaces.clone(),
            nas_route_interface: self.nas_route_interface.clone(),
        }
    }
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

// The collector runs in an owned subprocess: a blocked filesystem syscall must
// not strand a Tokio blocking thread or permit another collector to accumulate.
// A killed process remains in this slot until the OS confirms it was reaped.
static INVENTORY_PROCESS: tokio::sync::Mutex<
    Option<(
        tokio::process::Child,
        tokio::time::Instant,
        rustix::process::Pid,
    )>,
> = tokio::sync::Mutex::const_new(None);
const INVENTORY_PROCESS_BUDGET: Duration = Duration::from_secs(60);
const INVENTORY_OUTPUT_BYTES: u64 = 64 * 1024;

pub async fn collect_process(
    executable: &Path,
    arguments: &[String],
) -> Result<vonk_agent_protocol::generated::InventoryRequest, InventoryError> {
    collect_process_with_budget(executable, arguments, INVENTORY_PROCESS_BUDGET).await
}

async fn collect_process_with_budget(
    executable: &Path,
    arguments: &[String],
    budget: Duration,
) -> Result<vonk_agent_protocol::generated::InventoryRequest, InventoryError> {
    use tokio::io::AsyncReadExt;
    let unavailable = || InventoryError::PrerequisiteUnavailable("inventory producer observation");
    let mut slot = INVENTORY_PROCESS.try_lock().map_err(|_| unavailable())?;
    if let Some((child, deadline, group)) = slot.as_mut() {
        let exited = child.try_wait()?.is_some();
        if !exited || !process_group_gone(*group) {
            if exited || tokio::time::Instant::now() >= *deadline {
                let _ = rustix::process::kill_process_group(*group, rustix::process::Signal::KILL);
            }
            return Err(unavailable());
        }
        *slot = None;
    }
    let child = tokio::process::Command::new(executable)
        .args(arguments)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .process_group(0)
        .spawn()?;
    let group = child
        .id()
        .and_then(|pid| i32::try_from(pid).ok())
        .and_then(rustix::process::Pid::from_raw)
        .ok_or_else(unavailable)?;
    let deadline = tokio::time::Instant::now() + budget;
    *slot = Some((child, deadline, group));
    let Some((child, _, group)) = slot.as_mut() else {
        return Err(unavailable());
    };
    let observe = async {
        let mut bytes = Vec::new();
        child
            .stdout
            .as_mut()
            .ok_or_else(unavailable)?
            .take(INVENTORY_OUTPUT_BYTES + 1)
            .read_to_end(&mut bytes)
            .await?;
        if bytes.len() as u64 > INVENTORY_OUTPUT_BYTES {
            return Err(InventoryError::Parse);
        }
        let exited = tokio::time::timeout_at(deadline, child.wait())
            .await
            .map_err(|_| unavailable())??;
        if !exited.success() {
            return Err(unavailable());
        }
        vonk_agent_protocol::parse_strict(&bytes).map_err(|_| InventoryError::Parse)
    };
    let result = tokio::time::timeout_at(deadline, observe).await;
    // Cancellation/timeout never loses ownership. A subsequent bounded pass
    // reaps the old process before a fresh observer can run.
    match result {
        Ok(result) => {
            if child.try_wait()?.is_some() && process_group_gone(*group) {
                *slot = None;
            } else {
                let _ = rustix::process::kill_process_group(*group, rustix::process::Signal::KILL);
            }
            result
        }
        Err(_) => {
            let _ = rustix::process::kill_process_group(*group, rustix::process::Signal::KILL);
            Err(unavailable())
        }
    }
}

fn process_group_gone(group: rustix::process::Pid) -> bool {
    matches!(
        rustix::process::test_kill_process_group(group),
        Err(rustix::io::Errno::SRCH)
    )
}

/// Service shutdown signals the owned observer and allows a bounded reap.
/// An uninterruptible child stays owned by the service cgroup, never mistaken
/// for successful cleanup or permission to launch a second observer.
pub async fn stop_process() {
    if let Ok(mut slot) = INVENTORY_PROCESS.try_lock()
        && let Some((child, _, group)) = slot.as_mut()
    {
        let _ = rustix::process::kill_process_group(*group, rustix::process::Signal::KILL);
        if tokio::time::timeout(Duration::from_secs(1), child.wait())
            .await
            .is_ok_and(|result| result.is_ok())
            && process_group_gone(*group)
        {
            *slot = None;
        }
    }
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
            // Pulls pinned runtime images from the Controller's layered
            // image store through a loopback forwarder.
            "recipe.image.pull.v1".to_owned(),
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
            network_interfaces: None,
            nas_route_interface: None,
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
            eprintln!("vonk-agent: skipping malformed GPU inventory row");
            continue;
        }
        let (gpu_total, gpu_free) = match mib(fields[1]).and_then(|gpu_total| {
            mib(fields[2]).and_then(|gpu_free| {
                (gpu_free <= gpu_total)
                    .then_some((gpu_total, gpu_free))
                    .ok_or(InventoryError::Parse)
            })
        }) {
            Ok(memory) => memory,
            Err(_) => {
                eprintln!("vonk-agent: skipping GPU inventory row with invalid memory evidence");
                continue;
            }
        };
        if driver
            .as_ref()
            .is_some_and(|current: &String| current != fields[3])
        {
            eprintln!("vonk-agent: skipping GPU inventory row with inconsistent driver evidence");
            continue;
        }
        count = count.checked_add(1).ok_or(InventoryError::Parse)?;
        total = total.checked_add(gpu_total).ok_or(InventoryError::Parse)?;
        free = free.checked_add(gpu_free).ok_or(InventoryError::Parse)?;
        driver.get_or_insert_with(|| fields[3].to_owned());
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

#[cfg(all(test, target_os = "linux"))]
mod process_tests {
    use super::*;

    #[tokio::test]
    async fn blocked_os_collector_is_killed_reaped_and_a_fresh_wire_observation_succeeds() {
        let root = tempfile::tempdir().unwrap();
        let fifo = root.path().join("blocked");
        rustix::fs::mknodat(
            rustix::fs::CWD,
            &fifo,
            rustix::fs::FileType::Fifo,
            rustix::fs::Mode::from_raw_mode(0o600),
            0,
        )
        .unwrap();
        let pid_file = root.path().join("collector.pid");
        let blocked = vec![
            "-c".to_owned(),
            "printf '%s' \"$$\" > \"$1\"; exec /bin/cat \"$2\"".to_owned(),
            "collector".to_owned(),
            pid_file.to_string_lossy().into_owned(),
            fifo.to_string_lossy().into_owned(),
        ];
        assert!(
            collect_process_with_budget(Path::new("/bin/sh"), &blocked, Duration::from_secs(1))
                .await
                .is_err()
        );
        let pid = fs::read_to_string(&pid_file)
            .unwrap()
            .parse::<i32>()
            .unwrap();
        let request = vonk_agent_protocol::generated::InventoryRequest {
            schema_version: 1,
            observed_at: chrono::Utc::now().into(),
            disk_total_bytes: 100,
            disk_free_bytes: 50,
            host_memory_total_bytes: 100,
            host_memory_free_bytes: 50,
            gpu_memory_total_bytes: 100,
            gpu_memory_free_bytes: 50,
            gpu_count: 1,
            memory_pool: MemoryPool::Shared,
            artifact_store_read_only: false,
            capabilities: vec![],
            fabric_address: None,
            fabric_bandwidth_mbps: None,
            network_interfaces: None,
            nas_route_interface: None,
            nvidia_driver_version: "590".into(),
            container_runtime_version: "29".into(),
        };
        let wire = root.path().join("inventory.json");
        fs::write(
            &wire,
            vonk_agent_protocol::canonical_json(&request).unwrap(),
        )
        .unwrap();
        let arguments = vec![wire.to_string_lossy().into_owned()];
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        let fresh = loop {
            if let Ok(request) = collect_process_with_budget(
                Path::new("/bin/cat"),
                &arguments,
                Duration::from_secs(1),
            )
            .await
            {
                break request;
            }
            assert!(tokio::time::Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(10)).await;
        };
        assert_eq!(fresh, request);
        // The original blocking filesystem observer released its OS process,
        // rather than merely dropping a future and substituting a closure.
        assert!(
            rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid).unwrap())
                .is_err()
        );
    }
}
