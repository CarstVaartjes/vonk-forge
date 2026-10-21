//! Durable active intent, fenced by selected-profile generation. Commands are
//! bounded and retries are scheduled by normal inventory traffic and the hourly
//! host-settings pass, never by sleeping in a request or holding a workload slot.
use super::*;
use vonk_agent_protocol::generated::{
    GpuClockApplyState, GpuClockCheckpoint, GpuClockIntent, GpuClockRange,
};
use vonk_agent_protocol::gpu_clock::{CHECKPOINT_FILE, CSV_FORMAT, default_range, valid_range};

pub(super) const NVIDIA_SMI: &str = "/usr/bin/nvidia-smi";
pub(super) const OBSERVATION_QUERY: &str =
    "--query-gpu=clocks.current.graphics,clocks.current.sm,clocks_event_reasons.active";
const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);
const RETRY_SECONDS: u64 = 60;
const ATTEMPT_SECONDS: u64 = 10 * 60;
const RECORD_BYTES: u64 = 16 * 1024;

// One command definition serves the producer and the privileged process boundary.
fn lock_arguments(range: &GpuClockRange) -> Option<[String; 2]> {
    valid_range(range).then(|| {
        [
            "-lgc".to_owned(),
            format!("{},{}", range.min_mhz, range.max_mhz),
        ]
    })
}

