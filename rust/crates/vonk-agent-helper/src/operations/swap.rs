//! The managed swap file: one file of the helper's own beside the operating
//! system's swap, sized so all active swap reaches the desired total.
//!
//! A change activates the new file before it releases the old one, so the host
//! is never left with less swap mid-change. An old file is released only while
//! the pages it holds fit in memory with the host memory guard's stall bar to
//! spare; otherwise it stays active (more swap than desired) and the next pass
//! tries again. Swap the helper does not own (`/swap.img`, fstab) is never
//! touched. Retries ride inventory and the hourly host-settings pass, bounded
//! by a persisted deadline, like the GPU clock.
use super::*;
use vonk_agent_protocol::generated::{SwapApplyState, SwapCheckpoint, SwapIntent, SwapWaitReason};
use vonk_agent_protocol::host_memory_guard_policy::STALL_AVAILABLE_BYTES;
use vonk_agent_protocol::swap::{
    GIB, OBSERVATION_FILE, RECEIPT_BYTES, default_intent, desired_bytes, policy, tolerance_bytes,
    valid_intent,
};

pub(super) const FALLOCATE: &str = "/usr/bin/fallocate";
pub(super) const MKSWAP: &str = "/usr/sbin/mkswap";
pub(super) const SWAPON: &str = "/usr/sbin/swapon";
pub(super) const SWAPOFF: &str = "/usr/sbin/swapoff";
/// Root-private directory of the managed swap file, its checkpoint and lock.
const DIRECTORY: &str = "swap";
const CHECKPOINT_FILE: &str = "swap.json";
const PROC_SWAPS: &str = "/proc/swaps";
const PROC_MEMINFO: &str = "/proc/meminfo";
const BOOT_ID: &str = "/proc/sys/kernel/random/boot_id";
const COMMAND_TIMEOUT: Duration = Duration::from_secs(30);
// An interrupted swapoff leaves the file active (the kernel re-enables it);
// the pages it already moved stay in memory, so each pass makes progress.
// The agent waits SWAP_PASS_SECONDS for the whole pass.
const RELEASE_TIMEOUT: Duration = Duration::from_secs(20);
const RETRY_SECONDS: u64 = 60;
const ATTEMPT_SECONDS: u64 = 10 * 60;
const RECORD_BYTES: u64 = RECEIPT_BYTES;
/// mkswap keeps the first page for its header; /proc/swaps reports the rest.
const HEADER_BYTES: u64 = 4096;
const FILE_PREFIX: &str = "swap-";
const FILE_SUFFIX: &str = ".img";
const TEMPORARY_PREFIX: &str = ".swap-";
const TEMPORARY_SUFFIX: &str = ".tmp";

/// One `/proc/swaps` row in bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct ActiveSwap {
    pub path: PathBuf,
    pub size_bytes: u64,
    pub used_bytes: u64,
}

pub(super) fn parse_swaps(text: &str) -> Option<Vec<ActiveSwap>> {
    let mut lines = text.lines();
    lines.next()?.starts_with("Filename").then_some(())?;
    lines
        .filter(|line| !line.trim().is_empty())
        .map(|line| {
            let fields: Vec<_> = line.split_whitespace().collect();
            let [path, _kind, size, used, _priority] = fields.as_slice() else {
                return None;
            };
            Some(ActiveSwap {
                path: PathBuf::from(path),
                size_bytes: size.parse::<u64>().ok()?.checked_mul(1024)?,
                used_bytes: used.parse::<u64>().ok()?.checked_mul(1024)?,
            })
        })
        .collect()
}

/// The managed file a `/proc/swaps` row names, in this helper's own path.
fn managed_row(root: &Path, listed: &Path) -> Option<PathBuf> {
    let name = listed.file_name()?.to_str()?;
    (name.starts_with(FILE_PREFIX) && managed_name(name))
        .then(|| root.join(name))
        .filter(|path| path.is_file())
}

