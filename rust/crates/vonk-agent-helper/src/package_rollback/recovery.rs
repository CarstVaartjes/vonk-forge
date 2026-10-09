use std::fs::{self, File};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use vonk_agent_protocol::PackageActivationPhase as Phase;
use vonk_agent_protocol::generated::PackageActivationOutcome as Outcome;

use super::commands::{command, command_with_nonce};
use super::custody::{digest, digest_file, now, safe};
use super::{
    AGENT, HELPER, PROCESS_PROOF_INTERVAL, PROCESS_PROOF_TIMEOUT, ROLLBACK_RETRY_TIMEOUT, Store,
    Transaction,
};

impl Store {
    pub fn watch(&self) -> Result<(), String> {
        loop {
            let lock = self.lock()?;
            let mut observed = None;
            let observation = retry_process_proof(
                Instant::now() + Duration::from_secs(2),
                Duration::from_millis(100),
                || {
                    observed = Some(self.read()?);
                    Ok(())
                },
            );
            if observation.is_err() {
                // No journal means no rollback authority. End this observer;
                // a current verified request can replace it without a veto.
                return Ok(());
            }
            let mut tx = observed.ok_or("rollback observation unavailable")?;
            if matches!(tx.phase, Phase::Acknowledged | Phase::RolledBack) {
                return Ok(());
            }
            if tx.phase == Phase::Armed && now()? < tx.rollback.activation_deadline {
                drop(lock);
                std::thread::sleep(Duration::from_secs(1));
                continue;
            }
            let timestamp = now()?;
            let Some(remaining) = rollback_retry_budget(&tx, timestamp) else {
                // At expiry, reconcile exact current effects without another
                // destructive attempt. Unconfirmed effects remain a failed
                // completion, never a claim that the source is running.
                let restored = digest(Path::new(AGENT))
                    .is_ok_and(|digest| digest == tx.rollback.source.binary_sha256)
                    && digest(Path::new(HELPER))
                        .is_ok_and(|digest| digest == tx.rollback.source.helper_sha256)
                    && prove_running_process(
                        "vonk-forge-agent.service",
                        &tx.rollback.source.binary_sha256,
                    )
                    .is_ok();
                tx.phase = if restored {
                    Phase::RolledBack
                } else {
                    Phase::RollbackFailed
                };
                tx.updated_at = now()?;
                tx.outcome = if restored {
                    Outcome::SourceRestoredAndRestarted
                } else {
                    Outcome::SourceRestoreFailed
                };
                self.write(&tx)?;
                return Ok(());
            };
            tx.phase = Phase::RollingBack;
            tx.updated_at = timestamp;
            tx.outcome = Outcome::RestoringCapturedSource;
            self.write(&tx)?;
            let result =
                retry_process_proof(Instant::now() + remaining, Duration::from_secs(1), || {
                    self.restore(&tx)
                });
            tx.updated_at = now()?;
            if result.is_ok() {
                tx.phase = Phase::RolledBack;
                tx.outcome = Outcome::SourceRestoredAndRestarted;
            } else {
                tx.phase = Phase::RollbackFailed;
                tx.outcome = Outcome::SourceRestoreFailed;
            }
            self.write(&tx)?;
            return result;
        }
    }
    fn restore(&self, tx: &Transaction) -> Result<(), String> {
        let source = self.root.join("source.deb");
        safe(&source, false, self.owner, 0o600)?;
        if digest(&source)? != tx.rollback.source.package_sha256 {
            return Err("captured source changed".into());
        }
        // Refuse to overwrite a third identity; resuming an interrupted restore
        // may see either this candidate or precisely the captured source.
        for (path, expected_source, expected_candidate) in [
            (
                AGENT,
                &tx.rollback.source.binary_sha256,
                &tx.candidate_binary_sha256,
            ),
            (
                HELPER,
                &tx.rollback.source.helper_sha256,
                &tx.candidate_helper_sha256,
            ),
        ] {
            if Path::new(path).exists() {
                let actual = digest(Path::new(path))?;
                if actual != *expected_source && actual != *expected_candidate {
                    return Err("installed executable escaped rollback authority".into());
                }
            }
        }
        for unit in [
            "vonk-forge-package-upgrade-recover-capsule.service",
            "vonk-forge-package-upgrade-recover.service",
            "vonk-forge-agent.service",
            "vonk-forge-package-helper.service",
        ] {
            let loaded = command(
                "/usr/bin/systemctl",
                &["--system", "show", "--property=LoadState", "--value", unit],
                false,
            )?;
            if loaded != "not-found" {
                command("/usr/bin/systemctl", &["--system", "stop", unit], false)?;
            }
        }
        retire_candidate_recovery(&tx.candidate_sha256)?;
        command_with_nonce(
            "/usr/bin/dpkg",
            &[
                "--install",
                "--force-confold",
                source.to_str().ok_or("source path")?,
            ],
            true,
            Some(&tx.rollback.attempt_nonce),
        )?;
        if digest(Path::new(AGENT))? != tx.rollback.source.binary_sha256
            || digest(Path::new(HELPER))? != tx.rollback.source.helper_sha256
        {
            return Err("restored source executable mismatch".into());
        }
        command("/usr/bin/systemctl", &["--system", "daemon-reload"], false)?;
        for unit in [
            "vonk-forge-package-helper.socket",
            "vonk-forge-agent.service",
        ] {
            command("/usr/bin/systemctl", &["--system", "restart", unit], false)?;
        }
        prove_running_process(
            "vonk-forge-agent.service",
            &tx.rollback.source.binary_sha256,
        )
        .map_err(|_| "source process identity differs after rollback".to_owned())?;
        Ok(())
    }
}

