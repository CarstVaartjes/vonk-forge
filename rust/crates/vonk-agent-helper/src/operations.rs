use std::collections::{BTreeSet, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use ring::signature;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::{
    HostRuntimeAction, HostRuntimeRequest, RecipeRunObservationOutcome, canonical_json, hex_sha256,
    parse_strict,
};
use wait_timeout::ChildExt;

use crate::protocol::{
    ContainerRuntimeAction, HostOperation, ManagedArea, RestartUnit, artifact_signing_bytes,
};

const MAX_ARTIFACT_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_RUNTIME_ARCHIVE_BYTES: u64 = 1024 * 1024 * 1024 * 1024;
const MAX_COMMAND_OUTPUT_BYTES: u64 = 4096;
const MAX_RUNTIME_REQUEST_BYTES: u64 = 64 * 1024;
const DOCKER_FIREWALL: &str = "/usr/lib/vonk-forge/vonk-forge-docker-firewall";
const DOCKER_FIREWALL_CONFIG: &str = "/etc/vonk-forge-agent/docker-firewall.conf";

#[derive(Debug, Error)]
pub enum OperationError {
    #[error("managed operation is invalid")]
    InvalidOperation,
    #[error("managed path is unsafe")]
    UnsafePath,
    #[error("artifact verification failed")]
    InvalidArtifact,
    #[error("package metadata verification failed")]
    PackageMetadataInvalid,
    #[error("package installation failed")]
    PackageInstallFailed { exit_code: Option<i32> },
    #[error("compiled command failed")]
    CommandFailed,
    #[error("one-shot runtime could not be stopped safely")]
    StopUncertain,
    #[error("host mutation failed")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone)]
pub struct ManagedRoots {
    pub data: PathBuf,
    pub models: PathBuf,
    pub state: PathBuf,
    pub workloads: PathBuf,
    pub incoming: PathBuf,
    pub package_custody: PathBuf,
    pub runtime_requests: PathBuf,
    pub runtime_image_receipts: PathBuf,
    pub agent_data: PathBuf,
}

impl ManagedRoots {
    pub fn under(data: &Path) -> Self {
        Self {
            data: data.to_path_buf(),
            models: data.join("models"),
            state: data.join("state"),
            workloads: data.join("workloads"),
            incoming: data.join("incoming"),
            package_custody: data.join("helper/package-candidates"),
            runtime_requests: data.join("runtime-requests"),
            runtime_image_receipts: data.join("runtime-images"),
            agent_data: data.to_path_buf(),
        }
    }

    pub fn with_runtime_requests(mut self, root: &Path) -> Self {
        self.runtime_requests = root.to_path_buf();
        self
    }

    pub fn with_agent_data(mut self, root: &Path) -> Self {
        self.agent_data = root.to_path_buf();
        self
    }

    pub fn with_package_custody(mut self, root: &Path) -> Self {
        self.package_custody = root.to_path_buf();
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub success: bool,
    pub stdout: Vec<u8>,
    pub exit_code: Option<i32>,
}

pub trait CommandRunner: Send + Sync {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String>;

    fn run_with_timeout(
        &self,
        executable: &Path,
        arguments: &[String],
        _timeout: Duration,
    ) -> Result<CommandOutput, String> {
        self.run(executable, arguments)
    }
}

#[derive(Debug, Clone, Copy)]
pub struct ProcessCommandRunner;

impl CommandRunner for ProcessCommandRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        let timeout = if executable == Path::new("/usr/bin/dpkg") {
            Duration::from_secs(120)
        } else if executable == Path::new("/usr/bin/docker") {
            Duration::from_secs(600)
        } else {
            Duration::from_secs(30)
        };
        self.run_with_timeout(executable, arguments, timeout)
    }

    fn run_with_timeout(
        &self,
        executable: &Path,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<CommandOutput, String> {
        if !matches!(
            executable.to_str(),
            Some(
                "/usr/bin/dpkg-deb"
                    | "/usr/bin/dpkg"
                    | "/usr/bin/systemctl"
                    | "/usr/bin/systemd-run"
                    | DOCKER_FIREWALL
                    | "/usr/bin/docker"
                    | "/usr/bin/setfacl"
            )
        ) {
            return Err("executable is not compiled into the helper".to_owned());
        }
        let capture_output = matches!(
            executable.to_str(),
            Some("/usr/bin/dpkg-deb" | "/usr/bin/docker")
        ) && !(executable == Path::new("/usr/bin/docker")
            && arguments
                .iter()
                .any(|value| value.starts_with("VONK_JOB_TIMEOUT_SECONDS=")));
        let mut command = Command::new(executable);
        command
            .args(arguments)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("PATH", "/usr/bin:/bin")
            .current_dir("/")
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .stdout(if capture_output {
                Stdio::piped()
            } else {
                Stdio::null()
            });
        let mut child = command
            .spawn()
            .map_err(|_| "compiled command could not start".to_owned())?;
        let reader = child.stdout.take().map(|mut stdout| {
            thread::spawn(move || {
                let mut value = Vec::new();
                stdout
                    .by_ref()
                    .take(MAX_COMMAND_OUTPUT_BYTES + 1)
                    .read_to_end(&mut value)
                    .map(|_| value)
            })
        });
        let status = match child.wait_timeout(timeout) {
            Ok(Some(status)) => status,
            Ok(None) | Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("compiled command exceeded its deadline".to_owned());
            }
        };
        let stdout = match reader {
            Some(reader) => reader
                .join()
                .map_err(|_| "compiled command output failed".to_owned())?
                .map_err(|_| "compiled command output failed".to_owned())?,
            None => Vec::new(),
        };
        if stdout.len() as u64 > MAX_COMMAND_OUTPUT_BYTES {
            return Err("compiled command output exceeded its bound".to_owned());
        }
        Ok(CommandOutput {
            success: status.success(),
            stdout,
            exit_code: status.code(),
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OperationOutcome {
    pub schema_version: u8,
    pub status: String,
    pub evidence_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exit_code: Option<i32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recipe_run_observation: Option<RecipeRunObservationOutcome>,
}

struct RuntimeRequestOutcome {
    exit_code: Option<i32>,
    recipe_run_observation: Option<RecipeRunObservationOutcome>,
}

struct RuntimeRequestGrantBinding<'a> {
    job_id: &'a uuid::Uuid,
    operation_id: &'a uuid::Uuid,
    attempt: u32,
    fence: &'a uuid::Uuid,
}

#[derive(Default)]
struct JobCancellationState {
    active_starts: HashSet<String>,
    cancelled: HashSet<String>,
}

#[derive(Default)]
struct JobCancellationFence {
    state: Mutex<JobCancellationState>,
}

struct ActiveJobStart<'a> {
    fence: &'a JobCancellationFence,
    run_id: String,
}

impl JobCancellationFence {
    fn begin(&self, run_id: &str) -> Result<ActiveJobStart<'_>, OperationError> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?;
        if state.cancelled.contains(run_id) || !state.active_starts.insert(run_id.to_owned()) {
            return Err(OperationError::CommandFailed);
        }
        Ok(ActiveJobStart {
            fence: self,
            run_id: run_id.to_owned(),
        })
    }

    fn cancel(&self, run_id: &str) -> Result<(), OperationError> {
        self.state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?
            .cancelled
            .insert(run_id.to_owned());
        Ok(())
    }

    fn is_active(&self, run_id: &str) -> Result<bool, OperationError> {
        Ok(self
            .state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?
            .active_starts
            .contains(run_id))
    }
}

impl Drop for ActiveJobStart<'_> {
    fn drop(&mut self) {
        if let Ok(mut state) = self.fence.state.lock() {
            state.active_starts.remove(&self.run_id);
        }
    }
}

pub struct OperationExecutor<R> {
    roots: ManagedRoots,
    release_public_key: [u8; 32],
    runner: R,
    required_owner_uid: Option<u32>,
    package_owner_uid: Option<u32>,
    runtime_request_owner_uid: Option<u32>,
    package_install: Mutex<()>,
    job_cancellation: JobCancellationFence,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ArtifactIdentity {
    device: u64,
    inode: u64,
    uid: u32,
    gid: u32,
    mode: u32,
    links: u64,
    bytes: u64,
    modified_seconds: i64,
    modified_nanoseconds: i64,
    changed_seconds: i64,
    changed_nanoseconds: i64,
}

struct CustodiedPackage {
    path: PathBuf,
    invocation_directory: PathBuf,
    custody_root: PathBuf,
    cleaned: bool,
}

impl CustodiedPackage {
    fn new(path: PathBuf, invocation_directory: PathBuf, custody_root: PathBuf) -> Self {
        Self {
            path,
            invocation_directory,
            custody_root,
            cleaned: false,
        }
    }

    fn path(&self) -> &Path {
        &self.path
    }

    fn cleanup(mut self) -> Result<(), OperationError> {
        self.cleanup_inner()?;
        self.cleaned = true;
        Ok(())
    }

    fn cleanup_inner(&self) -> Result<(), OperationError> {
        match fs::remove_file(&self.path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        match fs::remove_dir(&self.invocation_directory) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        sync_directory(&self.custody_root)
    }
}

impl Drop for CustodiedPackage {
    fn drop(&mut self) {
        if !self.cleaned {
            let _ = self.cleanup_inner();
        }
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub fn new(
        roots: ManagedRoots,
        release_public_key: &[u8],
        runner: R,
        required_owner_uid: Option<u32>,
    ) -> Result<Self, OperationError> {
        let release_public_key = release_public_key
            .try_into()
            .map_err(|_| OperationError::InvalidArtifact)?;
        if !roots.data.is_absolute()
            || ![
                &roots.models,
                &roots.state,
                &roots.workloads,
                &roots.incoming,
            ]
            .iter()
            .all(|path| path.starts_with(&roots.data))
        {
            return Err(OperationError::UnsafePath);
        }
        Ok(Self {
            roots,
            release_public_key,
            runner,
            required_owner_uid,
            package_owner_uid: required_owner_uid,
            runtime_request_owner_uid: required_owner_uid,
            package_install: Mutex::new(()),
            job_cancellation: JobCancellationFence::default(),
        })
    }

    pub fn with_package_owner(mut self, uid: u32) -> Self {
        self.package_owner_uid = Some(uid);
        self
    }

    pub fn with_runtime_request_owner(mut self, uid: u32) -> Self {
        self.runtime_request_owner_uid = Some(uid);
        self
    }

    pub fn prepare_package_custody(&self) -> Result<(), OperationError> {
        let _install_guard = self
            .package_install
            .lock()
            .map_err(|_| OperationError::CommandFailed)?;
        if !self.roots.package_custody.is_absolute() {
            return Err(OperationError::UnsafePath);
        }
        let custody_parent = self
            .roots
            .package_custody
            .parent()
            .ok_or(OperationError::UnsafePath)?;
        require_safe_directory(custody_parent, self.required_owner_uid)?;
        ensure_private_directory(&self.roots.package_custody, self.required_owner_uid)?;

        let mut invocations =
            fs::read_dir(&self.roots.package_custody)?.collect::<Result<Vec<_>, _>>()?;
        invocations.sort_by_key(fs::DirEntry::file_name);
        for invocation in invocations {
            let name = invocation.file_name();
            let name = name.to_str().ok_or(OperationError::UnsafePath)?;
            if !lower_hex(name, 32) {
                return Err(OperationError::UnsafePath);
            }
            let directory = invocation.path();
            require_exact_directory(&directory, self.required_owner_uid, 0o700)?;
            let mut candidates = fs::read_dir(&directory)?.collect::<Result<Vec<_>, _>>()?;
            if candidates.len() > 1 {
                return Err(OperationError::UnsafePath);
            }
            if let Some(candidate) = candidates.pop() {
                let candidate_name = candidate.file_name();
                let candidate_name = candidate_name
                    .to_str()
                    .and_then(|value| value.strip_suffix(".deb"))
                    .ok_or(OperationError::UnsafePath)?;
                if !lower_hex(candidate_name, 64) {
                    return Err(OperationError::UnsafePath);
                }
                let metadata = fs::symlink_metadata(candidate.path())?;
                if !safe_custody_file(&metadata, self.required_owner_uid, metadata.len()) {
                    return Err(OperationError::UnsafePath);
                }
                fs::remove_file(candidate.path())?;
            }
            fs::remove_dir(directory)?;
        }
        sync_directory(&self.roots.package_custody)
    }

    pub fn execute(&self, operation: &HostOperation) -> Result<OperationOutcome, OperationError> {
        self.execute_for_node(operation, None)
    }

    pub fn execute_for_node(
        &self,
        operation: &HostOperation,
        observation_node_id: Option<&str>,
    ) -> Result<OperationOutcome, OperationError> {
        operation
            .validate()
            .map_err(|_| OperationError::InvalidOperation)?;
        self.require_directory(&self.roots.data)?;
        let (status, evidence, exit_code, recipe_run_observation) = match operation {
            HostOperation::CreateManagedDirectory {
                area,
                relative_path,
            } => {
                let path = self.create_managed_directory(area, relative_path)?;
                (
                    "directory-created",
                    path.to_string_lossy().into_owned(),
                    None,
                    None,
                )
            }
            HostOperation::InstallVonkDeb {
                package_sha256,
                package_signature,
            } => {
                self.install_package(package_sha256, package_signature)?;
                ("package-installed", package_sha256.clone(), None, None)
            }
            HostOperation::RestartVonkUnit { unit } => {
                let unit_name = self.restart_unit(unit)?;
                ("unit-restarted", unit_name.to_owned(), None, None)
            }
            HostOperation::ScheduleReboot { delay_seconds } => {
                self.schedule_reboot(*delay_seconds)?;
                ("reboot-scheduled", delay_seconds.to_string(), None, None)
            }
            HostOperation::ExecuteContainerRuntimeRequest {
                action,
                job_id,
                operation_id,
                attempt,
                fence,
                request_sha256,
                observation_identity_sha256,
            } => {
                let outcome = self.execute_runtime_request(
                    action,
                    RuntimeRequestGrantBinding {
                        job_id,
                        operation_id,
                        attempt: *attempt,
                        fence,
                    },
                    request_sha256,
                    observation_identity_sha256.as_deref(),
                    observation_node_id,
                );
                let (status, exit_code, recipe_run_observation) = match outcome {
                    Ok(outcome) => (
                        "container-runtime-request-executed",
                        outcome.exit_code,
                        outcome.recipe_run_observation,
                    ),
                    Err(OperationError::StopUncertain) => {
                        ("container-runtime-stop-uncertain", Some(124), None)
                    }
                    Err(error) => return Err(error),
                };
                (
                    status,
                    request_sha256.clone(),
                    exit_code,
                    recipe_run_observation,
                )
            }
        };
        Ok(OperationOutcome {
            schema_version: 1,
            status: status.to_owned(),
            evidence_sha256: hex_sha256(evidence.as_bytes()),
            exit_code,
            recipe_run_observation,
        })
    }

    fn create_managed_directory(
        &self,
        area: &ManagedArea,
        relative_path: &str,
    ) -> Result<PathBuf, OperationError> {
        let root = match area {
            ManagedArea::Models => &self.roots.models,
            ManagedArea::State => &self.roots.state,
            ManagedArea::Workloads => &self.roots.workloads,
        };
        let canonical_root = fs::canonicalize(root).map_err(|_| OperationError::UnsafePath)?;
        self.require_directory(root)?;
        let relative = Path::new(relative_path);
        if relative.is_absolute()
            || relative
                .components()
                .any(|component| !matches!(component, Component::Normal(_)))
        {
            return Err(OperationError::UnsafePath);
        }
        let mut current = root.clone();
        for component in relative.components() {
            let Component::Normal(component) = component else {
                return Err(OperationError::UnsafePath);
            };
            current.push(component);
            match fs::symlink_metadata(&current) {
                Ok(metadata) => {
                    if metadata.file_type().is_symlink() || !metadata.is_dir() {
                        return Err(OperationError::UnsafePath);
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    fs::create_dir(&current)?;
                    fs::set_permissions(&current, fs::Permissions::from_mode(0o750))?;
                }
                Err(error) => return Err(OperationError::Io(error)),
            }
            let canonical = fs::canonicalize(&current).map_err(|_| OperationError::UnsafePath)?;
            if !canonical.starts_with(&canonical_root) {
                return Err(OperationError::UnsafePath);
            }
            self.require_directory(&current)?;
        }
        sync_directory(root)?;
        Ok(current)
    }

    fn install_package(
        &self,
        digest: &str,
        detached_signature: &str,
    ) -> Result<(), OperationError> {
        let _install_guard = self
            .package_install
            .lock()
            .map_err(|_| OperationError::CommandFailed)?;
        require_safe_directory(&self.roots.incoming, self.package_owner_uid)?;
        let incoming = self.roots.incoming.join(format!("{digest}.deb"));
        let package = self.take_package_custody(&incoming, digest, detached_signature)?;
        let package_name = package.path().to_string_lossy().into_owned();
        self.require_package_field(&package_name, "Package", "vonk-forge-agent")?;
        self.require_package_field(&package_name, "Architecture", "arm64")?;
        let result = self
            .runner
            .run(
                Path::new("/usr/bin/dpkg"),
                &[
                    "--install".to_owned(),
                    "--force-confold".to_owned(),
                    package_name,
                ],
            )
            .map_err(|_| OperationError::PackageInstallFailed { exit_code: None })?;
        if !result.success {
            return Err(OperationError::PackageInstallFailed {
                exit_code: result.exit_code,
            });
        }
        package.cleanup()?;
        Ok(())
    }

    fn take_package_custody(
        &self,
        incoming: &Path,
        expected_digest: &str,
        detached_signature: &str,
    ) -> Result<CustodiedPackage, OperationError> {
        if !self.roots.package_custody.is_absolute() {
            return Err(OperationError::UnsafePath);
        }
        let custody_parent = self
            .roots
            .package_custody
            .parent()
            .ok_or(OperationError::UnsafePath)?;
        require_safe_directory(custody_parent, self.required_owner_uid)?;
        ensure_private_directory(&self.roots.package_custody, self.required_owner_uid)?;

        // The agent owns `incoming`, so a path verified there cannot be handed to
        // a privileged process. Copy through one no-follow descriptor into a
        // fresh root-only namespace and make every subsequent consumer use it.
        let invocation = uuid::Uuid::new_v4().simple().to_string();
        let invocation_directory = self.roots.package_custody.join(invocation);
        fs::create_dir(&invocation_directory)?;
        fs::set_permissions(&invocation_directory, fs::Permissions::from_mode(0o700))?;
        require_exact_directory(&invocation_directory, self.required_owner_uid, 0o700)?;
        let candidate = invocation_directory.join(format!("{expected_digest}.deb"));
        let custody = CustodiedPackage::new(
            candidate,
            invocation_directory,
            self.roots.package_custody.clone(),
        );

        let mut source = OpenOptions::new()
            .read(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(incoming)
            .map_err(|_| OperationError::InvalidArtifact)?;
        let source_before = source
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        require_agent_artifact(&source_before, self.package_owner_uid)?;
        let mut destination = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(custody.path())?;
        let mut digest = Sha256::new();
        let mut consumed = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = source
                .read(&mut buffer)
                .map_err(|_| OperationError::InvalidArtifact)?;
            if count == 0 {
                break;
            }
            consumed = consumed
                .checked_add(count as u64)
                .filter(|value| *value <= MAX_ARTIFACT_BYTES)
                .ok_or(OperationError::InvalidArtifact)?;
            digest.update(&buffer[..count]);
            destination.write_all(&buffer[..count])?;
        }
        destination.sync_all()?;
        let source_after = source
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        let destination_metadata = destination.metadata()?;
        let observed_digest = hex::encode(digest.finalize());
        if artifact_identity(&source_before) != artifact_identity(&source_after)
            || consumed != source_before.len()
            || observed_digest != expected_digest
            || !safe_custody_file(&destination_metadata, self.required_owner_uid, consumed)
        {
            return Err(OperationError::InvalidArtifact);
        }
        let signature_bytes =
            hex::decode(detached_signature).map_err(|_| OperationError::InvalidArtifact)?;
        signature::UnparsedPublicKey::new(&signature::ED25519, self.release_public_key)
            .verify(
                &artifact_signing_bytes("deb", expected_digest)
                    .map_err(|_| OperationError::InvalidArtifact)?,
                &signature_bytes,
            )
            .map_err(|_| OperationError::InvalidArtifact)?;
        drop(destination);
        drop(source);
        sync_directory(&self.roots.package_custody)?;
        Ok(custody)
    }

    fn require_package_field(
        &self,
        package: &str,
        field: &str,
        expected: &str,
    ) -> Result<(), OperationError> {
        let result = self
            .runner
            .run(
                Path::new("/usr/bin/dpkg-deb"),
                &["--field".to_owned(), package.to_owned(), field.to_owned()],
            )
            .map_err(|_| OperationError::PackageMetadataInvalid)?;
        if !result.success || result.stdout != format!("{expected}\n").as_bytes() {
            return Err(OperationError::PackageMetadataInvalid);
        }
        Ok(())
    }

    fn restart_unit(&self, unit: &RestartUnit) -> Result<&'static str, OperationError> {
        let unit = match unit {
            RestartUnit::Agent => "vonk-forge-agent.service",
            RestartUnit::Helper => "vonk-forge-package-helper.service",
        };
        if matches!(unit, "vonk-forge-package-helper.service") {
            let result = self
                .runner
                .run(
                    Path::new("/usr/bin/systemd-run"),
                    &[
                        "--quiet".to_owned(),
                        "--collect".to_owned(),
                        "--unit=vonk-forge-helper-restart.service".to_owned(),
                        "--on-active=1s".to_owned(),
                        "/usr/bin/systemctl".to_owned(),
                        "restart".to_owned(),
                        unit.to_owned(),
                    ],
                )
                .map_err(|_| OperationError::CommandFailed)?;
            if !result.success {
                return Err(OperationError::CommandFailed);
            }
            return Ok(unit);
        }
        let result = self
            .runner
            .run(
                Path::new("/usr/bin/systemctl"),
                &["restart".to_owned(), unit.to_owned()],
            )
            .map_err(|_| OperationError::CommandFailed)?;
        if !result.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(unit)
    }

    fn schedule_reboot(&self, delay_seconds: u16) -> Result<(), OperationError> {
        if !(60..=3600).contains(&delay_seconds) {
            return Err(OperationError::InvalidOperation);
        }
        let result = self
            .runner
            .run(
                Path::new("/usr/bin/systemd-run"),
                &[
                    "--quiet".to_owned(),
                    "--collect".to_owned(),
                    "--unit=vonk-forge-reboot.service".to_owned(),
                    format!("--on-active={delay_seconds}s"),
                    "/usr/bin/systemctl".to_owned(),
                    "reboot".to_owned(),
                ],
            )
            .map_err(|_| OperationError::CommandFailed)?;
        if !result.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(())
    }

    fn execute_runtime_request(
        &self,
        action: &ContainerRuntimeAction,
        binding: RuntimeRequestGrantBinding<'_>,
        request_sha256: &str,
        observation_identity_sha256: Option<&str>,
        observation_node_id: Option<&str>,
    ) -> Result<RuntimeRequestOutcome, OperationError> {
        let request = self.read_runtime_request(request_sha256)?;
        let expected_action = match action {
            ContainerRuntimeAction::ImageImport => HostRuntimeAction::ImageImport,
            ContainerRuntimeAction::ImageInspect => HostRuntimeAction::ImageInspect,
            ContainerRuntimeAction::RunInspect => HostRuntimeAction::RunInspect,
            ContainerRuntimeAction::Start => HostRuntimeAction::Start,
            ContainerRuntimeAction::Stop => HostRuntimeAction::Stop,
        };
        if request.action != expected_action
            || &request.job_id != binding.job_id
            || &request.operation_id != binding.operation_id
            || request.attempt != binding.attempt
            || &request.fence != binding.fence
        {
            return Err(OperationError::InvalidOperation);
        }
        match (
            request.observation.as_ref(),
            observation_identity_sha256,
            observation_node_id,
        ) {
            (None, None, _) => {}
            (Some(binding), Some(expected), Some(node_id)) => {
                let mut identity = serde_json::to_value(binding)
                    .map_err(|_| OperationError::InvalidOperation)?
                    .as_object()
                    .cloned()
                    .ok_or(OperationError::InvalidOperation)?;
                identity.insert("schema_version".to_owned(), serde_json::json!(1));
                identity.insert("node_id".to_owned(), serde_json::json!(node_id));
                if hex_sha256(
                    &canonical_json(&identity).map_err(|_| OperationError::InvalidOperation)?,
                ) != expected
                {
                    return Err(OperationError::InvalidOperation);
                }
            }
            _ => return Err(OperationError::InvalidOperation),
        }
        match request.action {
            HostRuntimeAction::ImageImport => self
                .runtime_image_import(request.operation_id, &request.arguments)
                .map(|()| RuntimeRequestOutcome {
                    exit_code: None,
                    recipe_run_observation: None,
                }),
            HostRuntimeAction::ImageInspect => {
                self.runtime_image_inspect(&request.arguments)
                    .map(|()| RuntimeRequestOutcome {
                        exit_code: None,
                        recipe_run_observation: None,
                    })
            }
            HostRuntimeAction::RunInspect => {
                let running = self.runtime_run_inspect(&request.arguments)?;
                if request.observation.is_none() && !running {
                    return Err(OperationError::InvalidArtifact);
                }
                Ok(RuntimeRequestOutcome {
                    exit_code: None,
                    recipe_run_observation: request.observation.as_ref().map(|_| {
                        if running {
                            RecipeRunObservationOutcome::Running
                        } else {
                            RecipeRunObservationOutcome::NotRunning
                        }
                    }),
                })
            }
            HostRuntimeAction::Start => {
                self.runtime_start(&request.arguments)
                    .map(|exit_code| RuntimeRequestOutcome {
                        exit_code,
                        recipe_run_observation: None,
                    })
            }
            HostRuntimeAction::Stop => {
                if request.arguments.get(1).map(String::as_str) == Some("run") {
                    self.runtime_start(&request.arguments)
                        .map(|exit_code| RuntimeRequestOutcome {
                            exit_code,
                            recipe_run_observation: None,
                        })
                } else {
                    self.runtime_stop(&request.arguments)
                        .map(|()| RuntimeRequestOutcome {
                            exit_code: None,
                            recipe_run_observation: None,
                        })
                }
            }
        }
    }

    fn read_runtime_request(
        &self,
        request_sha256: &str,
    ) -> Result<HostRuntimeRequest, OperationError> {
        if !lower_hex(request_sha256, 64) {
            return Err(OperationError::InvalidOperation);
        }
        let path = self
            .roots
            .runtime_requests
            .join(format!("{request_sha256}.json"));
        let metadata = fs::symlink_metadata(&path).map_err(|_| OperationError::UnsafePath)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.len() == 0
            || metadata.len() > MAX_RUNTIME_REQUEST_BYTES
            || metadata.mode() & 0o077 != 0
            || self
                .runtime_request_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(OperationError::UnsafePath);
        }
        let mut file = File::open(&path).map_err(|_| OperationError::UnsafePath)?;
        let before = file.metadata().map_err(|_| OperationError::UnsafePath)?;
        let mut raw = Vec::new();
        Read::by_ref(&mut file)
            .take(MAX_RUNTIME_REQUEST_BYTES + 1)
            .read_to_end(&mut raw)
            .map_err(|_| OperationError::UnsafePath)?;
        let after = file.metadata().map_err(|_| OperationError::UnsafePath)?;
        if raw.len() as u64 > MAX_RUNTIME_REQUEST_BYTES
            || stable_identity(&before) != stable_identity(&after)
            || hex_sha256(&raw) != request_sha256
        {
            return Err(OperationError::UnsafePath);
        }
        let request: HostRuntimeRequest =
            parse_strict(&raw).map_err(|_| OperationError::InvalidOperation)?;
        request
            .validate()
            .map_err(|_| OperationError::InvalidOperation)?;
        if canonical_json(&request).map_err(|_| OperationError::InvalidOperation)? != raw {
            return Err(OperationError::InvalidOperation);
        }
        Ok(request)
    }

    fn runtime_image_import(
        &self,
        operation_id: uuid::Uuid,
        arguments: &[String],
    ) -> Result<(), OperationError> {
        let [archive, archive_sha256, archive_bytes, image_digest, image] = arguments else {
            return Err(OperationError::InvalidOperation);
        };
        let archive = Path::new(archive);
        let import_root = self.roots.agent_data.join("image-imports");
        let expected_parent = import_root.join(operation_id.to_string());
        let canonical_import_root =
            fs::canonicalize(&import_root).map_err(|_| OperationError::InvalidArtifact)?;
        let canonical_parent =
            fs::canonicalize(&expected_parent).map_err(|_| OperationError::InvalidArtifact)?;
        if !archive.is_absolute()
            || archive.parent() != Some(expected_parent.as_path())
            || canonical_parent.parent() != Some(canonical_import_root.as_path())
            || archive.file_name().and_then(|value| value.to_str()) != Some("image.docker.tar")
            || !lower_hex(archive_sha256, 64)
            || !valid_oci_digest(image_digest)
            || !valid_local_image(image)
        {
            return Err(OperationError::InvalidOperation);
        }
        let expected_bytes = archive_bytes
            .parse::<u64>()
            .ok()
            .filter(|value| (1..=MAX_RUNTIME_ARCHIVE_BYTES).contains(value))
            .ok_or(OperationError::InvalidOperation)?;
        self.verify_runtime_archive(archive, archive_sha256, expected_bytes)?;
        let loaded = self.run_docker(&[
            "load".to_owned(),
            "--input".to_owned(),
            archive.display().to_string(),
        ])?;
        if !loaded.success
            || !std::str::from_utf8(&loaded.stdout)
                .ok()
                .is_some_and(|value| {
                    value
                        .lines()
                        .any(|line| line == format!("Loaded image: {image}:latest"))
                })
        {
            return Err(OperationError::CommandFailed);
        }
        let inspected = self.inspect_runtime_image(image)?;
        if inspected.1 != "linux"
            || inspected.2 != "arm64"
            || inspected.3 != "v1"
            || !numeric_non_root_user(&inspected.4)
        {
            return Err(OperationError::InvalidArtifact);
        }
        self.write_image_receipt(image_digest, image, &inspected.0)
    }

    fn runtime_image_inspect(&self, arguments: &[String]) -> Result<(), OperationError> {
        let [image_reference, image_digest, user] = arguments else {
            return Err(OperationError::InvalidOperation);
        };
        let (image, embedded_digest) = parse_local_image_reference(image_reference)?;
        if &embedded_digest != image_digest || !numeric_non_root_user(user) {
            return Err(OperationError::InvalidOperation);
        }
        let inspected = self.inspect_runtime_image(&image)?;
        if inspected.1 != "linux"
            || inspected.2 != "arm64"
            || inspected.3 != "v1"
            || inspected.4 != *user
        {
            return Err(OperationError::InvalidArtifact);
        }
        self.require_image_receipt(image_digest, &image, &inspected.0)
    }

    fn runtime_start(&self, arguments: &[String]) -> Result<Option<i32>, OperationError> {
        let (image_digest, docker) = arguments
            .split_first()
            .ok_or(OperationError::InvalidOperation)?;
        let validated = validate_docker_run(docker, &self.roots, self.runtime_request_owner_uid)?;
        if image_digest != &validated.image_digest {
            return Err(OperationError::InvalidOperation);
        }
        // A one-shot START remains registered from validation through attached container exit.
        // Cancellation STOPs can therefore distinguish "not created yet" from "already gone"
        // and retry until this exact job can no longer start late.
        let _active_job = validated
            .job_timeout_seconds
            .map(|_| self.job_cancellation.begin(&validated.run_id))
            .transpose()?;
        self.require_host_endpoint_firewall(&validated)?;
        let inspected = self.inspect_runtime_image(&validated.image)?;
        self.require_image_receipt(image_digest, &validated.image, &inspected.0)?;
        let semantic_digest = hex_sha256(
            &canonical_json(&validated.arguments).map_err(|_| OperationError::InvalidOperation)?,
        );
        if validated.detached {
            let existing = self.run_docker(&[
                "container".to_owned(),
                "inspect".to_owned(),
                "--format".to_owned(),
                "{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.runtime-request-sha256\"}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}".to_owned(),
                format!("vonk-{}", validated.run_id),
            ])?;
            if existing.success {
                let expected = format!("true\t{semantic_digest}\ttrue\t{}", validated.run_id);
                if std::str::from_utf8(&existing.stdout).ok().map(str::trim)
                    != Some(expected.as_str())
                {
                    return Err(OperationError::InvalidArtifact);
                }
                return Ok(None);
            }
        }
        self.prepare_runtime_access(&validated)?;
        let mut compiled = validated.arguments.clone();
        if validated.detached || validated.job_timeout_seconds.is_some() {
            compiled.splice(
                validated.image_index..validated.image_index,
                [
                    "--label".to_owned(),
                    format!("ai.vonkforge.runtime-request-sha256={semantic_digest}"),
                    "--label".to_owned(),
                    "ai.vonkforge.managed=true".to_owned(),
                    "--label".to_owned(),
                    format!("ai.vonkforge.run-id={}", validated.run_id),
                ],
            );
        }
        let output = if let Some(timeout) = validated.job_timeout_seconds {
            match self.runner.run_with_timeout(
                Path::new("/usr/bin/docker"),
                &compiled,
                Duration::from_secs(timeout.into()),
            ) {
                Ok(output) => output,
                Err(_) => {
                    return finish_timed_out_job(
                        self.runtime_stop(&[validated.run_id.clone(), "30".to_owned()]),
                    );
                }
            }
        } else {
            self.run_docker(&compiled)?
        };
        let identifier = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .unwrap_or("");
        if validated.detached && (!output.success || !lower_hex(identifier, 64)) {
            return Err(OperationError::CommandFailed);
        }
        if validated.job_timeout_seconds.is_some() {
            self.runtime_stop(&[validated.run_id.clone(), "30".to_owned()])
                .map_err(|_| OperationError::StopUncertain)?;
        }
        Ok(validated
            .job_timeout_seconds
            .map(|_| bounded_container_exit_code(&output)))
    }

    fn require_host_endpoint_firewall(
        &self,
        run: &ValidatedDockerRun,
    ) -> Result<(), OperationError> {
        let Some(port) = run.host_endpoint_port else {
            return Ok(());
        };
        let output = self
            .runner
            .run(
                Path::new(DOCKER_FIREWALL),
                &[
                    "--config".to_owned(),
                    DOCKER_FIREWALL_CONFIG.to_owned(),
                    "check-host-port".to_owned(),
                    port.to_string(),
                ],
            )
            .map_err(|_| OperationError::CommandFailed)?;
        if !output.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(())
    }

    fn runtime_run_inspect(&self, arguments: &[String]) -> Result<bool, OperationError> {
        let (image_digest, docker) = arguments
            .split_first()
            .ok_or(OperationError::InvalidOperation)?;
        let validated = validate_docker_run(docker, &self.roots, self.runtime_request_owner_uid)?;
        if image_digest != &validated.image_digest || !validated.detached {
            return Err(OperationError::InvalidOperation);
        }
        self.require_host_endpoint_firewall(&validated)?;
        let inspected = self.inspect_runtime_image(&validated.image)?;
        self.require_image_receipt(image_digest, &validated.image, &inspected.0)?;
        let semantic_digest = hex_sha256(
            &canonical_json(&validated.arguments).map_err(|_| OperationError::InvalidOperation)?,
        );
        let existing = self.run_docker(&[
            "container".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.runtime-request-sha256\"}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}".to_owned(),
            format!("vonk-{}", validated.run_id),
        ])?;
        let expected = format!("true\t{semantic_digest}\ttrue\t{}", validated.run_id);
        Ok(existing.success
            && std::str::from_utf8(&existing.stdout).ok().map(str::trim) == Some(expected.as_str()))
    }

    fn prepare_runtime_access(&self, run: &ValidatedDockerRun) -> Result<(), OperationError> {
        for path in run.models.iter().chain(run.inputs.iter()) {
            let output = self
                .runner
                .run(
                    Path::new("/usr/bin/setfacl"),
                    &[
                        "-R".to_owned(),
                        "-m".to_owned(),
                        format!("u:{}:rX", run.uid),
                        path.display().to_string(),
                    ],
                )
                .map_err(|_| OperationError::CommandFailed)?;
            if !output.success {
                return Err(OperationError::CommandFailed);
            }
        }
        for (path, access) in [(&run.outputs, "rwx"), (&run.runtime_contract, "r")] {
            let output = self
                .runner
                .run(
                    Path::new("/usr/bin/setfacl"),
                    &[
                        "-m".to_owned(),
                        format!("u:{}:{access}", run.uid),
                        path.display().to_string(),
                    ],
                )
                .map_err(|_| OperationError::CommandFailed)?;
            if !output.success {
                return Err(OperationError::CommandFailed);
            }
        }
        Ok(())
    }

    fn runtime_stop(&self, arguments: &[String]) -> Result<(), OperationError> {
        self.runtime_stop_until(arguments, Instant::now() + Duration::from_secs(30))
    }

    fn runtime_stop_until(
        &self,
        arguments: &[String],
        deadline: Instant,
    ) -> Result<(), OperationError> {
        let (run_id, timeout, cancel_job) = parse_runtime_stop(arguments)?;
        if cancel_job {
            self.job_cancellation.cancel(run_id)?;
        }
        loop {
            self.runtime_stop_once(run_id, timeout)?;
            if !cancel_job || !self.job_cancellation.is_active(run_id)? {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(OperationError::StopUncertain);
            }
            thread::sleep(Duration::from_millis(50));
        }
    }

    fn runtime_stop_once(&self, run_id: &str, timeout: u16) -> Result<(), OperationError> {
        let name = format!("vonk-{run_id}");
        let existing = self.run_docker_with_timeout(
            &[
            "container".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}".to_owned(),
            name.clone(),
            ],
            Duration::from_secs(15),
        )?;
        if !existing.success {
            let daemon = self.run_docker_with_timeout(
                &[
                    "version".to_owned(),
                    "--format".to_owned(),
                    "{{.Server.Version}}".to_owned(),
                ],
                Duration::from_secs(15),
            )?;
            return if daemon.success {
                Ok(())
            } else {
                Err(OperationError::CommandFailed)
            };
        }
        let expected = format!("true\t{run_id}");
        if std::str::from_utf8(&existing.stdout).ok().map(str::trim) != Some(expected.as_str()) {
            return Err(OperationError::InvalidArtifact);
        }
        let stopped = self.run_docker_with_timeout(
            &[
                "stop".to_owned(),
                "--timeout".to_owned(),
                timeout.to_string(),
                name.clone(),
            ],
            Duration::from_secs(u64::from(timeout) + 15),
        )?;
        if !stopped.success {
            return Err(OperationError::CommandFailed);
        }
        let removed =
            self.run_docker_with_timeout(&["rm".to_owned(), name], Duration::from_secs(15))?;
        if !removed.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(())
    }

    fn run_docker(&self, arguments: &[String]) -> Result<CommandOutput, OperationError> {
        self.runner
            .run(Path::new("/usr/bin/docker"), arguments)
            .map_err(|_| OperationError::CommandFailed)
    }

    fn run_docker_with_timeout(
        &self,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<CommandOutput, OperationError> {
        self.runner
            .run_with_timeout(Path::new("/usr/bin/docker"), arguments, timeout)
            .map_err(|_| OperationError::CommandFailed)
    }

    fn inspect_runtime_image(
        &self,
        image: &str,
    ) -> Result<(String, String, String, String, String), OperationError> {
        let output = self.run_docker(&[
            "image".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{.Id}}\t{{.Os}}\t{{.Architecture}}\t{{index .Config.Labels \"ai.vonkforge.runtime-interface\"}}\t{{.Config.User}}".to_owned(),
            image.to_owned(),
        ])?;
        let fields = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .map(|value| value.split('\t').map(str::to_owned).collect::<Vec<_>>())
            .unwrap_or_default();
        if !output.success || fields.len() != 5 || !valid_oci_digest(&fields[0]) {
            return Err(OperationError::InvalidArtifact);
        }
        Ok((
            fields[0].clone(),
            fields[1].clone(),
            fields[2].clone(),
            fields[3].clone(),
            fields[4].clone(),
        ))
    }

    fn write_image_receipt(
        &self,
        image_digest: &str,
        image: &str,
        image_id: &str,
    ) -> Result<(), OperationError> {
        fs::create_dir_all(&self.roots.runtime_image_receipts)?;
        fs::set_permissions(
            &self.roots.runtime_image_receipts,
            fs::Permissions::from_mode(0o700),
        )?;
        let path = self
            .roots
            .runtime_image_receipts
            .join(image_digest.trim_start_matches("sha256:"));
        let body = format!("{image}\n{image_id}\n");
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                file.write_all(body.as_bytes())?;
                file.sync_all()?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                if fs::read_to_string(&path)? != body {
                    return Err(OperationError::InvalidArtifact);
                }
            }
            Err(error) => return Err(error.into()),
        }
        sync_directory(&self.roots.runtime_image_receipts)
    }

    fn require_image_receipt(
        &self,
        image_digest: &str,
        image: &str,
        image_id: &str,
    ) -> Result<(), OperationError> {
        if !valid_oci_digest(image_digest) {
            return Err(OperationError::InvalidOperation);
        }
        let path = self
            .roots
            .runtime_image_receipts
            .join(image_digest.trim_start_matches("sha256:"));
        let expected = format!("{image}\n{image_id}\n");
        let metadata = fs::symlink_metadata(&path).map_err(|_| OperationError::InvalidArtifact)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
            || metadata.nlink() != 1
            || metadata.mode() & 0o022 != 0
            || metadata.len() > 2048
            || fs::read_to_string(path).map_err(|_| OperationError::InvalidArtifact)? != expected
        {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(())
    }

    fn verify_runtime_archive(
        &self,
        path: &Path,
        expected_digest: &str,
        expected_bytes: u64,
    ) -> Result<(), OperationError> {
        let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::InvalidArtifact)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.len() != expected_bytes
            || metadata.mode() & 0o077 != 0
            || self
                .runtime_request_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(OperationError::InvalidArtifact);
        }
        let mut file = File::open(path).map_err(|_| OperationError::InvalidArtifact)?;
        let before = file
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        let mut digest = Sha256::new();
        let mut consumed = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = file
                .read(&mut buffer)
                .map_err(|_| OperationError::InvalidArtifact)?;
            if count == 0 {
                break;
            }
            consumed += count as u64;
            digest.update(&buffer[..count]);
        }
        let after = file
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        if consumed != expected_bytes
            || stable_identity(&before) != stable_identity(&after)
            || hex::encode(digest.finalize()) != expected_digest
        {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(())
    }

    fn require_directory(&self, path: &Path) -> Result<(), OperationError> {
        require_safe_directory(path, self.required_owner_uid)
    }
}

fn bounded_container_exit_code(output: &CommandOutput) -> i32 {
    output
        .exit_code
        .filter(|code| (0..=255).contains(code))
        .unwrap_or(1)
}

struct ValidatedDockerRun {
    image: String,
    image_digest: String,
    arguments: Vec<String>,
    detached: bool,
    image_index: usize,
    run_id: String,
    uid: u32,
    models: Vec<PathBuf>,
    inputs: Option<PathBuf>,
    outputs: PathBuf,
    runtime_contract: PathBuf,
    host_endpoint_port: Option<u16>,
    job_timeout_seconds: Option<u16>,
}

fn validate_docker_run(
    arguments: &[String],
    roots: &ManagedRoots,
    agent_data_owner_uid: Option<u32>,
) -> Result<ValidatedDockerRun, OperationError> {
    if arguments.first().map(String::as_str) != Some("run") {
        return Err(OperationError::InvalidOperation);
    }
    let mut index = 1;
    let mut detach = false;
    let mut remove = false;
    let mut name: Option<String> = None;
    let mut restart = false;
    let mut read_only = false;
    let mut temporary_filesystem = false;
    let mut init = false;
    let mut pull_never = false;
    let mut local_logging = false;
    let mut log_max_size = false;
    let mut log_max_file = false;
    let mut cap_drop = false;
    let mut no_new_privileges = false;
    let mut network: Option<&str> = None;
    let mut ipc_host = false;
    let mut infiniband = false;
    let mut memlock = false;
    let mut stack = false;
    let mut pids = false;
    let mut memory: Option<u64> = None;
    let mut memory_swap: Option<u64> = None;
    let mut shm_size: Option<u64> = None;
    let mut user: Option<(u32, Option<u32>)> = None;
    let mut publishes = 0_usize;
    let mut environments = 0_usize;
    let mut listen_port = None;
    let mut job_timeout_seconds = None;
    let mut gpu = false;
    let mut models = Vec::new();
    let mut model_sources = BTreeSet::new();
    let mut model_targets = BTreeSet::new();
    let mut outputs = None;
    let mut inputs = None;
    let mut runtime_contract = None;

    while index < arguments.len() {
        let flag = &arguments[index];
        if !flag.starts_with('-') {
            break;
        }
        match flag.as_str() {
            "--detach" if !detach => detach = true,
            "--rm" if !remove => remove = true,
            "--read-only" if !read_only => read_only = true,
            "--tmpfs" if !temporary_filesystem => {
                index += 1;
                if arguments.get(index).map(String::as_str)
                    != Some("/tmp:rw,nosuid,nodev,mode=1777,size=1073741824")
                {
                    return Err(OperationError::InvalidOperation);
                }
                temporary_filesystem = true;
            }
            "--init" if !init => init = true,
            "--pull" if !pull_never => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("never") {
                    return Err(OperationError::InvalidOperation);
                }
                pull_never = true;
            }
            "--log-driver" if !local_logging => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("local") {
                    return Err(OperationError::InvalidOperation);
                }
                local_logging = true;
            }
            "--log-opt" if !log_max_size || !log_max_file => {
                index += 1;
                match arguments.get(index).map(String::as_str) {
                    Some("max-size=10m") if !log_max_size => log_max_size = true,
                    Some("max-file=3") if !log_max_file => log_max_file = true,
                    _ => return Err(OperationError::InvalidOperation),
                }
            }
            "--cap-drop=ALL" if !cap_drop => cap_drop = true,
            "--security-opt=no-new-privileges" if !no_new_privileges => no_new_privileges = true,
            "--name" if name.is_none() => {
                index += 1;
                let value = arguments
                    .get(index)
                    .ok_or(OperationError::InvalidOperation)?;
                let run_id = value
                    .strip_prefix("vonk-")
                    .ok_or(OperationError::InvalidOperation)?;
                if uuid::Uuid::parse_str(run_id)
                    .ok()
                    .map(|value| value.to_string())
                    != Some(run_id.to_owned())
                {
                    return Err(OperationError::InvalidOperation);
                }
                name = Some(run_id.to_owned());
            }
            "--restart" if !restart => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("no") {
                    return Err(OperationError::InvalidOperation);
                }
                restart = true;
            }
            "--network" if network.is_none() => {
                index += 1;
                network = match arguments.get(index).map(String::as_str) {
                    Some("bridge") => Some("bridge"),
                    Some("host") => Some("host"),
                    _ => return Err(OperationError::InvalidOperation),
                };
            }
            "--ipc" if !ipc_host => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("host") {
                    return Err(OperationError::InvalidOperation);
                }
                ipc_host = true;
            }
            "--device" if !infiniband || !gpu => {
                index += 1;
                match arguments.get(index).map(String::as_str) {
                    Some("/dev/infiniband:/dev/infiniband") if !infiniband => {
                        infiniband = true;
                    }
                    Some("nvidia.com/gpu=all") if !gpu => gpu = true,
                    _ => return Err(OperationError::InvalidOperation),
                }
            }
            "--ulimit" if !memlock || !stack => {
                index += 1;
                match arguments.get(index).map(String::as_str) {
                    Some("memlock=-1:-1") if !memlock => memlock = true,
                    Some("stack=67108864:67108864") if !stack => stack = true,
                    _ => return Err(OperationError::InvalidOperation),
                }
            }
            "--pids-limit" if !pids => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("4096") {
                    return Err(OperationError::InvalidOperation);
                }
                pids = true;
            }
            "--memory" if memory.is_none() => {
                index += 1;
                let value = arguments
                    .get(index)
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|value| (64 * 1024 * 1024..=128_000_000_000).contains(value))
                    .ok_or(OperationError::InvalidOperation)?;
                memory = Some(value);
            }
            "--memory-swap" if memory_swap.is_none() => {
                index += 1;
                memory_swap = arguments
                    .get(index)
                    .and_then(|value| value.parse::<u64>().ok());
            }
            "--shm-size" if shm_size.is_none() => {
                index += 1;
                shm_size = arguments
                    .get(index)
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|value| (64 * 1024 * 1024..=16 * 1024 * 1024 * 1024).contains(value));
            }
            "--user" if user.is_none() => {
                index += 1;
                user = Some(parse_numeric_user(
                    arguments
                        .get(index)
                        .ok_or(OperationError::InvalidOperation)?,
                )?);
            }
            "--publish" if publishes < 2 => {
                index += 1;
                if !valid_publication(
                    arguments
                        .get(index)
                        .ok_or(OperationError::InvalidOperation)?,
                ) {
                    return Err(OperationError::InvalidOperation);
                }
                publishes += 1;
            }
            "--env" if environments < 160 => {
                index += 1;
                let value = arguments
                    .get(index)
                    .ok_or(OperationError::InvalidOperation)?;
                if !valid_environment(value) {
                    return Err(OperationError::InvalidOperation);
                }
                if let Some(value) = value.strip_prefix("VONK_LISTEN_PORT=") {
                    let parsed = value
                        .parse::<u16>()
                        .ok()
                        .filter(|port| (1024..=65535).contains(port))
                        .ok_or(OperationError::InvalidOperation)?;
                    if listen_port.replace(parsed).is_some() {
                        return Err(OperationError::InvalidOperation);
                    }
                }
                if let Some(value) = value.strip_prefix("VONK_JOB_TIMEOUT_SECONDS=") {
                    let parsed = value
                        .parse::<u16>()
                        .ok()
                        .filter(|seconds| (1..=3600).contains(seconds))
                        .ok_or(OperationError::InvalidOperation)?;
                    if job_timeout_seconds.replace(parsed).is_some() {
                        return Err(OperationError::InvalidOperation);
                    }
                }
                environments += 1;
            }
            "--mount" => {
                index += 1;
                let value = arguments
                    .get(index)
                    .ok_or(OperationError::InvalidOperation)?;
                let (source, target, readonly) = parse_mount(value)?;
                if readonly && valid_model_mount(&source, target, roots) {
                    if !model_sources.insert(source.clone())
                        || !model_targets.insert(target.to_owned())
                    {
                        return Err(OperationError::InvalidOperation);
                    }
                    models.push(source);
                } else if target == "/outputs"
                    && !readonly
                    && source.file_name().and_then(|value| value.to_str()) == Some("outputs")
                    && source.parent().and_then(Path::parent)
                        == Some(roots.agent_data.join("runs").as_path())
                    && outputs.is_none()
                {
                    outputs = Some(source);
                } else if target == "/inputs"
                    && readonly
                    && source.file_name().and_then(|value| value.to_str()) == Some("inputs")
                    && source.parent().and_then(Path::parent)
                        == Some(roots.agent_data.join("runs").as_path())
                    && inputs.is_none()
                {
                    inputs = Some(source);
                } else if target == "/run/vonk/runtime.json"
                    && readonly
                    && source.starts_with(roots.agent_data.join("run-metadata"))
                    && source.file_name().and_then(|value| value.to_str()) == Some("runtime.json")
                    && runtime_contract.is_none()
                {
                    runtime_contract = Some(source);
                } else {
                    return Err(OperationError::InvalidOperation);
                }
            }
            _ => return Err(OperationError::InvalidOperation),
        }
        index += 1;
    }
    let image_reference = arguments
        .get(index)
        .cloned()
        .ok_or(OperationError::InvalidOperation)?;
    let (image, embedded_digest) = parse_local_image_reference(&image_reference)?;
    let (uid, _gid) = user.ok_or(OperationError::InvalidOperation)?;
    let outputs = outputs.ok_or(OperationError::InvalidOperation)?;
    let runtime_contract = runtime_contract.ok_or(OperationError::InvalidOperation)?;
    let state_run_id = outputs
        .parent()
        .and_then(Path::file_name)
        .and_then(|value| value.to_str())
        .ok_or(OperationError::InvalidOperation)?;
    let named_run_id = name.as_deref();
    if !((detach && !remove && restart && named_run_id == Some(state_run_id))
        || (!detach && remove && !restart && named_run_id.is_none())
        || (!detach
            && !remove
            && restart
            && named_run_id == Some(state_run_id)
            && job_timeout_seconds.is_some()))
        || !read_only
        || !temporary_filesystem
        || !init
        || !pull_never
        || !local_logging
        || !log_max_size
        || !log_max_file
        || !cap_drop
        || !no_new_privileges
        || network.is_none()
        || !pids
        || memory.is_none()
        || memory_swap != memory
        || shm_size.is_none_or(|value| value > memory.unwrap_or_default())
        || (detach && network == Some("bridge") && publishes == 0)
        || (network == Some("host") && publishes != 0)
        || (network == Some("host") && listen_port.is_none())
        || (!detach && publishes != 0)
        || (detach && (inputs.is_some() || job_timeout_seconds.is_some()))
        || (!detach && (inputs.is_none() || job_timeout_seconds.is_none()))
        || (!detach && (listen_port.is_some() || network != Some("bridge")))
        || (network == Some("host") && (!ipc_host || !infiniband || !memlock || !stack || !gpu))
        || (network == Some("bridge") && (ipc_host || infiniband || memlock || stack))
        || environments == 0
        || outputs.parent().and_then(Path::parent) != Some(roots.agent_data.join("runs").as_path())
        || runtime_contract.parent().and_then(Path::parent)
            != Some(roots.agent_data.join("run-metadata").as_path())
        || runtime_contract
            .parent()
            .and_then(Path::file_name)
            .and_then(|value| value.to_str())
            != Some(state_run_id)
        || inputs.as_ref().is_some_and(|inputs| {
            inputs
                .parent()
                .and_then(Path::file_name)
                .and_then(|value| value.to_str())
                != Some(state_run_id)
        })
    {
        return Err(OperationError::InvalidOperation);
    }
    if models.is_empty()
        || models.len() > 16
        || models.len() > 1 && model_targets.contains("/models")
    {
        return Err(OperationError::InvalidOperation);
    }
    let canonical_model_root = canonical_model_root(roots, agent_data_owner_uid)?;
    for path in &models {
        require_safe_directory(path, agent_data_owner_uid)?;
        let canonical = path
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        if canonical.parent() != Some(canonical_model_root.as_path()) {
            return Err(OperationError::UnsafePath);
        }
    }
    for path in inputs.iter().chain([&outputs, &runtime_contract]) {
        let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
        if metadata.file_type().is_symlink()
            || !(metadata.is_dir() || metadata.is_file())
            || roots.agent_data.canonicalize().ok().is_none_or(|root| {
                path.canonicalize()
                    .ok()
                    .is_none_or(|canonical| !canonical.starts_with(root))
            })
        {
            return Err(OperationError::UnsafePath);
        }
    }
    let agent_data = roots
        .agent_data
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let runs = roots.agent_data.join("runs");
    let run_root = runs.join(state_run_id);
    let metadata_root = roots.agent_data.join("run-metadata");
    let run_metadata = metadata_root.join(state_run_id);
    for path in [&runs, &run_root, &metadata_root, &run_metadata] {
        require_safe_directory(path, agent_data_owner_uid)?;
    }
    let canonical_runs = runs
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_run = run_root
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_metadata_root = metadata_root
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_run_metadata = run_metadata
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    if canonical_runs.parent() != Some(agent_data.as_path())
        || canonical_run.parent() != Some(canonical_runs.as_path())
        || canonical_metadata_root.parent() != Some(agent_data.as_path())
        || canonical_run_metadata.parent() != Some(canonical_metadata_root.as_path())
        || outputs
            .canonicalize()
            .ok()
            .and_then(|path| path.parent().map(Path::to_path_buf))
            != Some(canonical_run.clone())
        || inputs.as_ref().is_some_and(|path| {
            path.canonicalize()
                .ok()
                .and_then(|path| path.parent().map(Path::to_path_buf))
                != Some(canonical_run.clone())
        })
        || runtime_contract
            .canonicalize()
            .ok()
            .and_then(|path| path.parent().map(Path::to_path_buf))
            != Some(canonical_run_metadata)
    {
        return Err(OperationError::UnsafePath);
    }
    let mut compiled_arguments = arguments.to_vec();
    compiled_arguments[index] = image.clone();
    Ok(ValidatedDockerRun {
        image,
        image_digest: embedded_digest,
        arguments: compiled_arguments,
        detached: detach,
        image_index: index,
        run_id: state_run_id.to_owned(),
        uid,
        models,
        inputs,
        outputs,
        runtime_contract,
        host_endpoint_port: (network == Some("host")).then_some(listen_port).flatten(),
        job_timeout_seconds,
    })
}

