use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use vonk_agent_protocol::generated::PackageActivationOutcome as Outcome;
use vonk_agent_protocol::{PackageActivationPhase as Phase, PackageRollbackAuthority};

use super::commands::command;
use super::custody::{digest, now, safe};
use super::recovery::pending;
use super::{AGENT, HELPER, PACKAGE, Store, Transaction, prerequisites};

/// What dpkg and the filesystem say is installed at confirm time.
pub(super) struct Observed {
    pub dpkg_status: Result<String, String>,
    pub agent_sha256: String,
    pub helper_sha256: String,
}

impl Store {
    pub fn prepare(
        &self,
        node: &str,
        source: &Path,
        candidate: &Path,
        candidate_sha256: &str,
        authority: &PackageRollbackAuthority,
    ) -> Result<(), String> {
        prerequisites()?;
        command(
            "/usr/bin/systemctl",
            &[
                "--system",
                "is-enabled",
                "--quiet",
                "vonk-forge-package-rollback.service",
            ],
            false,
        )?;
        let timestamp = now()?;
        if node.len() != 36
            || !node.starts_with("spk_")
            || !node[4..]
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            || !authority.valid()
            || authority.activation_deadline <= timestamp
        {
            return Err("invalid activation deadline or source".into());
        }
        // The current signed request supersedes damaged or unfinished journals.
        // No old journal supplies source bytes or rollback authority. Verify the
        // current source bytes before replacing custody; disk drift cannot veto
        // a newer authorized candidate. Both packages arrive through
        // `take_package_custody`, which hashes and signature-checks them; they
        // are not hashed again here.
        for package in [source, candidate] {
            let package = package.to_str().ok_or("invalid package path")?;
            if command("/usr/bin/dpkg-deb", &["--field", package, "Package"], false)?
                != "vonk-forge-agent"
                || command(
                    "/usr/bin/dpkg-deb",
                    &["--field", package, "Architecture"],
                    false,
                )? != "arm64"
            {
                return Err("package identity mismatch".into());
            }
        }
        let candidate_version = command(
            "/usr/bin/dpkg-deb",
            &[
                "--field",
                candidate.to_str().ok_or("candidate path")?,
                "Version",
            ],
            false,
        )?;
        let parent = self.root.parent().ok_or("rollback parent missing")?;
        safe(parent, true, self.owner, 0o755)?;
        let verification = tempfile::Builder::new()
            .prefix(".package-verification-")
            .tempdir_in(parent)
            .map_err(|error| error.to_string())?;
        fs::set_permissions(verification.path(), fs::Permissions::from_mode(0o700))
            .map_err(|error| error.to_string())?;
        let source_extraction = verification.path().join("source");
        command(
            "/usr/bin/dpkg-deb",
            &[
                "--extract",
                source.to_str().ok_or("source path")?,
                source_extraction.to_str().ok_or("source extraction")?,
            ],
            false,
        )?;
        if digest(&source_extraction.join("usr/lib/vonk-forge/vonk-agent"))?
            != authority.source.binary_sha256
            || digest(&source_extraction.join("usr/lib/vonk-forge/vonk-agent-helper"))?
                != authority.source.helper_sha256
        {
            return Err("signed source payload does not match captured installed identity".into());
        }
        let extraction = verification.path().join("candidate");
        command(
            "/usr/bin/dpkg-deb",
            &[
                "--extract",
                candidate.to_str().ok_or("candidate path")?,
                extraction.to_str().ok_or("candidate extraction")?,
            ],
            false,
        )?;
        let tx = Transaction {
            schema_version: 2,
            node_id: node.into(),
            candidate_sha256: candidate_sha256.into(),
            candidate_version,
            candidate_binary_sha256: digest(&extraction.join("usr/lib/vonk-forge/vonk-agent"))?,
            candidate_helper_sha256: digest(
                &extraction.join("usr/lib/vonk-forge/vonk-agent-helper"),
            )?,
            rollback: authority.clone(),
            phase: Phase::Armed,
            created_at: timestamp,
            updated_at: timestamp,
            outcome: Outcome::AwaitingControllerActivation,
        };
        // Fence the previous independent observer only after all ingress bytes
        // are verified. Invalid input never disrupts its authorized recovery.
        command(
            "/usr/bin/systemctl",
            &["--system", "stop", "vonk-forge-package-rollback.service"],
            false,
        )?;
        let _lock = self.lock()?;
        self.copy_custody(source, "source.deb", 0o600)?;
        self.copy_custody(
            &source_extraction.join("usr/lib/vonk-forge/vonk-agent-helper"),
            "runner",
            0o500,
        )?;
        self.write(&tx)?;
        // Static installed unit survives reboot; its executable is the preserved
        // source helper, not the file that dpkg is about to replace.
        command("/usr/bin/systemctl", &["--system", "daemon-reload"], false)?;
        command(
            "/usr/bin/systemctl",
            &[
                "--system",
                "start",
                "--no-block",
                "vonk-forge-package-rollback.service",
            ],
            false,
        )?;
        Ok(())
    }
    pub fn activation_failed(&self) -> Result<(), String> {
        let _lock = self.lock()?;
        let mut tx = self.read()?;
        if tx.phase == Phase::Armed {
            self.advance(
                &mut tx,
                Phase::ActivationFailed,
                Outcome::CandidateInstallFailed,
                now()?,
            )?;
        }
        Ok(())
    }
    /// Removal supersedes an upgrade still in flight. A transaction the
    /// watchdog would still act on is deleted with its receipt: no journal is
    /// no rollback authority, so the watchdog ends at its next check and the
    /// unit's `ConditionPathExists` keeps it from starting at boot. Finished
    /// transactions and a missing store stay as they are.
    pub fn retire(&self) -> Result<(), String> {
        let journal = self.root.join("transaction.json");
        if matches!(fs::symlink_metadata(&journal), Err(error) if error.kind() == std::io::ErrorKind::NotFound)
        {
            return Ok(());
        }
        let _lock = self.lock()?;
        // Unreadable counts as no authority already: the watchdog ends on it.
        if !self.read().is_ok_and(|tx| pending(tx.phase)) {
            return Ok(());
        }
        fs::remove_file(&journal).map_err(|error| format!("{}: {error}", journal.display()))?;
        let receipt = self
            .root
            .parent()
            .ok_or("rollback parent missing")?
            .join("package-activation.receipt.json");
        match fs::remove_file(&receipt) {
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
                Err(format!("{}: {error}", receipt.display()))
            }
            _ => Ok(()),
        }
    }
    pub fn acknowledge(&self, node: &str, candidate: &str, nonce: &str) -> Result<(), String> {
        let _lock = self.lock()?;
        let tx = self.read()?;
        let timestamp = now()?;
        // Cheap request checks first: a wrong or expired request never spawns dpkg.
        self.check_acknowledgement(&tx, node, candidate, nonce, timestamp)?;
        // Observe what is installed: any unreadable observation refuses, the
        // transaction stays Armed and the watchdog restores at the deadline.
        let observed = Observed {
            dpkg_status: command(
                "/usr/bin/dpkg-query",
                &["-W", "-f=${Status}\t${Version}", PACKAGE],
                false,
            ),
            agent_sha256: digest(Path::new(AGENT))?,
            helper_sha256: digest(Path::new(HELPER))?,
        };
        self.finish_acknowledgement(tx, &observed, timestamp)
    }
    pub(super) fn finish_acknowledgement(
        &self,
        mut tx: Transaction,
        observed: &Observed,
        timestamp: i64,
    ) -> Result<(), String> {
        let installed = format!("install ok installed\t{}", tx.candidate_version);
        if observed.dpkg_status.as_deref() != Ok(installed.as_str())
            || observed.agent_sha256 != tx.candidate_binary_sha256
            || observed.helper_sha256 != tx.candidate_helper_sha256
        {
            return Err("candidate is not the installed package".into());
        }
        self.advance(
            &mut tx,
            Phase::Acknowledged,
            Outcome::ControllerConfirmedActivation,
            timestamp,
        )
    }
    #[expect(
        clippy::unused_self,
        reason = "a method so it stays beside its siblings behind the same type"
    )]
    pub(super) fn check_acknowledgement(
        &self,
        tx: &Transaction,
        node: &str,
        candidate: &str,
        nonce: &str,
        timestamp: i64,
    ) -> Result<(), String> {
        if tx.node_id != node
            || tx.candidate_sha256 != candidate
            || tx.rollback.attempt_nonce != nonce
            || !matches!(tx.phase, Phase::Armed | Phase::Acknowledged)
            || timestamp >= tx.rollback.activation_deadline
        {
            return Err("activation acknowledgement does not match this attempt".into());
        }
        Ok(())
    }
}
