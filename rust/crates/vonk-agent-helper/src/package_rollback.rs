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
const PROCESS_PROOF_TIMEOUT: Duration = Duration::from_secs(15);
const PROCESS_PROOF_INTERVAL: Duration = Duration::from_millis(100);
const ROLLBACK_RETRY_TIMEOUT: Duration = Duration::from_secs(600);

use vonk_agent_protocol::generated::PackageActivationOutcome as Outcome;
pub use vonk_agent_protocol::generated::PackageRollbackTransaction as Transaction;

fn now() -> Result<i64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .map_err(|e| e.to_string())
}
fn digest(path: &Path) -> Result<String, String> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32)
        .open(path)
        .map_err(|e| e.to_string())?;
    digest_file(&mut file)
}

fn digest_file(file: &mut File) -> Result<String, String> {
    let metadata = file.metadata().map_err(|error| error.to_string())?;
    if !metadata.is_file() {
        return Err("package identity is not regular executable data".into());
    }
    let mut hash = Sha256::new();
    let mut remaining = metadata.len() + 1;
    let mut buffer = [0; 65536];
    let deadline = Instant::now() + Duration::from_secs(180);
    while remaining > 0 {
        if Instant::now() >= deadline {
            return Err(
                vonk_agent_protocol::generated::WaitReason::ObservationUnavailable.to_string(),
            );
        }
        let limit = remaining.min(buffer.len() as u64) as usize;
        let count = file
            .read(&mut buffer[..limit])
            .map_err(|error| error.to_string())?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
        remaining -= count as u64;
    }
    let copied = metadata.len() + 1 - remaining;
    if copied != metadata.len() {
        return Err("package identity bytes changed during verification".into());
    }
    Ok(hex::encode(hash.finalize()))
}

fn publish_managed(temporary: &Path, destination: &Path) -> Result<(), String> {
    if fs::symlink_metadata(destination).is_ok_and(|metadata| metadata.is_dir()) {
        // Preserve an unexpected managed object without traversing it. Its old
        // shape is neither rollback authority nor a gate on a current request.
        let parent = destination.parent().ok_or("managed parent missing")?;
        fs::rename(
            destination,
            parent.join(format!(".retired-{}", uuid::Uuid::new_v4())),
        )
        .map_err(|error| error.to_string())?;
    }
    fs::rename(temporary, destination).map_err(|error| error.to_string())
}

fn safe(path: &Path, directory: bool, owner: u32, mode: u32) -> Result<(), String> {
    let m = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
    if m.file_type().is_symlink()
        || m.is_dir() != directory
        || m.uid() != owner
        || m.mode() & 0o777 & !mode != 0
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
    if path == "/usr/bin/dpkg" {
        let output = crate::package_command::run(&mut cmd, Duration::from_secs(180))?;
        if !output.status.success() || output.timed_out {
            return Err(format!(
                "package command failed: {path}: {}",
                String::from_utf8_lossy(&output.diagnostic())
            ));
        }
        return Ok(String::new());
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
            let _ = child.wait_timeout(Duration::from_secs(5));
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
        match fs::symlink_metadata(&self.root) {
            Ok(metadata) if metadata.is_dir() && metadata.uid() == self.owner => {
                fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))
                    .map_err(|error| error.to_string())?;
            }
            Ok(_) => {
                let parent = self.root.parent().ok_or("rollback parent missing")?;
                fs::rename(
                    &self.root,
                    parent.join(format!(".retired-{}", uuid::Uuid::new_v4())),
                )
                .map_err(|error| error.to_string())?;
                fs::create_dir(&self.root).map_err(|error| error.to_string())?;
                fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))
                    .map_err(|error| error.to_string())?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                fs::create_dir(&self.root).map_err(|error| error.to_string())?;
                fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))
                    .map_err(|error| error.to_string())?;
            }
            Err(error) => return Err(error.to_string()),
        }
        safe(&self.root, true, self.owner, 0o700)?;
        let path = self.root.join("lock");
        if let Ok(metadata) = fs::symlink_metadata(&path) {
            if metadata.is_file() && metadata.uid() == self.owner && metadata.nlink() == 1 {
                // Preserve the inode and any active flock while fixing mode.
                fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                    .map_err(|error| error.to_string())?;
            } else {
                fs::rename(
                    &path,
                    self.root
                        .join(format!(".retired-lock-{}", uuid::Uuid::new_v4())),
                )
                .map_err(|error| error.to_string())?;
            }
        }
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
            )
            .open(&path)
            .map_err(|e| e.to_string())?;
        safe(&path, false, self.owner, 0o600)?;
        retry_process_proof(
            Instant::now() + Duration::from_secs(2),
            Duration::from_millis(50),
            || {
                rustix::fs::flock(&file, rustix::fs::FlockOperation::NonBlockingLockExclusive)
                    .map_err(|error| error.to_string())
            },
        )?;
        Ok(file)
    }
    fn read(&self) -> Result<Transaction, String> {
        let path = self.root.join("transaction.json");
        if let Ok(metadata) = fs::symlink_metadata(&path)
            && metadata.is_file()
            && metadata.uid() == self.owner
            && metadata.nlink() == 1
            && metadata.mode() & 0o022 == 0
        {
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                .map_err(|error| error.to_string())?;
        }
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
        publish_managed(&path, &self.root.join("transaction.json"))?;
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
            outcome: tx.outcome,
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
        // Service UMask=0077 narrows create mode to 0600. This public,
        // root-owned receipt must be readable by the unprivileged agent.
        output
            .set_permissions(fs::Permissions::from_mode(0o644))
            .map_err(|e| e.to_string())?;
        output.sync_all().map_err(|e| e.to_string())?;
        publish_managed(&temporary, &parent.join("package-activation.receipt.json"))?;
        File::open(parent)
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())
    }
    fn copy_custody(&self, source: &Path, name: &str, mode: u32) -> Result<(), String> {
        let temporary = self.root.join(format!(".copy-{}", uuid::Uuid::new_v4()));
        let mut input = OpenOptions::new()
            .read(true)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
            )
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
        publish_managed(&temporary, &self.root.join(name))?;
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