fn parse_mount(value: &str) -> Result<(PathBuf, &str, bool), OperationError> {
    let fields = value.split(',').collect::<Vec<_>>();
    if !(fields.len() == 3 || fields.len() == 4) || fields[0] != "type=bind" {
        return Err(OperationError::InvalidOperation);
    }
    let source = fields[1]
        .strip_prefix("src=")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .ok_or(OperationError::InvalidOperation)?;
    let target = fields[2]
        .strip_prefix("dst=")
        .ok_or(OperationError::InvalidOperation)?;
    let readonly = fields.get(3).is_some_and(|value| *value == "readonly");
    if fields.len() == 4 && !readonly {
        return Err(OperationError::InvalidOperation);
    }
    Ok((source, target, readonly))
}

fn parse_runtime_stop(arguments: &[String]) -> Result<(&str, u16, bool), OperationError> {
    let (run_id, timeout, cancel_job) = match arguments {
        [run_id, timeout] => (run_id.as_str(), timeout.as_str(), false),
        [run_id, timeout, marker] if marker == "job-cancel" => {
            (run_id.as_str(), timeout.as_str(), true)
        }
        _ => return Err(OperationError::InvalidOperation),
    };
    let timeout = timeout
        .parse::<u16>()
        .ok()
        .filter(|value| (1..=600).contains(value))
        .ok_or(OperationError::InvalidOperation)?;
    if uuid::Uuid::parse_str(run_id)
        .ok()
        .map(|value| value.to_string())
        .as_deref()
        != Some(run_id)
    {
        return Err(OperationError::InvalidOperation);
    }
    Ok((run_id, timeout, cancel_job))
}