pub(super) fn rollback_retry_budget(tx: &Transaction, timestamp: i64) -> Option<Duration> {
    let deadline = tx
        .rollback
        .activation_deadline
        .saturating_add(ROLLBACK_RETRY_TIMEOUT.as_secs() as i64);
    let remaining = deadline.saturating_sub(timestamp);
    (remaining > 0)
        .then(|| Duration::from_secs((remaining as u64).min(ROLLBACK_RETRY_TIMEOUT.as_secs())))
}

pub(super) fn retry_process_proof<F>(
    deadline: Instant,
    interval: Duration,
    mut attempt: F,
) -> Result<(), String>
where
    F: FnMut() -> Result<(), String>,
{
    loop {
        let error = match attempt() {
            Ok(()) => return Ok(()),
            Err(error) => error,
        };
        if Instant::now() >= deadline {
            return Err(error);
        }
        std::thread::sleep(interval);
    }
}

pub(super) fn prove_running_process(service: &str, expected_digest: &str) -> Result<(), String> {
    retry_process_proof(
        Instant::now() + PROCESS_PROOF_TIMEOUT,
        PROCESS_PROOF_INTERVAL,
        || {
            command(
                "/usr/bin/systemctl",
                &["--system", "is-active", "--quiet", service],
                false,
            )?;
            let pid = command(
                "/usr/bin/systemctl",
                &["--system", "show", "--property=MainPID", "--value", service],
                false,
            )?
            .parse::<u32>()
            .map_err(|_| "invalid process id".to_owned())?;
            if pid <= 1 {
                return Err("service process is unavailable".into());
            }
            if digest_process(pid)? != expected_digest {
                return Err("service process identity differs".into());
            }
            Ok(())
        },
    )
}

fn digest_process(pid: u32) -> Result<String, String> {
    // /proc/PID/exe is a kernel-owned symlink to the actual running executable.
    let mut process = File::open(format!("/proc/{pid}/exe")).map_err(|e| e.to_string())?;
    digest_file(&mut process)
}

fn retire_candidate_recovery(candidate: &str) -> Result<(), String> {
    let root = Path::new("/var/lib/vonk-forge/package-upgrade");
    let intent = root.join("intent");
    if !intent.exists() {
        return Ok(());
    }
    if safe(root, true, 0, 0o700).is_err() || safe(&intent, false, 0, 0o600).is_err() {
        return Ok(());
    }
    let Ok(text) = fs::read_to_string(&intent) else {
        return Ok(());
    };
    if text.lines().count() != 17
        || text.lines().next() != Some("schema_version=2")
        || text.lines().nth(1) != Some("package=vonk-forge-agent")
        || text.lines().nth(4) != Some(format!("package_sha256={candidate}").as_str())
    {
        // Unmatched history cannot authorize cleanup or veto source recovery.
        return Ok(());
    }
    for path in [
        intent,
        root.join("agent-blocked"),
        PathBuf::from("/var/lib/vonk-forge/helper-upgrade.pending"),
        PathBuf::from(
            "/lib/systemd/system/vonk-forge-agent.service.d/10-package-upgrade-capsule.conf",
        ),
        PathBuf::from(
            "/lib/systemd/system/vonk-forge-agent.service.d/20-package-upgrade-recovery.conf",
        ),
    ] {
        match fs::symlink_metadata(&path) {
            Ok(m) if m.is_file() && !m.file_type().is_symlink() && m.uid() == 0 => {
                fs::remove_file(path).map_err(|e| e.to_string())?
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            _ => (), // Preserve unknown entries; they are not restore authority.
        }
    }
    Ok(())
}