fn managed_name(name: &str) -> bool {
    let uuid = |value: &str| uuid::Uuid::try_parse(value).is_ok_and(|id| id.to_string() == value);
    name.strip_prefix(FILE_PREFIX)
        .and_then(|rest| rest.strip_suffix(FILE_SUFFIX))
        .is_some_and(uuid)
        || name
            .strip_prefix(TEMPORARY_PREFIX)
            .and_then(|rest| rest.strip_suffix(TEMPORARY_SUFFIX))
            .is_some_and(uuid)
}

/// A managed swap file path: absolute, in a `swap` directory, with a managed
/// name. The process boundary accepts no other path.
fn managed_path(path: &str) -> bool {
    let path = Path::new(path);
    path.is_absolute()
        && path
            .components()
            .all(|part| matches!(part, Component::RootDir | Component::Normal(_)))
        && path
            .parent()
            .and_then(Path::file_name)
            .is_some_and(|name| name == DIRECTORY)
        && path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(managed_name)
}

// One command definition serves the producer and the privileged process boundary.
fn allocate_arguments(path: &Path, bytes: u64) -> Vec<String> {
    vec![
        "--length".to_owned(),
        bytes.to_string(),
        path.to_string_lossy().into_owned(),
    ]
}

pub(super) fn allowed_arguments(executable: &str, arguments: &[String]) -> bool {
    match (executable, arguments) {
        (FALLOCATE, [flag, bytes, path]) => {
            flag == "--length"
                && bytes.parse::<u64>().is_ok_and(|n| {
                    n > HEADER_BYTES && n <= u64::from(policy().maximum_gib) * GIB + HEADER_BYTES
                })
                && managed_path(path)
        }
        (MKSWAP | SWAPON | SWAPOFF, [path]) => managed_path(path),
        _ => false,
    }
}

struct Plan {
    keep: Option<ActiveSwap>,
    release: Vec<ActiveSwap>,
    usable_bytes: u64,
}