fn finish_timed_out_job(
    stop_result: Result<(), OperationError>,
) -> Result<Option<i32>, OperationError> {
    stop_result
        .map(|()| Some(124))
        .map_err(|_| OperationError::StopUncertain)
}

fn valid_model_mount(source: &Path, target: &str, roots: &ManagedRoots) -> bool {
    let model_root = roots.agent_data.join("models").join("sha256");
    let Some(key) = source.file_name().and_then(|value| value.to_str()) else {
        return false;
    };
    if !lower_hex(key, 64) || source != model_root.join(key) {
        return false;
    }
    if target == "/models" {
        return true;
    }
    target
        .strip_prefix("/models/")
        .is_some_and(valid_artifact_id)
}

fn canonical_model_root(
    roots: &ManagedRoots,
    agent_data_owner_uid: Option<u32>,
) -> Result<PathBuf, OperationError> {
    let agent_data = &roots.agent_data;
    let models = agent_data.join("models");
    let model_root = models.join("sha256");
    for path in [agent_data, &models, &model_root] {
        require_safe_directory(path, agent_data_owner_uid)?;
    }
    let canonical_agent_data = agent_data
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_models = models
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_model_root = model_root
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    if canonical_models.parent() != Some(canonical_agent_data.as_path())
        || canonical_model_root.parent() != Some(canonical_models.as_path())
    {
        return Err(OperationError::UnsafePath);
    }
    Ok(canonical_model_root)
}

