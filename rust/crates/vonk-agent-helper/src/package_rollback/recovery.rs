use std::fs::File;
use std::path::Path;
use std::time::{Duration, Instant};

use vonk_agent_protocol::PackageActivationPhase as Phase;
use vonk_agent_protocol::generated::PackageActivationOutcome as Outcome;

use super::commands::command;
use super::custody::{digest, digest_file, now, safe};
use super::{
    AGENT, HELPER, PROCESS_PROOF_INTERVAL, PROCESS_PROOF_TIMEOUT, ROLLBACK_RETRY_TIMEOUT, Store,
    Transaction,
};

impl Store {
    pub fn watch(&self) -> Result<(), String> {
        self.watch_with(
            |tx, remaining| {
                retry_process_proof(Instant::now() + remaining, Duration::from_secs(1), || {
                    self.restore(tx)
                })
            },
            start_services,
        )
    }

    /// `restore` is the bounded restore attempt; `heal` starts the units again.
    pub(super) fn watch_with(
        &self,
        restore: impl Fn(&Transaction, Duration) -> Result<(), String>,
        heal: impl Fn(),
    ) -> Result<(), String> {
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
            if !pending(tx.phase) {
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
                if !restored {
                    heal();
                }
                let (phase, outcome) = if restored {
                    (Phase::RolledBack, Outcome::SourceRestoredAndRestarted)
                } else {
                    (Phase::RollbackFailed, Outcome::SourceRestoreFailed)
                };
                self.advance(&mut tx, phase, outcome, now()?)?;
                return Ok(());
            };
            self.advance(
                &mut tx,
                Phase::RollingBack,
                Outcome::RestoringCapturedSource,
                timestamp,
            )?;
            let result = restore(&tx, remaining);
            if result.is_err() {
                // restore() stopped the agent and helper before dpkg. A failed
                // restore must not leave the Spark unreachable: the failure
                // stays in the journal for the Controller to see and re-drive.
                heal();
            }
            let (phase, outcome) = if result.is_ok() {
                (Phase::RolledBack, Outcome::SourceRestoredAndRestarted)
            } else {
                (Phase::RollbackFailed, Outcome::SourceRestoreFailed)
            };
            self.advance(&mut tx, phase, outcome, now()?)?;
            return result;
        }
    }
    fn restore(&self, tx: &Transaction) -> Result<(), String> {
        let source = self.root.join("source.deb");
        safe(&source, false, self.owner, 0o600)?;
        // The source package was verified against its signed digest at ingress
        // (`prepare`); custody is root-owned and checked by `safe`, so restore
        // never re-hashes it.
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
        command(
            "/usr/bin/dpkg",
            &[
                "--install",
                "--force-confold",
                source.to_str().ok_or("source path")?,
            ],
            true,
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

/// Best effort: bring the helper socket and the agent back after a failed
/// restore. `Restart=always` does not undo an explicit stop.
fn start_services() {
    for unit in [
        "vonk-forge-package-helper.socket",
        "vonk-forge-agent.service",
    ] {
        // --no-block: a Type=notify agent must not hold the store lock.
        if let Err(error) = command(
            "/usr/bin/systemctl",
            &["--system", "start", "--no-block", unit],
            false,
        ) {
            eprintln!("vonk-agent-helper: rollback heal: {unit}: {error}");
        }
    }
}

/// Every legal move of a package activation transaction. Anything else is
/// refused and leaves the journal as it is. A failed or interrupted rollback
/// stays re-drivable: the watchdog resumes it until it finishes.
const EDGES: &[(Phase, Phase)] = &[
    (Phase::Armed, Phase::ActivationFailed),
    (Phase::Armed, Phase::Acknowledged),
    (Phase::Acknowledged, Phase::Acknowledged),
    (Phase::Armed, Phase::RollingBack),
    (Phase::ActivationFailed, Phase::RollingBack),
    (Phase::RollingBack, Phase::RollingBack),
    (Phase::RollbackFailed, Phase::RollingBack),
    (Phase::Armed, Phase::RolledBack),
    (Phase::ActivationFailed, Phase::RolledBack),
    (Phase::RollingBack, Phase::RolledBack),
    (Phase::RollbackFailed, Phase::RolledBack),
    (Phase::Armed, Phase::RollbackFailed),
    (Phase::ActivationFailed, Phase::RollbackFailed),
    (Phase::RollingBack, Phase::RollbackFailed),
    (Phase::RollbackFailed, Phase::RollbackFailed),
];

pub(super) fn can_move(from: Phase, to: Phase) -> bool {
    EDGES.contains(&(from, to))
}

impl Store {
    /// The one place a transaction changes phase: checks the edge, stamps the
    /// outcome and time, and journals it. The caller holds the store lock.
    pub(super) fn advance(
        &self,
        tx: &mut Transaction,
        to: Phase,
        outcome: Outcome,
        at: i64,
    ) -> Result<(), String> {
        if !can_move(tx.phase, to) {
            return Err(format!("illegal activation move {:?} to {to:?}", tx.phase));
        }
        tx.phase = to;
        tx.outcome = outcome;
        tx.updated_at = at;
        self.write(tx)
    }
}

/// Whether the watchdog still has work for a transaction in this phase.
pub(super) fn pending(phase: Phase) -> bool {
    !matches!(phase, Phase::Acknowledged | Phase::RolledBack)
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

pub(crate) fn digest_process(pid: u32) -> Result<String, String> {
    // /proc/PID/exe is a kernel-owned symlink to the actual running executable.
    let mut process = File::open(format!("/proc/{pid}/exe")).map_err(|e| e.to_string())?;
    digest_file(&mut process)
}