fn rollback_retry_budget(tx: &Transaction, timestamp: i64) -> Option<Duration> {
    let deadline = tx
        .rollback
        .activation_deadline
        .saturating_add(ROLLBACK_RETRY_TIMEOUT.as_secs() as i64);
    let remaining = deadline.saturating_sub(timestamp);
    (remaining > 0)
        .then(|| Duration::from_secs((remaining as u64).min(ROLLBACK_RETRY_TIMEOUT.as_secs())))
}

fn retry_process_proof<F>(
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

fn prove_running_process(service: &str, expected_digest: &str) -> Result<(), String> {
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
            outcome: Outcome::AwaitingControllerActivation,
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
    }
    #[test]
    fn activation_receipt_permissions_survive_service_umask() {
        let status = std::process::Command::new("/bin/sh")
            .args(["-c", "umask 077; exec \"$@\"", "receipt-test"])
            .arg(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "package_rollback::tests::durable_transaction_roundtrip_retains_interrupted_rollback",
            ])
            .status()
            .unwrap();
        assert!(status.success());
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
        let receipt = temporary.path().join("package-activation.receipt.json");
        assert_eq!(fs::metadata(&receipt).unwrap().mode() & 0o777, 0o644);
        assert_eq!(
            fs::metadata(store.root.join("transaction.json"))
                .unwrap()
                .mode()
                & 0o777,
            0o600
        );
        let recovered = store.read().unwrap();
        assert_eq!(recovered.phase, Phase::RollingBack);
        assert_eq!(recovered.rollback, tx.rollback);
        assert_eq!(recovered.node_id, tx.node_id);
        fs::set_permissions(
            store.root.join("transaction.json"),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        assert_eq!(store.read().unwrap().rollback, tx.rollback);
        assert_eq!(
            fs::metadata(store.root.join("transaction.json"))
                .unwrap()
                .mode()
                & 0o777,
            0o600
        );
        fs::set_permissions(
            store.root.join("transaction.json"),
            fs::Permissions::from_mode(0o666),
        )
        .unwrap();
        assert!(store.read().is_err());
        store.write(&tx).unwrap();
        assert!(store.read().is_ok());
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
        assert!(parse_strict::<Transaction>(&serde_json::to_vec(&transaction()).unwrap()).is_ok());
    }

    #[test]
    fn process_proof_retries_transient_identity_failures() {
        let mut attempts = 0;
        let result = retry_process_proof(
            Instant::now() + Duration::from_secs(1),
            Duration::ZERO,
            || {
                attempts += 1;
                if attempts < 3 {
                    Err("identity not ready".into())
                } else {
                    Ok(())
                }
            },
        );
        assert!(result.is_ok());
        assert_eq!(attempts, 3);
    }

    #[test]
    fn process_proof_returns_the_last_error_at_the_deadline() {
        let mut attempts = 0;
        let result = retry_process_proof(Instant::now(), Duration::ZERO, || {
            attempts += 1;
            Err("identity unavailable".into())
        });
        assert!(result.is_err());
        assert_eq!(attempts, 1);
        assert!(retry_process_proof(Instant::now(), Duration::ZERO, || Ok(())).is_ok());
    }
    #[test]
    fn rollback_retry_end_survives_restart_and_a_fresh_request_is_admitted() {
        let mut tx = transaction();
        let end = tx.rollback.activation_deadline + ROLLBACK_RETRY_TIMEOUT.as_secs() as i64;
        assert_eq!(
            rollback_retry_budget(&tx, end - 1),
            Some(Duration::from_secs(1))
        );
        tx.updated_at = end - 1;
        tx.phase = Phase::RollingBack;
        assert!(rollback_retry_budget(&tx, end).is_none());
        let temporary = tempfile::tempdir().unwrap();
        let store = Store {
            root: temporary.path().join("rollback"),
            owner: fs::metadata(temporary.path()).unwrap().uid(),
        };
        let lock = store.lock().unwrap();
        tx.phase = Phase::RollbackFailed;
        tx.outcome = Outcome::SourceRestoreFailed;
        store.write(&tx).unwrap();
        let mut fresh = transaction();
        fresh.created_at = end;
        fresh.updated_at = end;
        fresh.rollback.activation_deadline = end + 120;
        fresh.rollback.attempt_nonce = "7".repeat(64);
        store.write(&fresh).unwrap();
        assert_eq!(store.read().unwrap().rollback, fresh.rollback);
        drop(lock);
        assert!(store.lock().is_ok());
    }

    #[test]
    fn more_private_parent_permissions_do_not_block_fresh_admission() {
        let temporary = tempfile::tempdir().unwrap();
        fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let store = Store {
            root: temporary.path().join("rollback"),
            owner: fs::metadata(temporary.path()).unwrap().uid(),
        };
        drop(store.lock().unwrap());
        assert!(store.lock().is_ok());
        assert_eq!(
            fs::metadata(temporary.path()).unwrap().mode() & 0o777,
            0o700
        );
    }

    #[test]
    fn busy_owner_ends_boundedly_and_releases_for_a_fresh_request() {
        let temporary = tempfile::tempdir().unwrap();
        fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o755)).unwrap();
        let store = Store {
            root: temporary.path().join("rollback"),
            owner: fs::metadata(temporary.path()).unwrap().uid(),
        };
        let owner = store.lock().unwrap();
        let start = Instant::now();
        assert!(store.lock().is_err());
        assert!(start.elapsed() < Duration::from_secs(3));
        drop(owner);
        let fresh = store.lock().unwrap();
        fs::write(store.root.join("transaction.json"), b"damaged history").unwrap();
        let tx = transaction();
        store.write(&tx).unwrap();
        assert_eq!(store.read().unwrap().rollback, tx.rollback);
        drop(fresh);
        assert!(store.lock().is_ok());
    }
    #[test]
    fn unexpected_journal_object_is_a_miss_for_current_verified_publication() {
        let temporary = tempfile::tempdir().unwrap();
        fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o755)).unwrap();
        let store = Store {
            root: temporary.path().join("rollback"),
            owner: fs::metadata(temporary.path()).unwrap().uid(),
        };
        let owner = store.lock().unwrap();
        let journal = store.root.join("transaction.json");
        fs::create_dir(&journal).unwrap();
        fs::write(journal.join("unrecognized-entry"), b"preserve this").unwrap();
        assert!(store.read().is_err());
        store.write(&transaction()).unwrap();
        assert_eq!(store.read().unwrap().rollback, transaction().rollback);
        drop(owner);
        assert!(store.lock().is_ok());
    }
    #[test]
    fn damaged_lock_shape_does_not_poison_fresh_admission() {
        let temporary = tempfile::tempdir().unwrap();
        fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o755)).unwrap();
        let store = Store {
            root: temporary.path().join("rollback"),
            owner: fs::metadata(temporary.path()).unwrap().uid(),
        };
        drop(store.lock().unwrap());
        fs::remove_file(store.root.join("lock")).unwrap();
        fs::create_dir(store.root.join("lock")).unwrap();
        fs::write(store.root.join("lock/unknown-entry"), b"preserved").unwrap();
        drop(store.lock().unwrap());
        assert!(store.lock().is_ok());
    }
}
