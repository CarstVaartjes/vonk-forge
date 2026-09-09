//! Root-custody activation transaction, independent of the replaced helper cgroup.
//!
//! Source bytes enter here only after the signed artifact verifier has copied
//! them to root custody. A rollback never chooses a version, path or command
//! from a request: it restores precisely that captured source package.
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use vonk_agent_protocol::{
    PackageActivationPhase as Phase, PackageActivationReceipt, PackageRollbackAuthority,
    parse_strict,
};
use wait_timeout::ChildExt;

const STATE: &str = "/var/lib/vonk-forge/package-rollback";
const AGENT: &str = "/usr/lib/vonk-forge/vonk-agent";
const HELPER: &str = "/usr/lib/vonk-forge/vonk-agent-helper";
const PATH: &str = "/usr/sbin:/usr/bin:/sbin:/bin";
const CAPSULE_UNIT: &str = "vonk-forge-package-upgrade-recover-capsule.service";
const PROCESS_PROOF_TIMEOUT: Duration = Duration::from_secs(15);
const PROCESS_PROOF_INTERVAL: Duration = Duration::from_millis(100);

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Transaction {
    pub schema_version: u8,
    pub node_id: String,
    pub candidate_sha256: String,
    pub candidate_version: String,
    pub candidate_binary_sha256: String,
    pub candidate_helper_sha256: String,
    pub rollback: PackageRollbackAuthority,
    pub phase: Phase,
    pub created_at: i64,
    pub updated_at: i64,
    pub outcome: String,
}

fn now() -> Result<i64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .map_err(|e| e.to_string())
}
fn digest(path: &Path) -> Result<String, String> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)
        .map_err(|e| e.to_string())?;
    let mut hash = Sha256::new();
    let mut buffer = [0; 65536];
    loop {
        let n = file.read(&mut buffer).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    Ok(hex::encode(hash.finalize()))
}
fn safe(path: &Path, directory: bool, owner: u32, mode: u32) -> Result<(), String> {
    let m = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
    if m.file_type().is_symlink()
        || m.is_dir() != directory
        || m.uid() != owner
        || m.mode() & 0o777 != mode
        || (!directory && (!m.is_file() || m.nlink() != 1))
    {
        return Err("unsafe rollback custody".into());
    }
    Ok(())
}
fn command(path: &str, arguments: &[&str], offline: bool) -> Result<String, String> {
    command_with_nonce(path, arguments, offline, None)
}

fn command_with_nonce(
    path: &str,
    arguments: &[&str],
    offline: bool,
    nonce: Option<&str>,
) -> Result<String, String> {
    let capture = path == "/usr/bin/dpkg-query"
        || (path == "/usr/bin/systemctl" && arguments.contains(&"show"))
        || (path == "/usr/bin/dpkg-deb" && arguments.first() == Some(&"--field"));
    let mut cmd = Command::new(path);
    cmd.args(arguments)
        .env_clear()
        .env("PATH", PATH)
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .current_dir("/")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(if capture {
            Stdio::piped()
        } else {
            Stdio::null()
        });
    if offline {
        cmd.env("SYSTEMD_OFFLINE", "1");
    }
    if let Some(nonce) = nonce {
        cmd.env("VONK_FORGE_PACKAGE_ROLLBACK_NONCE", nonce);
    }
    let mut child = cmd
        .spawn()
        .map_err(|_| format!("required executable unavailable: {path}"))?;
    // Package metadata output is small. Mutating commands deliberately suppress
    // output so a verbose package cannot fill this pipe while its parent waits.

    let status = match child
        .wait_timeout(Duration::from_secs(180))
        .map_err(|e| e.to_string())?
    {
        Some(status) => status,
        None => {
            let _ = child.kill();
            let _ = child.wait();
            return Err("package command timed out".into());
        }
    };
    if !status.success() {
        return Err(format!("package command failed: {path}"));
    }
    let mut result = String::new();
    if let Some(stdout) = child.stdout.take() {
        stdout
            .take(65536)
            .read_to_string(&mut result)
            .map_err(|e| e.to_string())?;
    }
    Ok(result.trim().to_owned())
}