fn require_safe_directory(
    path: &Path,
    required_owner_uid: Option<u32>,
) -> Result<(), OperationError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.mode() & 0o022 != 0
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
    {
        return Err(OperationError::UnsafePath);
    }
    Ok(())
}

fn ensure_private_directory(
    path: &Path,
    required_owner_uid: Option<u32>,
) -> Result<(), OperationError> {
    match fs::symlink_metadata(path) {
        Ok(_) => require_exact_directory(path, required_owner_uid, 0o700).map(|_| ()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir(path)?;
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
            require_exact_directory(path, required_owner_uid, 0o700).map(|_| ())
        }
        Err(error) => Err(error.into()),
    }
}

fn require_exact_directory(
    path: &Path,
    required_owner_uid: Option<u32>,
    expected_mode: u32,
) -> Result<(u32, u32), OperationError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.mode() & 0o777 != expected_mode
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
        || required_owner_uid == Some(0) && metadata.gid() != 0
    {
        return Err(OperationError::UnsafePath);
    }
    Ok((metadata.uid(), metadata.gid()))
}

fn require_agent_artifact(
    metadata: &fs::Metadata,
    required_owner_uid: Option<u32>,
) -> Result<(), OperationError> {
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.len() == 0
        || metadata.len() > MAX_ARTIFACT_BYTES
        || metadata.mode() & 0o777 != 0o600
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
    {
        return Err(OperationError::InvalidArtifact);
    }
    Ok(())
}

