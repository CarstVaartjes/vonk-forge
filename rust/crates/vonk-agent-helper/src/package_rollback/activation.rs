use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use vonk_agent_protocol::generated::PackageActivationOutcome as Outcome;
use vonk_agent_protocol::{PackageActivationPhase as Phase, PackageRollbackAuthority};

use super::commands::command;
use super::custody::{digest, now, safe};
use super::{AGENT, HELPER, Store, Transaction, prerequisites};

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
        // a newer authorized candidate.
        if digest(source)? != authority.source.package_sha256
            || digest(candidate)? != candidate_sha256
        {
            return Err("authorized package digest differs".into());
        }
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
            &[
                "--system",
                vonk_agent_protocol::generated::OperatorActionName::Stop.as_str(),
                "vonk-forge-package-rollback.service",
            ],
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
    pub fn validate_maintainer_rollback(
        &self,
        _version: &str,
        agent: &str,
        helper: &str,
    ) -> Result<(), String> {
        // The watchdog holds the transaction flock while dpkg invokes this
        // read-only verifier. Root custody and the exact unit/nonce bind the
        // nested maintainer process without trying to reacquire that lock.
        safe(self.root.parent().ok_or("rollback parent")?, true, 0, 0o755)?;
        safe(&self.root, true, 0, 0o700)?;
        let tx = self.read()?;
        let cgroup = fs::read_to_string("/proc/self/cgroup").map_err(|e| e.to_string())?;
        let unit_cgroup = command(
            "/usr/bin/systemctl",
            &[
                "--system",
                "show",
                "--property=ControlGroup",
                "--value",
                "vonk-forge-package-rollback.service",
            ],
            false,
        )?;
        if !unit_cgroup.starts_with('/') || unit_cgroup == "/" {
            return Err("rollback unit cgroup unavailable".into());
        }
        let nonce = std::env::var("VONK_FORGE_PACKAGE_ROLLBACK_NONCE")
            .map_err(|_| "rollback nonce missing")?;
        if tx.phase != Phase::RollingBack
            || nonce != tx.rollback.attempt_nonce
            || agent != tx.rollback.source.binary_sha256
            || helper != tx.rollback.source.helper_sha256
            || !cgroup
                .lines()
                .any(|line| line == format!("0::{unit_cgroup}"))
        {
            return Err("maintainer rollback is outside captured source authority".into());
        }
        let source = self.root.join("source.deb");
        safe(&source, false, 0, 0o600)?;
        if digest(&source)? != tx.rollback.source.package_sha256 {
            return Err("captured source changed".into());
        }
        Ok(())
    }
    pub fn activation_failed(&self) -> Result<(), String> {
        let _lock = self.lock()?;
        let mut tx = self.read()?;
        if tx.phase == Phase::Armed {
            tx.phase = Phase::ActivationFailed;
            tx.outcome = Outcome::CandidateInstallFailed;
            tx.updated_at = now()?;
            self.write(&tx)?;
        }
        Ok(())
    }
    pub fn acknowledge(&self, node: &str, candidate: &str, nonce: &str) -> Result<(), String> {
        let _lock = self.lock()?;
        let mut tx = self.read()?;
        self.check_acknowledgement(&tx, node, candidate, nonce, now()?)?;
        if digest(Path::new(AGENT))? != tx.candidate_binary_sha256
            || digest(Path::new(HELPER))? != tx.candidate_helper_sha256
        {
            return Err("candidate executable identity changed".into());
        }
        tx.phase = Phase::Acknowledged;
        tx.updated_at = now()?;
        tx.outcome = Outcome::ControllerConfirmedActivation;
        self.write(&tx)
    }
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