pub fn prerequisites() -> Result<(), String> {
    // Probe before dpkg invokes prerm and stops the healthy source agent. This
    // set is the actual current maintainer-script and watchdog executable set.
    for executable in [
        "/bin/sh",
        "/usr/bin/awk",
        "/usr/bin/base64",
        "/usr/bin/cat",
        "/usr/bin/chmod",
        "/usr/bin/chown",
        "/usr/bin/cp",
        "/usr/bin/cmp",
        "/usr/bin/deb-systemd-helper",
        "/usr/bin/deb-systemd-invoke",
        "/usr/bin/dirname",
        "/usr/bin/getent",
        "/usr/bin/ln",
        "/usr/bin/loginctl",
        "/usr/bin/openssl",
        "/usr/bin/sleep",
        "/usr/bin/tail",
        "/usr/sbin/addgroup",
        "/usr/sbin/nologin",
        "/usr/bin/cut",
        "/usr/bin/date",
        "/usr/bin/dpkg",
        "/usr/bin/dpkg-deb",
        "/usr/bin/dpkg-query",
        "/usr/bin/find",
        "/usr/bin/flock",
        "/usr/bin/grep",
        "/usr/bin/id",
        "/usr/bin/install",
        "/usr/bin/logger",
        "/usr/bin/mkdir",
        "/usr/bin/mktemp",
        "/usr/bin/mv",
        "/usr/bin/readlink",
        "/usr/bin/rm",
        "/usr/bin/rmdir",
        "/usr/bin/sed",
        "/usr/bin/sha256sum",
        "/usr/bin/stat",
        "/usr/bin/sync",
        "/usr/bin/systemctl",
        "/usr/bin/systemd-run",
        "/usr/bin/tr",
        "/usr/bin/wc",
        "/usr/sbin/adduser",
        "/usr/sbin/ldconfig",
        "/usr/sbin/start-stop-daemon",
    ] {
        let m = fs::metadata(executable)
            .map_err(|_| format!("missing package prerequisite: {executable}"))?;
        if !m.is_file() || m.uid() != 0 || m.mode() & 0o111 == 0 || m.mode() & 0o022 != 0 {
            return Err(format!("unsafe package prerequisite: {executable}"));
        }
    }
    command(
        "/usr/bin/systemctl",
        &["--system", "show", "--property=Version", "--value"],
        false,
    )?;
    Ok(())
}