fn safe_custody_file(
    metadata: &fs::Metadata,
    required_owner_uid: Option<u32>,
    expected_bytes: u64,
) -> bool {
    metadata.is_file()
        && metadata.nlink() == 1
        && metadata.len() == expected_bytes
        && metadata.mode() & 0o777 == 0o600
        && required_owner_uid.is_none_or(|uid| metadata.uid() == uid)
        && (required_owner_uid != Some(0) || metadata.gid() == 0)
}

fn artifact_identity(metadata: &fs::Metadata) -> ArtifactIdentity {
    ArtifactIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
        uid: metadata.uid(),
        gid: metadata.gid(),
        mode: metadata.mode(),
        links: metadata.nlink(),
        bytes: metadata.len(),
        modified_seconds: metadata.mtime(),
        modified_nanoseconds: metadata.mtime_nsec(),
        changed_seconds: metadata.ctime(),
        changed_nanoseconds: metadata.ctime_nsec(),
    }
}

fn valid_artifact_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && !matches!(value, "." | "..")
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
}

fn parse_numeric_user(value: &str) -> Result<(u32, Option<u32>), OperationError> {
    if !numeric_non_root_user(value) {
        return Err(OperationError::InvalidOperation);
    }
    let mut parts = value.split(':');
    let uid = parts
        .next()
        .and_then(|value| value.parse::<u32>().ok())
        .filter(|value| *value != 0)
        .ok_or(OperationError::InvalidOperation)?;
    let gid = parts.next().and_then(|value| value.parse::<u32>().ok());
    Ok((uid, gid))
}