pub(super) fn allowed_arguments(arguments: &[String]) -> bool {
    if arguments == [OBSERVATION_QUERY, CSV_FORMAT] {
        return true;
    }
    let [_, pair] = arguments else {
        return false;
    };
    let Some((minimum, maximum)) = pair.split_once(',') else {
        return false;
    };
    let (Ok(min_mhz), Ok(max_mhz)) = (minimum.parse::<u32>(), maximum.parse::<u32>()) else {
        return false;
    };
    lock_arguments(&GpuClockRange { min_mhz, max_mhz })
        .is_some_and(|expected| arguments == expected)
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn reconcile_gpu_clock(
        &self,
        desired: Option<&GpuClockIntent>,
        hourly: bool,
    ) -> Result<(), OperationError> {
        self.reconcile_gpu_clock_at(
            desired,
            hourly,
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs(),
        )
    }

    fn reconcile_gpu_clock_at(
        &self,
        desired: Option<&GpuClockIntent>,
        hourly: bool,
        now: u64,
    ) -> Result<(), OperationError> {
        if desired.is_some_and(|intent| !valid_range(&intent.range)) {
            return Err(OperationError::InvalidOperation);
        }
        let root = self.roots.data.join("gpu-clock");
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
        let _guard = HeldLock::try_exclusive(lock)
            .map_err(|_| OperationError::InstallationReconciliationBusy)?;
        let path = root.join(CHECKPOINT_FILE);
        let current = match OpenOptions::new()
            .read(true)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
            )
            .open(&path)
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
                Some(
                    parse_strict::<GpuClockCheckpoint>(&bytes)
                        .map_err(|_| OperationError::InvalidArtifact)?,
                )
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => return Err(error.into()),
        };
        if let (Some(old), Some(new)) = (&current, desired) {
            if new.generation < old.desired.generation {
                return Ok(());
            }
            if new.generation == old.desired.generation && new.range != old.desired.range {
                return Err(OperationError::InvalidOperation);
            }
        }
        let wanted = desired
            .cloned()
            .or_else(|| current.as_ref().map(|c| c.desired.clone()))
            .unwrap_or_else(|| GpuClockIntent {
                generation: 0,
                range: default_range(),
            });
        let changed = current.as_ref().is_none_or(|c| c.desired != wanted);
        let mut record = if changed {
            GpuClockCheckpoint {
                desired: wanted,
                state: GpuClockApplyState::Waiting,
                detail: None,
                next_attempt_at: Some(now),
                deadline: now + ATTEMPT_SECONDS,
                applied_boot_id: current.as_ref().and_then(|c| c.applied_boot_id.clone()),
                applied_range: current.as_ref().and_then(|c| c.applied_range.clone()),
            }
        } else {
            current.ok_or(OperationError::InvalidArtifact)?
        };
        let boot = fs::read_to_string("/proc/sys/kernel/random/boot_id")
            .ok()
            .map(|s| s.trim().to_owned());
        let reboot = record.applied_boot_id != boot;
        if !changed && !hourly && !reboot && record.state == GpuClockApplyState::Applied {
            return Ok(());
        }
        // After expiry a fresh bounded hourly reconciliation remains possible;
        // ordinary polls cannot extend a poisoned request's deadline forever.
        if hourly
            && (record.state == GpuClockApplyState::Applied
                || record.state == GpuClockApplyState::Expired)
            || reboot && record.state == GpuClockApplyState::Applied
        {
            record.deadline = now + ATTEMPT_SECONDS;
            record.next_attempt_at = Some(now);
        }
        if !changed && record.next_attempt_at.is_some_and(|next| now < next) {
            return Ok(());
        }
        if now >= record.deadline {
            if record.state == GpuClockApplyState::Expired {
                return Ok(());
            }
            record.state = GpuClockApplyState::Expired;
            record.next_attempt_at = None;
            record.detail = Some(format!(
                "GPU clock retry deadline expired; the hourly host-settings reconciliation will retry. Workloads continue. Last cause: {}",
                record.detail.as_deref().unwrap_or("not reported")
            ));
            return self.write_gpu_clock(&root, &path, &record);
        }
        if record.state == GpuClockApplyState::Refused {
            return Ok(());
        }
        record.state = GpuClockApplyState::Waiting;
        record.next_attempt_at = Some(now + RETRY_SECONDS);
        self.write_gpu_clock(&root, &path, &record)?; // intent and retry identity before effects
        eprintln!(
            "vonk-agent-helper: GPU clock before: applied={:?} desired={:?}",
            record.applied_range, record.desired
        );
        let range = &record.desired.range;
        let arguments = lock_arguments(range).ok_or(OperationError::InvalidOperation)?;
        let applied =
            self.runner
                .run_with_timeout(Path::new(NVIDIA_SMI), &arguments, COMMAND_TIMEOUT);
        match applied {
            Ok(output) if output.success => {
                // GB10 does not expose supported levels or a lock getter. The
                // successful ioctl is the receipt; live clocks corroborate it.
                // Idle clocks alone never establish that a cap was applied.
                match self.observe_gpu_clock(range) {
                    Ok(evidence) if boot.is_some() => {
                        record.state = GpuClockApplyState::Applied;
                        record.applied_range = Some(range.clone());
                        record.applied_boot_id = boot;
                        record.detail = Some(evidence);
                        record.next_attempt_at = None;
                    }
                    result => {
                        let cause = result
                            .err()
                            .unwrap_or_else(|| "boot identity unavailable".to_owned());
                        record.detail = Some(format!(
                            "GPU clock command succeeded but observation is unconfirmed: {cause}; retry after {} (deadline {}). Workloads continue.",
                            now + RETRY_SECONDS,
                            record.deadline
                        ));
                    }
                }
            }
            Ok(output) if output.exit_code == Some(4) => {
                record.state = GpuClockApplyState::Refused;
                record.next_attempt_at = None;
                record.detail = Some(format!(
                    "GPU clock permission denied: {}",
                    CommandFailure::exit(&output)
                ));
            }
            Ok(output) if output.exit_code == Some(2) || output.exit_code == Some(3) => {
                record.state = GpuClockApplyState::Refused;
                record.next_attempt_at = None;
                record.detail = Some(format!(
                    "GPU clock request rejected by driver: {}",
                    CommandFailure::exit(&output)
                ));
            }
            result => {
                let cause = match result {
                    Ok(output) => CommandFailure::exit(&output).to_string(),
                    Err(error) => error,
                };
                record.detail = Some(format!(
                    "GPU clock command failed: {cause}; retry after {} (deadline {}). Workloads continue.",
                    now + RETRY_SECONDS,
                    record.deadline
                ));
            }
        }
        eprintln!(
            "vonk-agent-helper: GPU clock after: {:?}: {:?}",
            record.state, record.detail
        );
        self.write_gpu_clock(&root, &path, &record)
    }

    fn observe_gpu_clock(&self, range: &GpuClockRange) -> Result<String, String> {
        let output = self.runner.run_with_timeout(
            Path::new(NVIDIA_SMI),
            &[OBSERVATION_QUERY.to_owned(), CSV_FORMAT.to_owned()],
            COMMAND_TIMEOUT,
        )?;
        if !output.success {
            return Err(CommandFailure::exit(&output).to_string());
        }
        // Leave room for intent and retry metadata in the bounded checkpoint.
        if output.stdout.len() as u64 > RECORD_BYTES / 2 {
            return Err(format!(
                "GPU clock observation exceeds {} bytes: {} bytes",
                RECORD_BYTES / 2,
                output.stdout.len()
            ));
        }
        let text = std::str::from_utf8(&output.stdout).map_err(|error| error.to_string())?;
        let fields: Vec<_> = text.trim().split(',').map(str::trim).collect();
        let [graphics, sm, reasons] = fields.as_slice() else {
            return Err("expected one GPU's graphics, SM and event-reason readings".to_owned());
        };
        let graphics = graphics
            .parse::<u32>()
            .map_err(|error| format!("graphics reading {graphics}: {error}"))?;
        let sm = sm
            .parse::<u32>()
            .map_err(|error| format!("SM reading {sm}: {error}"))?;
        let reasons = match reasons.strip_prefix("0x") {
            Some(hex) => u64::from_str_radix(hex, 16),
            None => reasons.parse(),
        }
        .map_err(|error| format!("event reasons {reasons}: {error}"))?;
        if graphics > range.max_mhz || sm > range.max_mhz {
            return Err(format!(
                "observed graphics {graphics} / SM {sm} MHz exceeds requested ceiling {} MHz; event reasons {reasons}",
                range.max_mhz
            ));
        }
        Ok(format!(
            "Successful helper apply; observed graphics {graphics} / SM {sm} MHz, event reasons {reasons}. Current clocks corroborate the receipt; the driver exposes no lock readback."
        ))
    }

    fn write_gpu_clock(
        &self,
        root: &Path,
        path: &Path,
        record: &GpuClockCheckpoint,
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
        // Public observation projection contains no authority. Root keeps the
        // authoritative checkpoint private; the agent reads only this receipt.
        fs::rename(&temporary, path)?;
        sync_directory(root)?;
        let observation = self.roots.data.join("gpu-clock-observation.json");
        let temporary = self
            .roots
            .data
            .join(format!(".gpu-clock-{}.tmp", uuid::Uuid::new_v4()));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o644)
            .open(&temporary)?;
        file.write_all(&bytes)?;
        // The helper's unit has UMask=0077. This observation is public, while
        // the authoritative checkpoint above remains root-private.
        file.set_permissions(fs::Permissions::from_mode(0o644))?;
        file.sync_all()?;
        fs::rename(temporary, observation)?;
        sync_directory(&self.roots.data)
    }
}

#[cfg(test)]
mod tests;