pub struct Store {
    root: PathBuf,
    owner: u32,
}
impl Store {
    pub fn system() -> Self {
        Self {
            root: PathBuf::from(STATE),
            owner: 0,
        }
    }
    fn lock(&self) -> Result<File, String> {
        safe(
            self.root.parent().ok_or("rollback parent missing")?,
            true,
            self.owner,
            0o755,
        )?;
        if !self.root.exists() {
            fs::create_dir(&self.root).map_err(|e| e.to_string())?;
            fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))
                .map_err(|e| e.to_string())?;
        }
        safe(&self.root, true, self.owner, 0o700)?;
        let path = self.root.join("lock");
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(&path)
            .map_err(|e| e.to_string())?;
        safe(&path, false, self.owner, 0o600)?;
        rustix::fs::flock(&file, rustix::fs::FlockOperation::LockExclusive)
            .map_err(|e| e.to_string())?;
        Ok(file)
    }
    fn read(&self) -> Result<Transaction, String> {
        let path = self.root.join("transaction.json");
        safe(&path, false, self.owner, 0o600)?;
        let tx: Transaction = parse_strict(&fs::read(path).map_err(|e| e.to_string())?)
            .map_err(|_| "invalid rollback transaction")?;
        if tx.schema_version != 2 || !tx.rollback.valid() {
            return Err("invalid rollback authority".into());
        }
        Ok(tx)
    }
    fn write(&self, tx: &Transaction) -> Result<(), String> {
        let path = self.root.join(format!(".{}.json", uuid::Uuid::new_v4()));
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&path)
            .map_err(|e| e.to_string())?;
        file.write_all(&serde_json::to_vec(tx).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        file.sync_all().map_err(|e| e.to_string())?;
        fs::rename(path, self.root.join("transaction.json")).map_err(|e| e.to_string())?;
        File::open(&self.root)
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())?;
        let receipt = PackageActivationReceipt {
            schema_version: 2,
            node_id: tx.node_id.clone(),
            source_package_sha256: tx.rollback.source.package_sha256.clone(),
            source_version: tx.rollback.source.package_version.clone(),
            source_binary_sha256: tx.rollback.source.binary_sha256.clone(),
            candidate_package_sha256: tx.candidate_sha256.clone(),
            candidate_version: tx.candidate_version.clone(),
            candidate_binary_sha256: tx.candidate_binary_sha256.clone(),
            attempt_nonce: tx.rollback.attempt_nonce.clone(),
            phase: tx.phase,
            created_at: tx.created_at,
            updated_at: tx.updated_at,
            outcome: tx.outcome.clone(),
        };
        receipt.validate()?;
        let parent = self.root.parent().ok_or("receipt parent")?;
        let temporary = parent.join(format!(".activation-{}.json", uuid::Uuid::new_v4()));
        let mut output = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o644)
            .open(&temporary)
            .map_err(|e| e.to_string())?;
        output
            .write_all(&serde_json::to_vec(&receipt).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        output.sync_all().map_err(|e| e.to_string())?;
        fs::rename(temporary, parent.join("package-activation.receipt.json"))
            .map_err(|e| e.to_string())?;
        File::open(parent)
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())
    }
    fn copy_custody(&self, source: &Path, name: &str, mode: u32) -> Result<(), String> {
        let temporary = self.root.join(format!(".copy-{}", uuid::Uuid::new_v4()));
        let mut input = OpenOptions::new()
            .read(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(source)
            .map_err(|e| e.to_string())?;
        let mut output = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(mode)
            .open(&temporary)
            .map_err(|e| e.to_string())?;
        std::io::copy(&mut input, &mut output).map_err(|e| e.to_string())?;
        output.sync_all().map_err(|e| e.to_string())?;
        fs::rename(temporary, self.root.join(name)).map_err(|e| e.to_string())?;
        File::open(&self.root)
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())
    }
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
        let _lock = self.lock()?;
        let timestamp = now()?;
        if !authority.valid()
            || authority.activation_deadline <= timestamp
            || authority.activation_deadline > timestamp + 3600
            || authority.source.package_sha256 == candidate_sha256
        {
            return Err("invalid activation deadline or source".into());
        }
        if self.root.join("transaction.json").exists() {
            let previous = self.read()?;
            if previous.rollback.attempt_nonce == authority.attempt_nonce
                || self
                    .root
                    .join(format!("receipt-{}.json", authority.attempt_nonce))
                    .exists()
            {
                return Err("activation attempt nonce was already used".into());
            }
            if !matches!(previous.phase, Phase::Acknowledged | Phase::RolledBack) {
                return Err("another package activation is unresolved".into());
            }
            fs::rename(
                self.root.join("transaction.json"),
                self.root
                    .join(format!("receipt-{}.json", previous.rollback.attempt_nonce)),
            )
            .map_err(|e| e.to_string())?;
        }
        if digest(source)? != authority.source.package_sha256
            || digest(candidate)? != candidate_sha256
            || digest(Path::new(AGENT))? != authority.source.binary_sha256
            || digest(Path::new(HELPER))? != authority.source.helper_sha256
        {
            return Err("captured source identity differs from installed package".into());
        }
        let installed = command(
            "/usr/bin/dpkg-query",
            &[
                "-W",
                "-f=${db:Status-Abbrev}|${Version}",
                "vonk-forge-agent",
            ],
            false,
        )?;
        if installed != format!("ii |{}", authority.source.package_version) {
            return Err("source package is not configured".into());
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
        command(
            "/usr/bin/dpkg",
            &[
                "--compare-versions",
                &candidate_version,
                "ge",
                &authority.source.package_version,
            ],
            false,
        )
        .map_err(|_| "candidate would downgrade the healthy source")?;
        let source_extraction = self.root.join("source-check");
        if source_extraction.exists() {
            fs::remove_dir_all(&source_extraction).map_err(|e| e.to_string())?;
        }
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
            || command(
                "/usr/bin/dpkg-deb",
                &["--field", source.to_str().ok_or("source path")?, "Version"],
                false,
            )? != authority.source.package_version
        {
            return Err("signed source payload does not match captured installed identity".into());
        }
        self.copy_custody(source, "source.deb", 0o600)?;
        self.copy_custody(Path::new(HELPER), "runner", 0o500)?;
        let extraction = self.root.join("candidate");
        if extraction.exists() {
            fs::remove_dir_all(&extraction).map_err(|e| e.to_string())?;
        }
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
            outcome: "awaiting_controller_activation".into(),
        };
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
        version: &str,
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
            || version != tx.rollback.source.package_version
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
            // dpkg can report the initial maintainer-script failure while the
            // package's signed recovery capsule is still completing the exact
            // same candidate.  Let that root-owned owner finish; otherwise the
            // rollback watchdog can stop it and downgrade a candidate that was
            // already repaired successfully.
            if now()? < tx.rollback.activation_deadline
                && self.candidate_recovery_can_finish(&tx)?
            {
                return Ok(());
            }
            tx.phase = Phase::ActivationFailed;
            tx.outcome = "candidate_install_failed".into();
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
        tx.outcome = "controller_confirmed_activation".into();
        self.write(&tx)
    }
    fn check_acknowledgement(
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
    pub fn watch(&self) -> Result<(), String> {
        loop {
            let lock = self.lock()?;
            let mut tx = self.read()?;
            if matches!(tx.phase, Phase::Acknowledged | Phase::RolledBack) {
                return Ok(());
            }
            if matches!(
                tx.phase,
                Phase::ActivationFailed | Phase::RollingBack | Phase::RollbackFailed
            ) && now()? < tx.rollback.activation_deadline
                && self.candidate_recovery_can_finish(&tx)?
            {
                drop(lock);
                std::thread::sleep(Duration::from_secs(1));
                continue;
            }
            if tx.phase == Phase::Armed && now()? < tx.rollback.activation_deadline {
                drop(lock);
                std::thread::sleep(Duration::from_secs(1));
                continue;
            }
            tx.phase = Phase::RollingBack;
            tx.updated_at = now()?;
            tx.outcome = "restoring_captured_source".into();
            self.write(&tx)?;
            let result = self.restore(&tx);
            tx.updated_at = now()?;
            if result.is_ok() {
                tx.phase = Phase::RolledBack;
                tx.outcome = "source_restored_and_restarted".into();
            } else {
                tx.phase = Phase::RollbackFailed;
                tx.outcome = "source_restore_failed".into();
            }
            self.write(&tx)?;
            return result;
        }
    }
    fn restore(&self, tx: &Transaction) -> Result<(), String> {
        // Recheck immediately before stopping any unit.  The capsule may have
        // become active after watch() inspected the transaction, and stopping
        // it here would race its root-owned package repair.
        if now()? < tx.rollback.activation_deadline && self.candidate_recovery_can_finish(tx)? {
            return Err("candidate recovery still owns activation".into());
        }
        let source = self.root.join("source.deb");
        safe(&source, false, self.owner, 0o600)?;
        if digest(&source)? != tx.rollback.source.package_sha256 {
            return Err("captured source changed".into());
        }
        // Refuse to overwrite a third identity; resuming an interrupted restore
        // may see either this candidate or precisely the captured source.
        let installed = command(
            "/usr/bin/dpkg-query",
            &["-W", "-f=${Version}", "vonk-forge-agent"],
            false,
        )
        .ok();
        if installed.as_ref().is_some_and(|value| {
            value != &tx.candidate_version && value != &tx.rollback.source.package_version
        }) {
            return Err("installed version escaped rollback authority".into());
        }
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
        let installed = command(
            "/usr/bin/dpkg-query",
            &[
                "-W",
                "-f=${db:Status-Abbrev}|${Version}",
                "vonk-forge-agent",
            ],
            false,
        )?;
        if installed != format!("ii |{}", tx.rollback.source.package_version) {
            return Err("source package is not configured after rollback".into());
        }
        prove_running_process(
            "vonk-forge-agent.service",
            &tx.rollback.source.binary_sha256,
        )
        .map_err(|_| "source process identity differs after rollback".to_owned())?;
        Ok(())
    }

    fn candidate_recovery_can_finish(&self, tx: &Transaction) -> Result<bool, String> {
        if candidate_recovery_intent_matches(tx)? && recovery_capsule_is_active()? {
            return Ok(true);
        }
        candidate_process_matches(tx)
    }
}