fn valid_publication(value: &str) -> bool {
    let (address, ports) = if let Some(value) = value.strip_prefix('[') {
        let Some((address, ports)) = value.split_once("]:") else {
            return false;
        };
        (address, ports)
    } else {
        let Some((address, ports)) = value.split_once(':') else {
            return false;
        };
        (address, ports)
    };
    let Ok(std::net::IpAddr::V4(address)) = address.parse::<std::net::IpAddr>() else {
        return false;
    };
    if address.is_unspecified()
        || address.is_loopback()
        || address.is_multicast()
        || address.is_link_local()
    {
        return false;
    }
    let Some((host, container)) = ports.split_once(':') else {
        return false;
    };
    if container.contains(':') {
        return false;
    }
    [host, container].iter().all(|part| {
        part.parse::<u16>()
            .is_ok_and(|port| (1024..=65535).contains(&port))
    })
}

fn valid_environment(value: &str) -> bool {
    let Some((name, _)) = value.split_once('=') else {
        return false;
    };
    !name.is_empty()
        && name.len() <= 128
        && name.bytes().enumerate().all(|(index, byte)| {
            if index == 0 {
                byte.is_ascii_uppercase()
            } else {
                byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_'
            }
        })
}

fn valid_local_image(value: &str) -> bool {
    value
        .strip_prefix("localhost/vonk/recipe-build-")
        .and_then(|value| uuid::Uuid::parse_str(value).ok().map(|id| (value, id)))
        .is_some_and(|(value, id)| id.to_string() == value)
}