type Wait = (SwapWaitReason, String);

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn reconcile_swap(
        &self,
        desired: Option<&SwapIntent>,
        hourly: bool,
    ) -> Result<(), OperationError> {
        self.reconcile_swap_at(
            desired,
            hourly,
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs(),
        )
    }

    pub(super) fn reconcile_swap_at(
        &self,
        desired: Option<&SwapIntent>,
        hourly: bool,
        now: u64,
    ) -> Result<(), OperationError> {
        if desired.is_some_and(|intent| !valid_intent(intent)) {
            return Err(OperationError::InvalidOperation);
        }
        let root = self.roots.data.join(DIRECTORY);
        ensure_runtime_directory(&root)?;
        require_exact_directory(&root, Some(rustix::process::geteuid().as_raw()), 0o700)?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(root.join("lock"))?;
        // Another pass is converging and writes the receipt; the next
        // inventory brings this intent again.
        let Ok(_guard) = HeldLock::try_exclusive(lock) else {
            eprintln!("vonk-agent-helper: swap: a pass is already running");
            return Ok(());
        };
        let path = root.join(CHECKPOINT_FILE);
        let current = read_checkpoint(&path)?;
        // An older generation never overrides. The same generation with another
        // size is a changed policy default (a Controller release): the signed,
        // short-lived grant steers, as any newer one does.
        if let (Some(old), Some(new)) = (&current, desired)
            && new.generation < old.desired.generation
        {
            return Ok(());
        }
        let wanted = desired
            .cloned()
            .or_else(|| current.as_ref().map(|c| c.desired.clone()))
            .unwrap_or_else(default_intent);
        let changed = current.as_ref().is_none_or(|c| c.desired != wanted);
        let mut record = match current {
            Some(record) if !changed => record,
            previous => SwapCheckpoint {
                desired: wanted,
                state: SwapApplyState::Waiting,
                reason: None,
                detail: None,
                next_attempt_at: Some(now),
                deadline: now + ATTEMPT_SECONDS,
                applied_boot_id: previous.and_then(|c| c.applied_boot_id),
            },
        };
        let boot = self
            .runner
            .read_host_text(Path::new(BOOT_ID))
            .ok()
            .map(|s| s.trim().to_owned());
        let reboot = record.applied_boot_id != boot;
        if !changed && !hourly && !reboot && record.state == SwapApplyState::Applied {
            return Ok(());
        }
        // After expiry a fresh bounded hourly reconciliation remains possible;
        // ordinary polls cannot extend a failing change's deadline forever.
        if hourly && record.state != SwapApplyState::Waiting
            || reboot && record.state == SwapApplyState::Applied
        {
            record.deadline = now + ATTEMPT_SECONDS;
            record.next_attempt_at = Some(now);
        }
        if !changed && record.next_attempt_at.is_some_and(|next| now < next) {
            return Ok(());
        }
        if now >= record.deadline {
            if record.state == SwapApplyState::Expired {
                return Ok(());
            }
            record.state = SwapApplyState::Expired;
            record.next_attempt_at = None;
            record.detail = Some(format!(
                "Swap retry deadline expired; the hourly host-settings reconciliation will retry. Workloads continue. Last cause: {}",
                record.detail.as_deref().unwrap_or("not reported")
            ));
            return self.write_swap(&root, &path, &record);
        }
        record.state = SwapApplyState::Waiting;
        record.next_attempt_at = Some(now + RETRY_SECONDS);
        self.write_swap(&root, &path, &record)?; // intent and retry identity before effects
        match self.converge_swap(&root, &record.desired) {
            Ok(summary) if boot.is_some() => {
                record.state = SwapApplyState::Applied;
                record.reason = None;
                record.detail = Some(summary);
                record.next_attempt_at = None;
                record.applied_boot_id = boot;
            }
            Ok(_) => {
                record.reason = Some(SwapWaitReason::HostUnreadable);
                record.detail =
                    Some("Swap is active but the boot identity is unreadable; retry".to_owned());
            }
            Err((reason, detail)) => {
                record.reason = Some(reason);
                record.detail = Some(format!(
                    "{detail}; retry after {} (deadline {}). Workloads continue.",
                    now + RETRY_SECONDS,
                    record.deadline
                ));
            }
        }
        eprintln!(
            "vonk-agent-helper: swap {} GiB: {:?} {:?}: {:?}",
            record.desired.size_gib, record.state, record.reason, record.detail
        );
        self.write_swap(&root, &path, &record)
    }

    fn plan_swap(&self, root: &Path, desired: &SwapIntent) -> Result<Plan, Wait> {
        let failed = |what: &str, cause: String| {
            (SwapWaitReason::HostUnreadable, format!("{what}: {cause}"))
        };
        let text = self
            .runner
            .read_host_text(Path::new(PROC_SWAPS))
            .map_err(|error| failed("active swap unreadable", error.to_string()))?;
        let active = parse_swaps(&text).ok_or_else(|| {
            failed(
                "active swap unreadable",
                "unexpected /proc/swaps".to_owned(),
            )
        })?;
        // A row names a managed file by its name, not its path: the kernel
        // prints the path through the mount the helper activated it from,
        // and a restarted helper's private mount namespace is a new one (a
        // detached mount prints only `/swap/swap-<uuid>.img`).
        let (managed, other): (Vec<_>, Vec<_>) = active
            .into_iter()
            .map(|swap| match managed_row(root, &swap.path) {
                Some(path) => (true, ActiveSwap { path, ..swap }),
                None => (false, swap),
            })
            .partition(|(managed, _)| *managed);
        let managed: Vec<_> = managed.into_iter().map(|(_, swap)| swap).collect();
        let other_bytes: u64 = other.iter().map(|(_, swap)| swap.size_bytes).sum();
        let usable_bytes = match desired_bytes(desired).saturating_sub(other_bytes) {
            shortfall if shortfall < tolerance_bytes() => 0,
            shortfall => shortfall,
        };
        let mut release = managed;
        let keep = release
            .iter()
            .position(|swap| usable_bytes > 0 && swap.size_bytes == usable_bytes)
            .map(|index| release.remove(index));
        Ok(Plan {
            keep,
            release,
            usable_bytes,
        })
    }

    /// One bounded pass toward the desired total; what it cannot do now waits.
    fn converge_swap(&self, root: &Path, desired: &SwapIntent) -> Result<String, Wait> {
        let plan = self.plan_swap(root, desired)?;
        let active: Vec<_> = plan
            .keep
            .iter()
            .chain(&plan.release)
            .map(|swap| swap.path.clone())
            .collect();
        self.remove_inactive_swap_files(root, &active);
        let mut kept = plan.keep.clone();
        if plan.usable_bytes > 0 && kept.is_none() {
            kept = Some(self.activate_swap_file(root, plan.usable_bytes)?);
        }
        for old in &plan.release {
            self.release_swap_file(old)?;
        }
        Ok(format!(
            "Managed swap {} bytes active; desired total {} GiB",
            kept.map_or(0, |swap| swap.size_bytes),
            desired.size_gib
        ))
    }

    /// Leftovers of a crash or reboot: inactive managed files are never reused.
    fn remove_inactive_swap_files(&self, root: &Path, active: &[PathBuf]) {
        let Ok(entries) = fs::read_dir(root) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let managed = path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(managed_name);
            if managed
                && !active.contains(&path)
                && let Err(error) = fs::remove_file(&path)
            {
                eprintln!(
                    "vonk-agent-helper: swap: leftover {} not removed: {error}",
                    path.display()
                );
            }
        }
    }

    fn activate_swap_file(&self, root: &Path, usable_bytes: u64) -> Result<ActiveSwap, Wait> {
        let file_bytes = usable_bytes + HEADER_BYTES;
        let space = self.runner.filesystem_space(root).map_err(|error| {
            (
                SwapWaitReason::HostUnreadable,
                format!("filesystem space unreadable: {error}"),
            )
        })?;
        let reserve = space.total_bytes / 100 * u64::from(policy().free_disk_percent_after);
        if space.available_bytes < file_bytes.saturating_add(reserve) {
            return Err((
                SwapWaitReason::InsufficientDisk,
                format!(
                    "a {file_bytes}-byte swap file needs {} bytes available ({}% of the filesystem stays free); {} available",
                    file_bytes.saturating_add(reserve),
                    policy().free_disk_percent_after,
                    space.available_bytes
                ),
            ));
        }
        let id = uuid::Uuid::new_v4();
        let temporary = root.join(format!("{TEMPORARY_PREFIX}{id}{TEMPORARY_SUFFIX}"));
        let target = root.join(format!("{FILE_PREFIX}{id}{FILE_SUFFIX}"));
        let created = (|| -> Result<(), Wait> {
            let io = |what: &'static str| {
                move |error: std::io::Error| {
                    (SwapWaitReason::FileFailed, format!("{what}: {error}"))
                }
            };
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
                .open(&temporary)
                .map_err(io("swap file not created"))?;
            self.swap_command(
                FALLOCATE,
                &allocate_arguments(&temporary, file_bytes),
                COMMAND_TIMEOUT,
            )?;
            self.swap_command(
                MKSWAP,
                &[temporary.to_string_lossy().into_owned()],
                COMMAND_TIMEOUT,
            )?;
            fs::rename(&temporary, &target).map_err(io("swap file not published"))?;
            sync_directory(root).map_err(|error| {
                (
                    SwapWaitReason::FileFailed,
                    format!("swap directory not synced: {error}"),
                )
            })?;
            self.swap_command(
                SWAPON,
                &[target.to_string_lossy().into_owned()],
                COMMAND_TIMEOUT,
            )
        })();
        if let Err(wait) = created {
            let _ = fs::remove_file(&temporary);
            let _ = fs::remove_file(&target);
            return Err(wait);
        }
        Ok(ActiveSwap {
            path: target,
            size_bytes: usable_bytes,
            used_bytes: 0,
        })
    }

    fn release_swap_file(&self, old: &ActiveSwap) -> Result<(), Wait> {
        let available = self
            .runner
            .read_host_text(Path::new(PROC_MEMINFO))
            .ok()
            .and_then(|text| crate::host_memory_guard::meminfo_bytes(&text, "MemAvailable:"));
        let Some(available) = available else {
            return Err((
                SwapWaitReason::ReleaseDeferred,
                format!(
                    "old swap file {} kept: MemAvailable unreadable",
                    old.path.display()
                ),
            ));
        };
        if old.used_bytes.saturating_add(STALL_AVAILABLE_BYTES) > available {
            return Err((
                SwapWaitReason::ReleaseDeferred,
                format!(
                    "old swap file {} kept: it holds {} bytes in use and only {available} bytes are available (the memory guard's {STALL_AVAILABLE_BYTES}-byte bar must stay free)",
                    old.path.display(),
                    old.used_bytes
                ),
            ));
        }
        self.swap_command(
            SWAPOFF,
            &[old.path.to_string_lossy().into_owned()],
            RELEASE_TIMEOUT,
        )
        .map_err(|(_, detail)| (SwapWaitReason::ReleaseDeferred, detail))?;
        fs::remove_file(&old.path).map_err(|error| {
            (
                SwapWaitReason::FileFailed,
                format!(
                    "released swap file {} not removed: {error}",
                    old.path.display()
                ),
            )
        })
    }

    fn swap_command(
        &self,
        executable: &str,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<(), Wait> {
        match self
            .runner
            .run_with_timeout(Path::new(executable), arguments, timeout)
        {
            Ok(output) if output.success => Ok(()),
            Ok(output) => Err((
                SwapWaitReason::CommandFailed,
                format!("{executable} failed: {}", CommandFailure::exit(&output)),
            )),
            Err(cause) => Err((
                SwapWaitReason::CommandFailed,
                format!("{executable} failed: {cause}"),
            )),
        }
    }

    fn write_swap(
        &self,
        root: &Path,
        path: &Path,
        record: &SwapCheckpoint,
    ) -> Result<(), OperationError> {
        let bytes = canonical_json(record).map_err(|_| OperationError::InvalidOperation)?;
        let temporary = root.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        sync_directory(root)?;
        // The same record, public: the agent reports it; it carries no authority.
        let observation = self.roots.data.join(OBSERVATION_FILE);
        let temporary = self
            .roots
            .data
            .join(format!(".swap-observation-{}.tmp", uuid::Uuid::new_v4()));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o644)
            .open(&temporary)?;
        file.write_all(&bytes)?;
        file.set_permissions(fs::Permissions::from_mode(0o644))?;
        file.sync_all()?;
        fs::rename(temporary, observation)?;
        sync_directory(&self.roots.data)
    }
}

fn read_checkpoint(path: &Path) -> Result<Option<SwapCheckpoint>, OperationError> {
    match OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32)
        .open(path)
    {
        Ok(file) => {
            let metadata = file.metadata()?;
            if !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.mode() & 0o777 != 0o600
                || metadata.uid() != rustix::process::geteuid().as_raw()
                || metadata.len() > RECORD_BYTES
            {
                return Err(OperationError::UnsafePath);
            }
            let mut bytes = Vec::new();
            file.take(RECORD_BYTES + 1).read_to_end(&mut bytes)?;
            // A record this helper cannot read is replaced by the current
            // intent, never a reason to stop managing swap.
            Ok(parse_strict::<SwapCheckpoint>(&bytes).ok())
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

#[cfg(test)]
mod tests;