fn candidate_recovery_intent_matches(tx: &Transaction) -> Result<bool, String> {
    let root = Path::new("/var/lib/vonk-forge/package-upgrade");
    let intent = root.join("intent");
    if !intent.exists() {
        return Ok(false);
    }
    safe(root, true, 0, 0o700)?;
    safe(&intent, false, 0, 0o600)?;
    let text = fs::read_to_string(intent).map_err(|e| e.to_string())?;
    Ok(candidate_recovery_intent_matches_text(&text, tx))
}

fn candidate_recovery_intent_matches_text(text: &str, tx: &Transaction) -> bool {
    let lines = text.lines().collect::<Vec<_>>();
    if lines.len() != 17
        || lines[0] != "schema_version=2"
        || lines[1] != "package=vonk-forge-agent"
        || lines[2] != format!("target_version={}", tx.candidate_version)
        || lines[3] != "architecture=arm64"
        || lines[4] != format!("package_sha256={}", tx.candidate_sha256)
        || !decimal_field(lines[5], "package_bytes")
        || lines[6] != format!("agent_sha256={}", tx.candidate_binary_sha256)
        || lines[7] != format!("helper_sha256={}", tx.candidate_helper_sha256)
        || !hex_field(lines[8], "runner_sha256")
        || !hex_field(lines[9], "unit_sha256")
        || !boot_id_field(lines[10], "request_boot_id")
        || !decimal_field(lines[11], "dpkg_pid")
        || !decimal_field(lines[12], "dpkg_start_time")
        || !hex_field(lines[13], "recovery_nonce")
        || !hex_field(lines[14], "capsule_unit_sha256")
        || !hex_field(lines[15], "capsule_gate_sha256")
        || !hex_field(lines[16], "capsule_suppression_sha256")
    {
        return false;
    }
    true
}