fn parse_local_image_reference(value: &str) -> Result<(String, String), OperationError> {
    let (image, digest) = value
        .split_once('@')
        .ok_or(OperationError::InvalidOperation)?;
    if !valid_local_image(image) || !valid_oci_digest(digest) {
        return Err(OperationError::InvalidOperation);
    }
    Ok((image.to_owned(), digest.to_owned()))
}

fn valid_oci_digest(value: &str) -> bool {
    value
        .strip_prefix("sha256:")
        .is_some_and(|value| lower_hex(value, 64))
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn numeric_non_root_user(value: &str) -> bool {
    let mut parts = value.split(':');
    let valid = |part: &str| {
        !part.is_empty() && !part.starts_with('0') && part.bytes().all(|byte| byte.is_ascii_digit())
    };
    valid(parts.next().unwrap_or_default())
        && parts.next().is_none_or(valid)
        && parts.next().is_none()
}

fn stable_identity(metadata: &fs::Metadata) -> (u64, u64, u64, i64, i64) {
    (
        metadata.dev(),
        metadata.ino(),
        metadata.len(),
        metadata.mtime(),
        metadata.ctime(),
    )
}

fn sync_directory(path: &Path) -> Result<(), OperationError> {
    OpenOptions::new().read(true).open(path)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
    use std::path::{Path, PathBuf};
    use std::time::Instant;

    use tempfile::TempDir;

    use super::{
        CommandOutput, CommandRunner, JobCancellationFence, ManagedRoots, OperationError,
        OperationExecutor, bounded_container_exit_code, finish_timed_out_job, parse_runtime_stop,
        valid_publication, validate_docker_run,
    };

    const RUN_ID: &str = "40000000-0000-4000-8000-000000000004";

    #[derive(Clone, Copy)]
    struct MissingContainerRunner;

    impl CommandRunner for MissingContainerRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new("/usr/bin/docker"));
            let daemon_probe = arguments.first().map(String::as_str) == Some("version");
            Ok(CommandOutput {
                success: daemon_probe,
                stdout: Vec::new(),
                exit_code: Some(if daemon_probe { 0 } else { 1 }),
            })
        }
    }

    #[test]
    fn job_cancellation_fence_blocks_late_start_and_tracks_active_start() {
        let fence = JobCancellationFence::default();
        let active = fence.begin(RUN_ID).unwrap();
        assert!(fence.is_active(RUN_ID).unwrap());
        drop(active);
        assert!(!fence.is_active(RUN_ID).unwrap());

        fence.cancel(RUN_ID).unwrap();
        assert!(fence.begin(RUN_ID).is_err());
    }

    #[test]
    fn only_exact_job_cancel_marker_enables_the_late_start_fence() {
        assert_eq!(
            parse_runtime_stop(&[RUN_ID.to_owned(), "5".to_owned()]).unwrap(),
            (RUN_ID, 5, false)
        );
        assert_eq!(
            parse_runtime_stop(&[RUN_ID.to_owned(), "5".to_owned(), "job-cancel".to_owned(),])
                .unwrap(),
            (RUN_ID, 5, true)
        );
        assert!(
            parse_runtime_stop(&[
                RUN_ID.to_owned(),
                "5".to_owned(),
                "service-cancel".to_owned(),
            ])
            .is_err()
        );
    }

    #[test]
    fn stop_deadline_never_reports_success_while_a_slow_start_remains_active() {
        let temp = tempfile::tempdir().unwrap();
        let executor = OperationExecutor::new(
            ManagedRoots::under(temp.path()),
            &[0; 32],
            MissingContainerRunner,
            None,
        )
        .unwrap();
        let _active = executor.job_cancellation.begin(RUN_ID).unwrap();

        assert!(matches!(
            executor.runtime_stop_until(
                &[RUN_ID.to_owned(), "5".to_owned(), "job-cancel".to_owned(),],
                Instant::now(),
            ),
            Err(OperationError::StopUncertain)
        ));
    }

    #[test]
    fn timeout_stop_failure_is_never_downgraded_to_exit_124() {
        assert!(matches!(
            finish_timed_out_job(Err(OperationError::CommandFailed)),
            Err(OperationError::StopUncertain)
        ));
        assert_eq!(finish_timed_out_job(Ok(())).unwrap(), Some(124));
    }

    #[test]
    fn attached_job_preserves_a_bounded_container_exit_status() {
        assert_eq!(
            bounded_container_exit_code(&CommandOutput {
                success: false,
                stdout: Vec::new(),
                exit_code: Some(37),
            }),
            37
        );
        for exit_code in [None, Some(-1), Some(256)] {
            assert_eq!(
                bounded_container_exit_code(&CommandOutput {
                    success: false,
                    stdout: Vec::new(),
                    exit_code,
                }),
                1
            );
        }
    }

    fn runtime_fixture() -> (TempDir, ManagedRoots) {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(&temp.path().join("data"));
        fs::create_dir_all(roots.agent_data.join("models").join("sha256")).unwrap();
        fs::create_dir_all(roots.agent_data.join("runs").join(RUN_ID).join("outputs")).unwrap();
        fs::create_dir_all(roots.agent_data.join("runs").join(RUN_ID).join("inputs")).unwrap();
        let metadata = roots.agent_data.join("run-metadata").join(RUN_ID);
        fs::create_dir_all(&metadata).unwrap();
        fs::write(metadata.join("runtime.json"), b"{}").unwrap();
        (temp, roots)
    }

    fn artifact_path(roots: &ManagedRoots, key: char) -> PathBuf {
        roots
            .agent_data
            .join("models")
            .join("sha256")
            .join(key.to_string().repeat(64))
    }

    fn runtime_arguments(roots: &ManagedRoots, mounts: &[(PathBuf, &str, bool)]) -> Vec<String> {
        let mut arguments = vec![
            "run".to_owned(),
            "--detach".to_owned(),
            "--name".to_owned(),
            format!("vonk-{RUN_ID}"),
            "--restart".to_owned(),
            "no".to_owned(),
            "--read-only".to_owned(),
            "--tmpfs".to_owned(),
            "/tmp:rw,nosuid,nodev,mode=1777,size=1073741824".to_owned(),
            "--init".to_owned(),
            "--pull".to_owned(),
            "never".to_owned(),
            "--log-driver".to_owned(),
            "local".to_owned(),
            "--log-opt".to_owned(),
            "max-size=10m".to_owned(),
            "--log-opt".to_owned(),
            "max-file=3".to_owned(),
            "--cap-drop=ALL".to_owned(),
            "--security-opt=no-new-privileges".to_owned(),
            "--network".to_owned(),
            "bridge".to_owned(),
            "--pids-limit".to_owned(),
            "4096".to_owned(),
            "--memory".to_owned(),
            "1000000000".to_owned(),
            "--memory-swap".to_owned(),
            "1000000000".to_owned(),
            "--shm-size".to_owned(),
            "134217728".to_owned(),
            "--user".to_owned(),
            "10001:10001".to_owned(),
            "--publish".to_owned(),
            "192.168.1.211:8101:8000".to_owned(),
            "--env".to_owned(),
            "VONK_LISTEN_PORT=8000".to_owned(),
        ];
        for (source, target, readonly) in mounts {
            arguments.extend([
                "--mount".to_owned(),
                format!(
                    "type=bind,src={},dst={target}{}",
                    source.display(),
                    if *readonly { ",readonly" } else { "" }
                ),
            ]);
        }
        arguments.extend([
            "--mount".to_owned(),
            format!(
                "type=bind,src={},dst=/outputs",
                roots
                    .agent_data
                    .join("runs")
                    .join(RUN_ID)
                    .join("outputs")
                    .display()
            ),
            "--mount".to_owned(),
            format!(
                "type=bind,src={},dst=/run/vonk/runtime.json,readonly",
                roots
                    .agent_data
                    .join("run-metadata")
                    .join(RUN_ID)
                    .join("runtime.json")
                    .display()
            ),
            format!(
                "localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002@sha256:{}",
                "c".repeat(64)
            ),
        ]);
        arguments
    }

    fn job_runtime_arguments(roots: &ManagedRoots, model: PathBuf) -> Vec<String> {
        let mut arguments = runtime_arguments(roots, &[(model, "/models", true)]);
        arguments.remove(
            arguments
                .iter()
                .position(|value| value == "--detach")
                .unwrap(),
        );
        let publish = arguments
            .iter()
            .position(|value| value == "--publish")
            .unwrap();
        arguments.drain(publish..=publish + 1);
        let listen = arguments
            .iter()
            .position(|value| value == "VONK_LISTEN_PORT=8000")
            .unwrap();
        arguments.drain(listen - 1..=listen);
        let image = arguments.len() - 1;
        arguments.splice(
            image..image,
            [
                "--env".to_owned(),
                "VONK_JOB_TIMEOUT_SECONDS=3600".to_owned(),
                "--mount".to_owned(),
                format!(
                    "type=bind,src={},dst=/inputs,readonly",
                    roots
                        .agent_data
                        .join("runs")
                        .join(RUN_ID)
                        .join("inputs")
                        .display()
                ),
            ],
        );
        arguments
    }

    #[test]
    fn attached_jobs_require_readonly_inputs_from_the_same_run() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::create_dir(&model).unwrap();
        let arguments = job_runtime_arguments(&roots, model);
        let validated = validate_docker_run(&arguments, &roots, None).unwrap();
        assert!(!validated.detached);
        assert_eq!(validated.job_timeout_seconds, Some(3600));
        let expected_inputs = roots.agent_data.join("runs").join(RUN_ID).join("inputs");
        assert_eq!(validated.inputs.as_deref(), Some(expected_inputs.as_path()));

        let mut writable = arguments.clone();
        let mount = writable
            .iter_mut()
            .find(|value| value.contains("dst=/inputs"))
            .unwrap();
        mount.truncate(mount.len() - ",readonly".len());
        assert!(validate_docker_run(&writable, &roots, None).is_err());

        let other_run = "50000000-0000-4000-8000-000000000005";
        let other_inputs = roots.agent_data.join("runs").join(other_run).join("inputs");
        fs::create_dir_all(&other_inputs).unwrap();
        let mut cross_run = arguments.clone();
        *cross_run
            .iter_mut()
            .find(|value| value.contains("dst=/inputs"))
            .unwrap() = format!(
            "type=bind,src={},dst=/inputs,readonly",
            other_inputs.display()
        );
        assert!(validate_docker_run(&cross_run, &roots, None).is_err());

        let run_root = roots.agent_data.join("runs").join(RUN_ID);
        let relocated = roots.agent_data.join("runs").join(other_run);
        fs::remove_dir_all(&relocated).unwrap();
        fs::rename(&run_root, &relocated).unwrap();
        symlink(&relocated, &run_root).unwrap();
        assert!(validate_docker_run(&arguments, &roots, None).is_err());
    }

    #[test]
    fn runtime_publications_require_an_explicit_routable_bind_address() {
        assert!(valid_publication("192.168.1.211:8101:8000"));
        assert!(valid_publication("192.168.100.10:29500:29500"));

        for value in [
            "8101:8000",
            "0.0.0.0:8101:8000",
            "127.0.0.1:8101:8000",
            "169.254.1.1:8101:8000",
            "[::]:8101:8000",
            "[fe80::1]:8101:8000",
            "[fd00::10]:8101:8000",
            "192.168.1.211:80:8000",
            "192.168.1.211:8101:80",
        ] {
            assert!(!valid_publication(value), "{value}");
        }
    }

    #[test]
    fn runtime_accepts_exact_single_and_multiple_artifact_mounts() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        let tokenizer = artifact_path(&roots, 'b');
        fs::create_dir_all(&model).unwrap();
        fs::create_dir_all(&tokenizer).unwrap();

        let single = runtime_arguments(&roots, &[(model.clone(), "/models", true)]);
        let validated = validate_docker_run(&single, &roots, None).unwrap();
        assert_eq!(validated.models, vec![model.clone()]);

        let multiple = runtime_arguments(
            &roots,
            &[
                (model.clone(), "/models/model", true),
                (tokenizer.clone(), "/models/tokenizer-v2", true),
            ],
        );
        let validated = validate_docker_run(&multiple, &roots, None).unwrap();
        assert_eq!(validated.models, vec![model, tokenizer]);
    }

    #[test]
    fn runtime_rejects_noncanonical_or_unsafe_artifact_mounts() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        let tokenizer = artifact_path(&roots, 'b');
        fs::create_dir_all(&model).unwrap();
        fs::create_dir_all(&tokenizer).unwrap();

        let invalid_single_mounts = [
            (roots.agent_data.join("models"), "/models", true),
            (
                roots.agent_data.join("models").join("sha256"),
                "/models",
                true,
            ),
            (
                roots.agent_data.join("models").join("a".repeat(64)),
                "/models",
                true,
            ),
            (
                roots
                    .agent_data
                    .join("models")
                    .join("sha256")
                    .join("..")
                    .join("sha256")
                    .join("a".repeat(64)),
                "/models",
                true,
            ),
            (artifact_path(&roots, 'A'), "/models", true),
            (model.clone(), "/models", false),
            (model.clone(), "/model", true),
            (model.clone(), "/models/..", true),
            (model.clone(), "/models/model/nested", true),
            (model.clone(), "/models/model/", true),
            (model.clone(), "/models/Model", true),
        ];
        for mount in invalid_single_mounts {
            assert!(
                validate_docker_run(&runtime_arguments(&roots, &[mount]), &roots, None).is_err()
            );
        }

        for mounts in [
            vec![
                (model.clone(), "/models/model", true),
                (model.clone(), "/models/tokenizer", true),
            ],
            vec![
                (model.clone(), "/models/model", true),
                (tokenizer.clone(), "/models/model", true),
            ],
            vec![
                (model.clone(), "/models", true),
                (tokenizer.clone(), "/models/tokenizer", true),
            ],
        ] {
            assert!(
                validate_docker_run(&runtime_arguments(&roots, &mounts), &roots, None).is_err()
            );
        }

        let too_many = (0..17)
            .map(|index| {
                let source = roots
                    .agent_data
                    .join("models")
                    .join("sha256")
                    .join(format!("{index:064x}"));
                fs::create_dir(&source).unwrap();
                (source, format!("/models/artifact-{index}"))
            })
            .collect::<Vec<_>>();
        let too_many = too_many
            .iter()
            .map(|(source, target)| (source.clone(), target.as_str(), true))
            .collect::<Vec<_>>();
        assert!(validate_docker_run(&runtime_arguments(&roots, &too_many), &roots, None).is_err());

        let symlinked = artifact_path(&roots, 'd');
        symlink(&model, &symlinked).unwrap();
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(symlinked, "/models", true)]),
                &roots,
                None,
            )
            .is_err()
        );
    }

    #[test]
    fn runtime_rejects_symlinked_model_ancestors_and_canonical_escapes() {
        {
            let (temp, roots) = runtime_fixture();
            let models = roots.agent_data.join("models");
            fs::remove_dir_all(&models).unwrap();
            let outside_models = temp.path().join("outside-models");
            let outside_model = outside_models.join("sha256").join("a".repeat(64));
            fs::create_dir_all(&outside_model).unwrap();
            symlink(&outside_models, &models).unwrap();

            let mount = artifact_path(&roots, 'a');
            assert!(
                validate_docker_run(
                    &runtime_arguments(&roots, &[(mount, "/models", true)]),
                    &roots,
                    None,
                )
                .is_err()
            );
        }

        {
            let (temp, roots) = runtime_fixture();
            let model_root = roots.agent_data.join("models").join("sha256");
            fs::remove_dir(&model_root).unwrap();
            let outside_model_root = temp.path().join("outside-sha256");
            fs::create_dir_all(outside_model_root.join("a".repeat(64))).unwrap();
            symlink(&outside_model_root, &model_root).unwrap();

            let mount = artifact_path(&roots, 'a');
            assert!(
                validate_docker_run(
                    &runtime_arguments(&roots, &[(mount, "/models", true)]),
                    &roots,
                    None,
                )
                .is_err()
            );
        }

        {
            let temp = tempfile::tempdir().unwrap();
            let outside = temp.path().join("outside-agent-data");
            let agent_data = temp.path().join("agent-data-link");
            let roots = ManagedRoots::under(&agent_data);
            let model = outside.join("models").join("sha256").join("a".repeat(64));
            fs::create_dir_all(&model).unwrap();
            fs::create_dir_all(outside.join("runs").join(RUN_ID).join("outputs")).unwrap();
            let metadata = outside.join("run-metadata").join(RUN_ID);
            fs::create_dir_all(&metadata).unwrap();
            fs::write(metadata.join("runtime.json"), b"{}").unwrap();
            symlink(&outside, &agent_data).unwrap();

            let mount = artifact_path(&roots, 'a');
            assert!(
                validate_docker_run(
                    &runtime_arguments(&roots, &[(mount, "/models", true)]),
                    &roots,
                    None,
                )
                .is_err()
            );
        }
    }

    #[test]
    fn runtime_rejects_unsafe_model_ownership_and_modes() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::create_dir_all(&model).unwrap();
        let arguments = runtime_arguments(&roots, &[(model.clone(), "/models", true)]);

        let owner = fs::symlink_metadata(&roots.agent_data).unwrap().uid();
        assert!(validate_docker_run(&arguments, &roots, Some(owner ^ 1)).is_err());
        validate_docker_run(&arguments, &roots, Some(owner)).unwrap();

        for path in [
            roots.agent_data.clone(),
            roots.agent_data.join("models"),
            roots.agent_data.join("models").join("sha256"),
            model,
        ] {
            fs::set_permissions(&path, fs::Permissions::from_mode(0o770)).unwrap();
            assert!(validate_docker_run(&arguments, &roots, Some(owner)).is_err());
            fs::set_permissions(&path, fs::Permissions::from_mode(0o750)).unwrap();
        }
    }
}