fn field_value<'a>(line: &'a str, key: &str) -> Option<&'a str> {
    line.strip_prefix(key)
        .and_then(|value| value.strip_prefix('='))
}

fn decimal_field(line: &str, key: &str) -> bool {
    field_value(line, key)
        .is_some_and(|value| !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()))
}

fn hex_field(line: &str, key: &str) -> bool {
    field_value(line, key).is_some_and(|value| {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

fn boot_id_field(line: &str, key: &str) -> bool {
    field_value(line, key).is_some_and(|value| {
        value.len() == 36
            && value.bytes().enumerate().all(|(index, byte)| {
                if [8, 13, 18, 23].contains(&index) {
                    byte == b'-'
                } else {
                    byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
                }
            })
    })
}

fn recovery_capsule_is_active() -> Result<bool, String> {
    let state = command(
        "/usr/bin/systemctl",
        &[
            "--system",
            "show",
            "--property=ActiveState",
            "--value",
            CAPSULE_UNIT,
        ],
        false,
    )?;
    Ok(matches!(state.as_str(), "active" | "activating"))
}

fn candidate_process_matches(tx: &Transaction) -> Result<bool, String> {
    let installed = match command(
        "/usr/bin/dpkg-query",
        &[
            "-W",
            "-f=${db:Status-Abbrev}|${Version}",
            "vonk-forge-agent",
        ],
        false,
    ) {
        Ok(value) => value,
        Err(_) => return Ok(false),
    };
    if installed != format!("ii |{}", tx.candidate_version)
        || digest(Path::new(AGENT)).ok().as_deref() != Some(tx.candidate_binary_sha256.as_str())
        || digest(Path::new(HELPER)).ok().as_deref() != Some(tx.candidate_helper_sha256.as_str())
    {
        return Ok(false);
    }
    let active = command(
        "/usr/bin/systemctl",
        &[
            "--system",
            "is-active",
            "--quiet",
            "vonk-forge-agent.service",
        ],
        false,
    )
    .is_ok();
    if !active {
        return Ok(false);
    }
    let pid = command(
        "/usr/bin/systemctl",
        &[
            "--system",
            "show",
            "--property=MainPID",
            "--value",
            "vonk-forge-agent.service",
        ],
        false,
    )?;
    let Ok(pid) = pid.parse::<u32>() else {
        return Ok(false);
    };
    Ok(pid > 1 && digest_process(pid).is_ok_and(|value| value == tx.candidate_binary_sha256))
}

fn prove_running_process(service: &str, expected_digest: &str) -> Result<(), String> {
    let deadline = Instant::now() + PROCESS_PROOF_TIMEOUT;
    loop {
        let active = command(
            "/usr/bin/systemctl",
            &["--system", "is-active", "--quiet", service],
            false,
        )
        .is_ok();
        if active {
            if let Ok(pid) = command(
                "/usr/bin/systemctl",
                &["--system", "show", "--property=MainPID", "--value", service],
                false,
            )
            .and_then(|value| {
                value
                    .parse::<u32>()
                    .map_err(|_| "invalid process id".into())
            }) {
                if pid > 1 && digest_process(pid).is_ok_and(|value| value == expected_digest) {
                    return Ok(());
                }
            }
        }
        if Instant::now() >= deadline {
            return Err("process identity proof timed out".into());
        }
        std::thread::sleep(PROCESS_PROOF_INTERVAL);
    }
}

fn digest_process(pid: u32) -> Result<String, String> {
    // /proc/PID/exe is a kernel-owned symlink to the actual running executable.
    let mut process = File::open(format!("/proc/{pid}/exe")).map_err(|e| e.to_string())?;
    let mut hash = Sha256::new();
    let mut buffer = [0; 65536];
    loop {
        let size = process.read(&mut buffer).map_err(|e| e.to_string())?;
        if size == 0 {
            break;
        }
        hash.update(&buffer[..size]);
    }
    Ok(hex::encode(hash.finalize()))
}

fn retire_candidate_recovery(candidate: &str) -> Result<(), String> {
    let root = Path::new("/var/lib/vonk-forge/package-upgrade");
    let intent = root.join("intent");
    if !intent.exists() {
        return Ok(());
    }
    safe(root, true, 0, 0o700)?;
    safe(&intent, false, 0, 0o600)?;
    let text = fs::read_to_string(&intent).map_err(|e| e.to_string())?;
    if text.lines().count() != 17
        || text.lines().next() != Some("schema_version=2")
        || text.lines().nth(1) != Some("package=vonk-forge-agent")
        || text.lines().nth(4) != Some(format!("package_sha256={candidate}").as_str())
    {
        return Err("candidate recovery belongs to another transaction".into());
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
            _ => return Err("unsafe candidate recovery gate".into()),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn transaction() -> Transaction {
        Transaction {
            schema_version: 2,
            node_id: "spk_11111111111111111111111111111111".into(),
            candidate_sha256: "a".repeat(64),
            candidate_version: "0.2.0".into(),
            candidate_binary_sha256: "b".repeat(64),
            candidate_helper_sha256: "c".repeat(64),
            rollback: PackageRollbackAuthority {
                source: vonk_agent_protocol::PackageRollbackSource {
                    package_sha256: "d".repeat(64),
                    package_signature: "e".repeat(128),
                    package_version: "0.1.0".into(),
                    binary_sha256: "f".repeat(64),
                    helper_sha256: "1".repeat(64),
                },
                attempt_nonce: "2".repeat(64),
                activation_deadline: 200,
            },
            phase: Phase::Armed,
            created_at: 100,
            updated_at: 100,
            outcome: "awaiting_controller_activation".into(),
        }
    }
    #[test]
    fn acknowledgement_is_bound_to_node_candidate_attempt_and_deadline() {
        let store = Store::system();
        let tx = transaction();
        assert!(
            store
                .check_acknowledgement(
                    &tx,
                    &tx.node_id,
                    &tx.candidate_sha256,
                    &tx.rollback.attempt_nonce,
                    150
                )
                .is_ok()
        );
        for (node, candidate, nonce, time) in [
            (
                "other",
                tx.candidate_sha256.as_str(),
                tx.rollback.attempt_nonce.as_str(),
                150,
            ),
            (
                tx.node_id.as_str(),
                "wrong",
                tx.rollback.attempt_nonce.as_str(),
                150,
            ),
            (
                tx.node_id.as_str(),
                tx.candidate_sha256.as_str(),
                "wrong",
                150,
            ),
            (
                tx.node_id.as_str(),
                tx.candidate_sha256.as_str(),
                tx.rollback.attempt_nonce.as_str(),
                200,
            ),
        ] {
            assert!(
                store
                    .check_acknowledgement(&tx, node, candidate, nonce, time)
                    .is_err()
            );
        }
        let mut reverting = tx.clone();
        reverting.phase = Phase::RollingBack;
        assert!(
            store
                .check_acknowledgement(
                    &reverting,
                    &tx.node_id,
                    &tx.candidate_sha256,
                    &tx.rollback.attempt_nonce,
                    150
                )
                .is_err()
        );
    }
    #[test]
    fn durable_transaction_roundtrip_retains_interrupted_rollback() {
        let temporary = tempfile::tempdir().unwrap();
        fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o755)).unwrap();
        let store = Store {
            root: temporary.path().join("rollback"),
            owner: fs::metadata(temporary.path()).unwrap().uid(),
        };
        let _lock = store.lock().unwrap();
        let mut tx = transaction();
        tx.phase = Phase::RollingBack;
        store.write(&tx).unwrap();
        let recovered = store.read().unwrap();
        assert_eq!(recovered.phase, Phase::RollingBack);
        assert_eq!(recovered.rollback, tx.rollback);
        assert_eq!(recovered.node_id, tx.node_id);
        fs::set_permissions(
            store.root.join("transaction.json"),
            fs::Permissions::from_mode(0o666),
        )
        .unwrap();
        assert!(store.read().is_err());
    }
    #[test]
    fn current_transaction_rejects_commands_paths_and_schema_coercion() {
        let mut value = serde_json::to_value(transaction()).unwrap();
        value["command"] = serde_json::json!("sh -c arbitrary");
        assert!(parse_strict::<Transaction>(&serde_json::to_vec(&value).unwrap()).is_err());
        let mut value = serde_json::to_value(transaction()).unwrap();
        value["rollback"]["source"]["package_sha256"] = serde_json::json!("../../source.deb");
        assert!(parse_strict::<Transaction>(&serde_json::to_vec(&value).unwrap()).is_err());
        let mut value = serde_json::to_value(transaction()).unwrap();
        value["schema_version"] = serde_json::json!(2.0);
        assert!(parse_strict::<Transaction>(&serde_json::to_vec(&value).unwrap()).is_err());
    }

    #[test]
    fn recovery_capsule_coordination_requires_the_exact_candidate_intent() {
        let tx = transaction();
        let intent = format!(
            "schema_version=2
package=vonk-forge-agent
target_version={}
architecture=arm64
package_sha256={}
package_bytes=123
agent_sha256={}
helper_sha256={}
runner_sha256={}
unit_sha256={}
request_boot_id=01234567-89ab-cdef-0123-456789abcdef
dpkg_pid=123
dpkg_start_time=456
recovery_nonce={}
capsule_unit_sha256={}
capsule_gate_sha256={}
capsule_suppression_sha256={}
",
            tx.candidate_version,
            tx.candidate_sha256,
            tx.candidate_binary_sha256,
            tx.candidate_helper_sha256,
            "a".repeat(64),
            "b".repeat(64),
            tx.rollback.attempt_nonce,
            "c".repeat(64),
            "d".repeat(64),
            "e".repeat(64),
        );
        assert!(candidate_recovery_intent_matches_text(&intent, &tx));

        let mut changed = intent.replace(
            &format!("package_sha256={}", tx.candidate_sha256),
            &format!("package_sha256={}", "f".repeat(64)),
        );
        assert!(!candidate_recovery_intent_matches_text(&changed, &tx));
        changed.push_str("unexpected=field\n");
        assert!(!candidate_recovery_intent_matches_text(&changed, &tx));
    }
}
