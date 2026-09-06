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
const MAX_COMPILED_MODEL_FILES: usize = 4096;
const MAX_COMPILED_MODEL_PATH_BYTES: usize = 512;
const MAX_COMPILED_MODEL_BYTES: u64 = 1024 * 1024 * 1024 * 1024;
const DOCKER_FIREWALL: &str = "/usr/lib/vonk-forge/vonk-forge-docker-firewall";
const DOCKER_FIREWALL_CONFIG: &str = "/etc/vonk-forge-agent/docker-firewall.conf";
const RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION: u8 = 2;

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
    #[error("runtime image load failed")]
    RuntimeImageLoadFailed,
    #[error("runtime image inspection failed")]
    RuntimeImageInspectFailed,
    #[error("runtime image identity is invalid")]
    RuntimeImageIdentityInvalid,
    #[error("runtime image receipt could not be written")]
    RuntimeImageReceiptFailed,
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

const ROOT_COMMAND_PATH: &str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

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
            .env("PATH", ROOT_COMMAND_PATH)
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

#[cfg(test)]
mod process_command_runner_tests {
    use super::ROOT_COMMAND_PATH;

    #[test]
    fn privileged_command_path_includes_debian_administrative_binaries() {
        let entries = ROOT_COMMAND_PATH.split(':').collect::<Vec<_>>();
        assert!(entries.contains(&"/usr/sbin"));
        assert!(entries.contains(&"/sbin"));
        assert!(entries.iter().all(|entry| entry.starts_with('/')));
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

/// The helper's durable proof that one exact archive was imported into one
/// exact local image reference.  These identities are deliberately separate:
/// the registry manifest identifies the signed image, the archive digest
/// identifies the transferred bytes, and Docker's config ID identifies the
/// object actually loaded on this host.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct RuntimeImageReceipt {
    schema_version: u8,
    registry_index_digest: String,
    platform_manifest_digest: String,
    archive_sha256: String,
    archive_bytes: u64,
    image_config_id: String,
    local_image_reference: String,
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
            HostRuntimeAction::ImageImport => {
                self.runtime_image_import(&request.arguments)
                    .map(|()| RuntimeRequestOutcome {
                        exit_code: None,
                        recipe_run_observation: None,
                    })
            }
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

    fn runtime_image_import(&self, arguments: &[String]) -> Result<(), OperationError> {
        let [
            archive,
            archive_sha256,
            archive_bytes,
            registry_index_digest,
            platform_manifest_digest,
            image_reference,
        ] = arguments
        else {
            return Err(OperationError::InvalidOperation);
        };
        let archive = Path::new(archive);
        let (archive_root, canonical_archive_root) = self.canonical_archive_root()?;
        let expected_archive = archive_root.join(archive_sha256);
        let canonical_archive =
            fs::canonicalize(&expected_archive).map_err(|_| OperationError::InvalidArtifact)?;
        let (local_image, embedded_digest) = parse_local_image_reference(image_reference)?;
        if !archive.is_absolute()
            || archive != expected_archive
            || canonical_archive.parent() != Some(canonical_archive_root.as_path())
            || !lower_hex(archive_sha256, 64)
            || !valid_oci_digest(registry_index_digest)
            || !valid_oci_digest(platform_manifest_digest)
            || embedded_digest != *platform_manifest_digest
        {
            return Err(OperationError::InvalidOperation);
        }
        let expected_bytes = archive_bytes
            .parse::<u64>()
            .ok()
            .filter(|value| (1..=MAX_RUNTIME_ARCHIVE_BYTES).contains(value))
            .ok_or(OperationError::InvalidOperation)?;
        self.verify_runtime_archive(archive, archive_sha256, expected_bytes)?;
        let loaded = self
            .run_docker(&[
                "load".to_owned(),
                "--input".to_owned(),
                archive.display().to_string(),
            ])
            .map_err(|error| match error {
                OperationError::CommandFailed => OperationError::RuntimeImageLoadFailed,
                other => other,
            })?;
        // Docker's human-readable load output is not a stable interface: it
        // varies across Docker releases and archive producers (some omit the
        // tag, and some emit no output at all). The image reference and
        // metadata are verified below, so requiring a particular output line
        // would reject an otherwise valid, digest-bound import for no safety
        // benefit.
        if !loaded.success {
            return Err(OperationError::RuntimeImageLoadFailed);
        }
        // Docker archives retain the source tag that the producer exported.
        // Bind that verified loaded object to the controller-derived local
        // reference before inspecting it. Some Docker versions omit the
        // human-readable load line; inspection below remains authoritative.
        if let Some(source_image) = loaded_image_source(&loaded.stdout) {
            let tagged = self
                .run_docker(&["tag".to_owned(), source_image, local_image])
                .map_err(|error| match error {
                    OperationError::CommandFailed => OperationError::RuntimeImageInspectFailed,
                    other => other,
                })?;
            if !tagged.success {
                return Err(OperationError::RuntimeImageInspectFailed);
            }
        }
        let inspected =
            self.inspect_runtime_image(image_reference)
                .map_err(|error| match error {
                    OperationError::CommandFailed => OperationError::RuntimeImageInspectFailed,
                    OperationError::InvalidArtifact => OperationError::RuntimeImageIdentityInvalid,
                    other => other,
                })?;
        if inspected.1 != "linux"
            || inspected.2 != "arm64"
            || inspected.3 != "v1"
            || !numeric_non_root_user(&inspected.4)
        {
            return Err(OperationError::RuntimeImageIdentityInvalid);
        }
        self.write_image_receipt(
            registry_index_digest,
            platform_manifest_digest,
            archive_sha256,
            expected_bytes,
            image_reference,
            &inspected.0,
        )
        .map_err(|error| match error {
            OperationError::Io(_) | OperationError::InvalidArtifact => {
                OperationError::RuntimeImageReceiptFailed
            }
            other => other,
        })
    }

    fn runtime_image_inspect(&self, arguments: &[String]) -> Result<(), OperationError> {
        let [
            archive_sha256,
            registry_index_digest,
            platform_manifest_digest,
            image_reference,
            user,
        ] = arguments
        else {
            return Err(OperationError::InvalidOperation);
        };
        let (_image, embedded_digest) = parse_local_image_reference(image_reference)?;
        if &embedded_digest != platform_manifest_digest
            || !lower_hex(archive_sha256, 64)
            || !valid_oci_digest(registry_index_digest)
            || !valid_oci_digest(platform_manifest_digest)
            || !numeric_non_root_user(user)
        {
            return Err(OperationError::InvalidOperation);
        }
        let inspected = self.inspect_runtime_image(image_reference)?;
        if inspected.1 != "linux"
            || inspected.2 != "arm64"
            || inspected.3 != "v1"
            || inspected.4 != *user
        {
            return Err(OperationError::InvalidArtifact);
        }
        self.require_image_receipt(
            archive_sha256,
            registry_index_digest,
            platform_manifest_digest,
            image_reference,
            &inspected.0,
        )
    }

    fn runtime_start(&self, arguments: &[String]) -> Result<Option<i32>, OperationError> {
        let [
            archive_sha256,
            registry_index_digest,
            platform_manifest_digest,
            image_reference,
            docker @ ..,
        ] = arguments
        else {
            return Err(OperationError::InvalidOperation);
        };
        let validated = validate_docker_run_with_archive(
            docker,
            &self.roots,
            self.runtime_request_owner_uid,
            Some(archive_sha256),
            Some(registry_index_digest),
        )?;
        if image_reference != &validated.local_image_reference
            || archive_sha256 != &validated.archive_sha256
            || registry_index_digest != &validated.registry_index_digest
            || platform_manifest_digest != &validated.platform_manifest_digest
        {
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
        let inspected = self.inspect_runtime_image(&validated.local_image_reference)?;
        self.require_image_receipt(
            archive_sha256,
            registry_index_digest,
            platform_manifest_digest,
            &validated.local_image_reference,
            &inspected.0,
        )?;
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
        // The signed wire shape carries the executable once after the image as
        // an explicit marker for validation. Docker already receives that
        // executable through --entrypoint, so consume the marker here to keep
        // the actual process argv from running it twice. Keep
        // validated.arguments unchanged: the semantic receipt identity is
        // bound to the exact signed request shape above.
        let mut compiled = validated.docker_arguments()?;
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
        let [
            archive_sha256,
            registry_index_digest,
            platform_manifest_digest,
            image_reference,
            docker @ ..,
        ] = arguments
        else {
            return Err(OperationError::InvalidOperation);
        };
        let validated = validate_docker_run_with_archive(
            docker,
            &self.roots,
            self.runtime_request_owner_uid,
            Some(archive_sha256),
            Some(registry_index_digest),
        )?;
        if image_reference != &validated.local_image_reference
            || archive_sha256 != &validated.archive_sha256
            || registry_index_digest != &validated.registry_index_digest
            || platform_manifest_digest != &validated.platform_manifest_digest
            || !validated.detached
        {
            return Err(OperationError::InvalidOperation);
        }
        self.require_host_endpoint_firewall(&validated)?;
        let inspected = self.inspect_runtime_image(&validated.local_image_reference)?;
        self.require_image_receipt(
            archive_sha256,
            registry_index_digest,
            platform_manifest_digest,
            &validated.local_image_reference,
            &inspected.0,
        )?;
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
        let cache_root = run.cache_root.as_path();
        let tmp_root = run.tmp_root.parent().ok_or(OperationError::UnsafePath)?;
        ensure_runtime_directory(cache_root)?;
        ensure_runtime_directory(&run.cache_home)?;
        ensure_runtime_directory(tmp_root)?;
        ensure_runtime_directory(&run.tmp_root)?;
        for (path, access) in [
            (run.outputs.as_path(), "rwx"),
            (cache_root, "rwx"),
            (run.cache_home.as_path(), "rwx"),
            (tmp_root, "rwx"),
            (run.tmp_root.as_path(), "rwx"),
            (run.runtime_contract.as_path(), "r"),
        ] {
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
        registry_index_digest: &str,
        platform_manifest_digest: &str,
        archive_sha256: &str,
        archive_bytes: u64,
        local_image_reference: &str,
        local_config_id: &str,
    ) -> Result<(), OperationError> {
        fs::create_dir_all(&self.roots.runtime_image_receipts)?;
        fs::set_permissions(
            &self.roots.runtime_image_receipts,
            fs::Permissions::from_mode(0o700),
        )?;
        if !valid_oci_digest(registry_index_digest)
            || !valid_oci_digest(platform_manifest_digest)
            || !lower_hex(archive_sha256, 64)
            || !valid_local_image_reference(local_image_reference)
            || !valid_oci_digest(local_config_id)
        {
            return Err(OperationError::InvalidArtifact);
        }
        let path = self.roots.runtime_image_receipts.join(archive_sha256);
        let receipt = RuntimeImageReceipt {
            schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
            registry_index_digest: registry_index_digest.to_owned(),
            platform_manifest_digest: platform_manifest_digest.to_owned(),
            archive_sha256: archive_sha256.to_owned(),
            archive_bytes,
            image_config_id: local_config_id.to_owned(),
            local_image_reference: local_image_reference.to_owned(),
        };
        let mut body = canonical_json(&receipt).map_err(|_| OperationError::InvalidArtifact)?;
        body.push(b'\n');
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                file.write_all(&body)?;
                file.sync_all()?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                if fs::read(&path)? != body {
                    return Err(OperationError::InvalidArtifact);
                }
            }
            Err(error) => return Err(error.into()),
        }
        sync_directory(&self.roots.runtime_image_receipts)
    }

    fn require_image_receipt(
        &self,
        archive_sha256: &str,
        registry_index_digest: &str,
        platform_manifest_digest: &str,
        local_image_reference: &str,
        image_config_id: &str,
    ) -> Result<(), OperationError> {
        if !lower_hex(archive_sha256, 64)
            || !valid_oci_digest(registry_index_digest)
            || !valid_oci_digest(platform_manifest_digest)
            || !valid_local_image_reference(local_image_reference)
            || !valid_oci_digest(image_config_id)
        {
            return Err(OperationError::InvalidOperation);
        }
        let path = self.roots.runtime_image_receipts.join(archive_sha256);
        let metadata = fs::symlink_metadata(&path).map_err(|_| OperationError::InvalidArtifact)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
            || metadata.nlink() != 1
            || metadata.mode() & 0o022 != 0
            || metadata.len() > 2048
        {
            return Err(OperationError::InvalidArtifact);
        }
        let receipt: RuntimeImageReceipt =
            serde_json::from_slice(&fs::read(path).map_err(|_| OperationError::InvalidArtifact)?)
                .map_err(|_| OperationError::InvalidArtifact)?;
        if receipt.schema_version != RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION
            || receipt.archive_sha256 != archive_sha256
            || receipt.registry_index_digest != registry_index_digest
            || receipt.platform_manifest_digest != platform_manifest_digest
            || receipt.local_image_reference != local_image_reference
            || receipt.image_config_id != image_config_id
        {
            return Err(OperationError::InvalidArtifact);
        }
        let (archive_root, _) = self.canonical_archive_root()?;
        let archive = archive_root.join(archive_sha256);
        self.verify_runtime_archive(archive.as_path(), archive_sha256, receipt.archive_bytes)?;
        Ok(())
    }

    fn canonical_archive_root(&self) -> Result<(PathBuf, PathBuf), OperationError> {
        let archive_root = self.roots.agent_data.join("oci-archives");
        let metadata =
            fs::symlink_metadata(&archive_root).map_err(|_| OperationError::UnsafePath)?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(OperationError::UnsafePath);
        }
        let canonical_root = archive_root
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        let canonical_agent_data = self
            .roots
            .agent_data
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        if canonical_root.parent() != Some(canonical_agent_data.as_path()) {
            return Err(OperationError::UnsafePath);
        }
        Ok((archive_root, canonical_root))
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
    local_image_reference: String,
    registry_index_digest: String,
    platform_manifest_digest: String,
    archive_sha256: String,
    arguments: Vec<String>,
    entrypoint: String,
    detached: bool,
    image_index: usize,
    run_id: String,
    uid: u32,
    models: Vec<PathBuf>,
    inputs: Option<PathBuf>,
    outputs: PathBuf,
    cache_root: PathBuf,
    cache_home: PathBuf,
    tmp_root: PathBuf,
    runtime_contract: PathBuf,
    host_endpoint_port: Option<u16>,
    job_timeout_seconds: Option<u16>,
}

impl ValidatedDockerRun {
    fn docker_arguments(&self) -> Result<Vec<String>, OperationError> {
        let marker_index = self
            .image_index
            .checked_add(1)
            .ok_or(OperationError::InvalidOperation)?;
        if self.arguments.get(marker_index) != Some(&self.entrypoint) {
            return Err(OperationError::InvalidOperation);
        }
        let mut arguments = self.arguments.clone();
        arguments.remove(marker_index);
        Ok(arguments)
    }
}

#[cfg(test)]
fn validate_docker_run(
    arguments: &[String],
    roots: &ManagedRoots,
    agent_data_owner_uid: Option<u32>,
) -> Result<ValidatedDockerRun, OperationError> {
    validate_docker_run_with_archive(arguments, roots, agent_data_owner_uid, None, None)
}

fn validate_docker_run_with_archive(
    arguments: &[String],
    roots: &ManagedRoots,
    agent_data_owner_uid: Option<u32>,
    archive_sha256: Option<&str>,
    registry_index_digest: Option<&str>,
) -> Result<ValidatedDockerRun, OperationError> {
    if arguments.first().map(String::as_str) != Some("run") {
        return Err(OperationError::InvalidOperation);
    }
    let mut index = 1;
    let mut detach = false;
    let mut remove = false;
    let mut name: Option<String> = None;
    let mut entrypoint: Option<String> = None;
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
    let infiniband = false;
    let mut memlock = false;
    let mut stack = false;
    let mut pids = false;
    let mut memory: Option<u64> = None;
    let mut memory_swap: Option<u64> = None;
    let mut shm_size: Option<u64> = None;
    let mut user: Option<(u32, Option<u32>)> = None;
    let mut publishes = 0_usize;
    let mut published_ports = BTreeSet::new();
    let mut environments = 0_usize;
    let mut listen_port = None;
    let mut master_port = None;
    let mut rank = None;
    let mut job_timeout_seconds = None;
    let mut gpu = false;
    let mut home = false;
    let mut xdg_cache_home = false;
    let mut tmpdir = false;
    let mut models = Vec::new();
    let mut model_sources = BTreeSet::new();
    let mut model_targets = BTreeSet::new();
    let mut outputs = None;
    let mut cache_root = None;
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
            "--entrypoint" if entrypoint.is_none() => {
                index += 1;
                let value = arguments
                    .get(index)
                    .filter(|value| valid_entrypoint(value))
                    .ok_or(OperationError::InvalidOperation)?;
                entrypoint = Some(value.clone());
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
                    Some("none") => Some("none"),
                    Some("bridge") => Some("bridge"),
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
            "--device" if !gpu => {
                index += 1;
                match arguments.get(index).map(String::as_str) {
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
                let (_, _, container_port) = parse_publication(
                    arguments
                        .get(index)
                        .ok_or(OperationError::InvalidOperation)?,
                )
                .ok_or(OperationError::InvalidOperation)?;
                if !published_ports.insert(container_port) {
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
                if let Some(value) = value.strip_prefix("VONK_MASTER_PORT=") {
                    let parsed = value
                        .parse::<u16>()
                        .ok()
                        .filter(|port| (1024..=65535).contains(port))
                        .ok_or(OperationError::InvalidOperation)?;
                    if master_port.replace(parsed).is_some() {
                        return Err(OperationError::InvalidOperation);
                    }
                }
                if let Some(value) = value.strip_prefix("VONK_RANK=") {
                    let parsed = value
                        .parse::<u32>()
                        .ok()
                        .ok_or(OperationError::InvalidOperation)?;
                    if rank.replace(parsed).is_some() {
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
                home |= value == "HOME=/outputs/cache/home";
                xdg_cache_home |= value == "XDG_CACHE_HOME=/outputs/cache";
                tmpdir |= value == "TMPDIR=/outputs/tmp";
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
                } else if target == "/outputs/cache"
                    && !readonly
                    && valid_runtime_cache_mount(&source, roots)
                    && cache_root.is_none()
                {
                    cache_root = Some(source);
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
    let entrypoint = entrypoint.ok_or(OperationError::InvalidOperation)?;
    if arguments.get(index + 1) != Some(&entrypoint) {
        return Err(OperationError::InvalidOperation);
    }
    let (_image, embedded_digest) = parse_local_image_reference(&image_reference)?;
    let registry_index_digest = registry_index_digest.unwrap_or(embedded_digest.as_str());
    if !valid_oci_digest(registry_index_digest)
        || archive_sha256.is_some_and(|digest| !lower_hex(digest, 64))
    {
        return Err(OperationError::InvalidOperation);
    }
    let (uid, _gid) = user.ok_or(OperationError::InvalidOperation)?;
    let outputs = outputs.ok_or(OperationError::InvalidOperation)?;
    let cache_root = cache_root.ok_or(OperationError::InvalidOperation)?;
    require_safe_directory(&cache_root, agent_data_owner_uid)?;
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
        || !valid_entrypoint(&entrypoint)
        || network.is_none()
        || !pids
        || memory.is_none()
        || memory_swap != memory
        || shm_size.is_none_or(|value| value > memory.unwrap_or_default())
        || (detach && (inputs.is_some() || job_timeout_seconds.is_some()))
        || (!detach && (inputs.is_none() || job_timeout_seconds.is_none()))
        || ipc_host
        || infiniband
        || memlock
        || stack
        || !home
        || !xdg_cache_home
        || !tmpdir
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
    let endpoint_workload = listen_port.is_some();
    let distributed_workload = master_port.is_some();
    let bridge_workload = endpoint_workload || distributed_workload;
    let expected_network = if bridge_workload { "bridge" } else { "none" };
    let publication_ports_match = published_ports
        .iter()
        .all(|port| Some(*port) == listen_port || Some(*port) == master_port);
    let endpoint_published = listen_port.is_some_and(|port| published_ports.contains(&port));
    let rendezvous_published =
        master_port.is_some_and(|port| rank == Some(0) && published_ports.contains(&port));
    let nonzero_rendezvous_published = master_port.is_some_and(|port| {
        rank != Some(0) && listen_port != Some(port) && published_ports.contains(&port)
    });
    if network != Some(expected_network)
        || publishes != published_ports.len()
        || !publication_ports_match
        || (!bridge_workload && publishes != 0)
        || (endpoint_workload && !endpoint_published)
        || (distributed_workload && rank.is_none())
        || (distributed_workload && rank == Some(0) && !rendezvous_published)
        || nonzero_rendezvous_published
    {
        return Err(OperationError::InvalidOperation);
    }
    if models.is_empty()
        || models.len() > MAX_COMPILED_MODEL_FILES
        || models.len() > 1 && model_targets.contains("/models")
    {
        return Err(OperationError::InvalidOperation);
    }
    let canonical_model_root = canonical_model_root(roots, agent_data_owner_uid)?;
    let mut model_files = 0_usize;
    let mut model_bytes = 0_u64;
    for path in &models {
        require_safe_model_path(path, &canonical_model_root, agent_data_owner_uid)?;
        collect_model_tree(
            path,
            agent_data_owner_uid,
            &mut model_files,
            &mut model_bytes,
        )?;
        if model_files > MAX_COMPILED_MODEL_FILES || model_bytes > MAX_COMPILED_MODEL_BYTES {
            return Err(OperationError::InvalidOperation);
        }
        let canonical = path
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        if !canonical.starts_with(&canonical_model_root) {
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
    compiled_arguments[index] = image_reference.clone();
    let tmp_root = outputs.join("tmp").join(state_run_id);
    Ok(ValidatedDockerRun {
        local_image_reference: image_reference,
        registry_index_digest: registry_index_digest.to_owned(),
        platform_manifest_digest: embedded_digest,
        archive_sha256: archive_sha256.unwrap_or_default().to_owned(),
        arguments: compiled_arguments,
        entrypoint,
        detached: detach,
        image_index: index,
        run_id: state_run_id.to_owned(),
        uid,
        models,
        inputs,
        cache_root: cache_root.clone(),
        outputs,
        cache_home: cache_root.join("home"),
        tmp_root,
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
    if !source.starts_with(&roots.models) {
        return false;
    }
    if target.len() > MAX_COMPILED_MODEL_PATH_BYTES
        || (target != "/models"
            && (!target.starts_with("/models/")
                || target.ends_with('/')
                || !target.split('/').skip(1).all(valid_artifact_id)))
    {
        return false;
    }
    let relative = source.strip_prefix(&roots.models).ok();
    let components = relative
        .into_iter()
        .flat_map(Path::components)
        .collect::<Vec<_>>();
    let new_layout = components.len() >= 3
        && matches!(components[0], Component::Normal(value) if lower_hex(&value.to_string_lossy(), 64))
        && matches!(components[1], Component::Normal(value) if valid_artifact_id(&value.to_string_lossy()))
        && components[2..]
            .iter()
            .all(|component| matches!(component, Component::Normal(value) if valid_artifact_id(&value.to_string_lossy())));
    let selection_layout = components.len() >= 2
        && matches!(components[0], Component::Normal(value) if valid_artifact_id(&value.to_string_lossy()) && value != "sha256")
        && components[1..]
            .iter()
            .all(|component| matches!(component, Component::Normal(value) if valid_artifact_id(&value.to_string_lossy())));
    let legacy_layout = components.len() == 2
        && matches!(components[0], Component::Normal(value) if value == "sha256")
        && matches!(components[1], Component::Normal(value) if lower_hex(&value.to_string_lossy(), 64));
    if !(new_layout || selection_layout || legacy_layout) {
        return false;
    }
    if target == "/models" {
        return true;
    }
    target
        .strip_prefix("/models/")
        .is_some_and(|value| value.split('/').all(valid_artifact_id))
}

fn valid_runtime_cache_mount(source: &Path, roots: &ManagedRoots) -> bool {
    let Ok(relative) = source.strip_prefix(roots.agent_data.join("installations")) else {
        return false;
    };
    let components = relative.components().collect::<Vec<_>>();
    components.len() == 2
        && components[1].as_os_str() == "runtime-cache"
        && components[0]
            .as_os_str()
            .to_str()
            .is_some_and(valid_artifact_id)
}

fn require_safe_model_path(
    path: &Path,
    canonical_model_root: &Path,
    required_owner_uid: Option<u32>,
) -> Result<(), OperationError> {
    let canonical_path = path
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    if !canonical_path.starts_with(canonical_model_root) {
        return Err(OperationError::UnsafePath);
    }
    let mut current = path.to_path_buf();
    let metadata = fs::symlink_metadata(&current).map_err(|_| OperationError::UnsafePath)?;
    if metadata.file_type().is_symlink()
        || !(metadata.is_file() || metadata.is_dir())
        || metadata.mode() & 0o022 != 0
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
    {
        return Err(OperationError::UnsafePath);
    }
    while let Some(parent) = current.parent() {
        let metadata = fs::symlink_metadata(parent).map_err(|_| OperationError::UnsafePath)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || metadata.mode() & 0o022 != 0
            || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(OperationError::UnsafePath);
        }
        if parent.canonicalize().ok().as_deref() == Some(canonical_model_root) {
            return Ok(());
        }
        current = parent.to_path_buf();
    }
    Err(OperationError::UnsafePath)
}

fn collect_model_tree(
    path: &Path,
    required_owner_uid: Option<u32>,
    file_count: &mut usize,
    total_bytes: &mut u64,
) -> Result<(), OperationError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
    if metadata.file_type().is_symlink()
        || metadata.mode() & 0o022 != 0
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
    {
        return Err(OperationError::UnsafePath);
    }
    if metadata.is_file() {
        *file_count = file_count
            .checked_add(1)
            .ok_or(OperationError::InvalidOperation)?;
        *total_bytes = total_bytes
            .checked_add(metadata.len())
            .ok_or(OperationError::InvalidOperation)?;
        return Ok(());
    }
    if !metadata.is_dir() {
        return Err(OperationError::UnsafePath);
    }
    let mut entries = fs::read_dir(path)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);
    for entry in entries {
        collect_model_tree(&entry.path(), required_owner_uid, file_count, total_bytes)?;
    }
    Ok(())
}

fn canonical_model_root(
    roots: &ManagedRoots,
    agent_data_owner_uid: Option<u32>,
) -> Result<PathBuf, OperationError> {
    let agent_data = &roots.agent_data;
    let models = agent_data.join("models");
    for path in [agent_data, &models] {
        require_safe_directory(path, agent_data_owner_uid)?;
    }
    let canonical_agent_data = agent_data
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_models = models
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    if canonical_models.parent() != Some(canonical_agent_data.as_path()) {
        return Err(OperationError::UnsafePath);
    }
    Ok(canonical_models)
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

fn ensure_runtime_directory(path: &Path) -> Result<(), OperationError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(OperationError::UnsafePath);
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir_all(path)?;
        }
        Err(error) => return Err(error.into()),
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() || metadata.mode() & 0o077 != 0 {
        return Err(OperationError::UnsafePath);
    }
    Ok(())
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

fn parse_publication(value: &str) -> Option<(std::net::Ipv4Addr, u16, u16)> {
    let (address, ports) = if let Some(value) = value.strip_prefix('[') {
        let (address, ports) = value.split_once("]:")?;
        (address, ports)
    } else {
        let (address, ports) = value.split_once(':')?;
        (address, ports)
    };
    let address = address.parse::<std::net::Ipv4Addr>().ok()?;
    if address.is_unspecified()
        || address.is_loopback()
        || address.is_multicast()
        || address.is_link_local()
    {
        return None;
    }
    let (host, container) = ports.split_once(':')?;
    if container.contains(':') {
        return None;
    }
    let host = host
        .parse::<u16>()
        .ok()
        .filter(|port| (1024..=65535).contains(port))?;
    let container = container
        .parse::<u16>()
        .ok()
        .filter(|port| (1024..=65535).contains(port))?;
    Some((address, host, container))
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
    let recipe_build = value
        .strip_prefix("localhost/vonk/recipe-build-")
        .and_then(|value| uuid::Uuid::parse_str(value).ok().map(|id| (value, id)))
        .is_some_and(|(value, id)| id.to_string() == value);
    let compiled_runtime = value
        .strip_prefix("localhost/vonk/compiled-runtime-")
        .is_some_and(|value| lower_hex(value, 64));
    recipe_build || compiled_runtime
}

fn valid_local_image_reference(value: &str) -> bool {
    value
        .split_once('@')
        .is_some_and(|(image, digest)| valid_local_image(image) && valid_oci_digest(digest))
}

fn loaded_image_source(stdout: &[u8]) -> Option<String> {
    std::str::from_utf8(stdout)
        .ok()?
        .lines()
        .find_map(|line| line.strip_prefix("Loaded image: "))
        .map(str::trim)
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 256
                && !value.starts_with('-')
                && value
                    .bytes()
                    .all(|byte| !byte.is_ascii_control() && !byte.is_ascii_whitespace())
        })
        .map(str::to_owned)
}

fn valid_entrypoint(value: &str) -> bool {
    value.starts_with("/opt/vonk/bin/")
        && value.len() <= 256
        && !value.ends_with('/')
        && !value.contains("//")
        && !value.split('/').any(|part| part == "." || part == "..")
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'/' | b'_' | b'-' | b'.'))
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
    use std::sync::{Arc, Mutex};
    use std::time::Instant;

    use tempfile::TempDir;

    use super::{
        CommandOutput, CommandRunner, JobCancellationFence, ManagedRoots, OperationError,
        OperationExecutor, RuntimeImageReceipt, bounded_container_exit_code, finish_timed_out_job,
        hex_sha256, loaded_image_source, parse_publication, parse_runtime_stop,
        validate_docker_run,
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

    #[derive(Clone, Default)]
    struct RecordingAclRunner {
        calls: Arc<Mutex<Vec<Vec<String>>>>,
    }

    impl CommandRunner for RecordingAclRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new("/usr/bin/setfacl"));
            self.calls.lock().unwrap().push(arguments.to_vec());
            Ok(CommandOutput {
                success: true,
                stdout: Vec::new(),
                exit_code: Some(0),
            })
        }
    }

    #[derive(Clone, Copy)]
    struct RuntimeImportRunner;

    impl CommandRunner for RuntimeImportRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new("/usr/bin/docker"));
            let inspect = arguments.first().map(String::as_str) == Some("image")
                && arguments.get(1).map(String::as_str) == Some("inspect");
            Ok(CommandOutput {
                success: true,
                stdout: if inspect {
                    format!("sha256:{}\tlinux\tarm64\tv1\t10001:10001\n", "c".repeat(64))
                        .into_bytes()
                } else {
                    Vec::new()
                },
                exit_code: Some(0),
            })
        }
    }

    #[test]
    fn loaded_image_source_accepts_only_a_single_safe_load_line() {
        assert_eq!(
            loaded_image_source(
                b"Loaded image: localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002:latest\n"
            ),
            Some(
                "localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002:latest"
                    .to_owned()
            )
        );
        assert_eq!(
            loaded_image_source(b"Loaded image ID: sha256:deadbeef\n"),
            None
        );
        assert_eq!(loaded_image_source(b"Loaded image: unsafe tag\n"), None);
        assert_eq!(loaded_image_source(b"Loaded image: --help\n"), None);
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
        fs::create_dir_all(
            roots
                .agent_data
                .join("installations")
                .join("installation-1")
                .join("runtime-cache"),
        )
        .unwrap();
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
            "--entrypoint".to_owned(),
            "/opt/vonk/bin/vllm".to_owned(),
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
            "none".to_owned(),
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
            "--env".to_owned(),
            "HOME=/outputs/cache/home".to_owned(),
            "--env".to_owned(),
            "XDG_CACHE_HOME=/outputs/cache".to_owned(),
            "--env".to_owned(),
            "TMPDIR=/outputs/tmp".to_owned(),
            "--env".to_owned(),
            "VONK_RUNTIME_SPEC=/run/vonk/runtime.json".to_owned(),
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
                "type=bind,src={},dst=/outputs/cache",
                roots
                    .agent_data
                    .join("installations")
                    .join("installation-1")
                    .join("runtime-cache")
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
            "/opt/vonk/bin/vllm".to_owned(),
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
        let image = arguments
            .iter()
            .position(|value| value.starts_with("localhost/vonk/"))
            .unwrap();
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
        assert!(parse_publication("192.168.1.211:8101:8000").is_some());
        assert!(parse_publication("192.168.100.10:29500:29500").is_some());

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
            assert!(parse_publication(value).is_none(), "{value}");
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
    fn runtime_accepts_selection_scoped_nested_model_files_beyond_legacy_limit() {
        let (_temp, roots) = runtime_fixture();
        let model_set = roots.agent_data.join("models").join("a".repeat(64));
        let primary = model_set.join("primary");
        let draft = model_set.join("draft");
        fs::create_dir_all(&primary).unwrap();
        fs::create_dir_all(&draft).unwrap();
        let mut mounts = Vec::new();
        for index in 0..132 {
            let name = format!("artifact-{index}.bin");
            let source = if index == 0 {
                primary.join(&name)
            } else {
                draft.join(&name)
            };
            fs::write(&source, b"identical receipt bytes").unwrap();
            let target = if index == 0 {
                format!("/models/{name}")
            } else {
                format!("/models/draft/{name}")
            };
            mounts.push((source, target));
        }
        let mounts = mounts
            .iter()
            .map(|(source, target)| (source.clone(), target.as_str(), true))
            .collect::<Vec<_>>();
        let validated = validate_docker_run(&runtime_arguments(&roots, &mounts), &roots, None)
            .expect("selection-scoped model files should remain independently mountable");
        assert_eq!(validated.models.len(), 132);
    }

    #[test]
    fn compiled_workload_fixture_reaches_helper_validation_with_scoped_receipts() {
        let plan: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/compiled_workload_v2.json"))
                .unwrap();
        assert_eq!(plan["schema_version"], 2);
        assert_eq!(plan["runtime"]["executable"], "/opt/vonk/bin/vllm");
        assert_eq!(
            plan["runtime_image"]["distribution_object"]["kind"],
            "oci-archive"
        );
        assert_eq!(plan["security"]["host_network"], false);

        let (_temp, roots) = runtime_fixture();
        let model_set = roots.agent_data.join("models").join(
            plan["identity"]["model_artifact_set_sha256"]
                .as_str()
                .unwrap(),
        );
        let primary = model_set.join("primary");
        let draft = model_set.join("draft");
        fs::create_dir_all(&primary).unwrap();
        fs::create_dir_all(&draft).unwrap();
        let primary_file = primary.join("config.json");
        let draft_file = draft.join("config.json");
        fs::write(&primary_file, b"same cached receipt").unwrap();
        fs::write(&draft_file, b"same cached receipt").unwrap();
        let mut arguments = runtime_arguments(
            &roots,
            &[
                (primary_file, "/models/primary", true),
                (draft_file, "/models/draft", true),
            ],
        );
        let image = arguments
            .iter_mut()
            .find(|value| value.starts_with("localhost/vonk/recipe-build-"))
            .unwrap();
        *image = format!(
            "localhost/vonk/compiled-runtime-{}@{}",
            plan["runtime_image"]["oci_layout_sha256"].as_str().unwrap(),
            plan["runtime_image"]["image_digest"].as_str().unwrap(),
        );
        let validated = validate_docker_run(&arguments, &roots, None).unwrap();
        assert_eq!(validated.models.len(), 2);
        assert_eq!(
            validated.platform_manifest_digest,
            plan["runtime"]["image_digest"].as_str().unwrap()
        );
        assert_eq!(validated.arguments.last().unwrap(), "/opt/vonk/bin/vllm");
    }

    #[test]
    fn runtime_consumes_post_image_entrypoint_marker_before_docker() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::create_dir_all(&model).unwrap();
        let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
        let image = arguments
            .iter()
            .position(|value| value.starts_with("localhost/vonk/"))
            .unwrap();
        arguments.insert(image + 2, "--once".to_owned());

        let validated = validate_docker_run(&arguments, &roots, None).unwrap();
        assert_eq!(
            validated.arguments[validated.image_index + 1],
            validated.entrypoint
        );
        let docker = validated.docker_arguments().unwrap();
        let image = docker
            .iter()
            .position(|value| value.starts_with("localhost/vonk/"))
            .unwrap();
        assert_eq!(&docker[image + 1..], &["--once"]);
    }

    #[test]
    fn runtime_requires_explicit_none_network_and_entrypoint() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::create_dir_all(&model).unwrap();
        let arguments = runtime_arguments(&roots, &[(model.clone(), "/models", true)]);
        for value in ["bridge", "host", "custom-network"] {
            let mut candidate = arguments.clone();
            let position = candidate.iter().position(|item| item == "none").unwrap();
            candidate[position] = value.to_owned();
            assert!(
                validate_docker_run(&candidate, &roots, None).is_err(),
                "{value}"
            );
        }
        let mut missing = arguments.clone();
        let entrypoint = missing
            .iter()
            .position(|item| item == "--entrypoint")
            .unwrap();
        missing.drain(entrypoint..=entrypoint + 1);
        assert!(validate_docker_run(&missing, &roots, None).is_err());
        let mut mismatched = arguments;
        let command = mismatched
            .iter()
            .position(|item| item == "/opt/vonk/bin/vllm")
            .unwrap();
        mismatched[command] = "/opt/vonk/bin/other".to_owned();
        assert!(validate_docker_run(&mismatched, &roots, None).is_err());
    }

    #[test]
    fn runtime_accepts_signed_bridge_endpoint_and_exact_cdi_gpu() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::create_dir_all(&model).unwrap();
        let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
        let network = arguments.iter().position(|value| value == "none").unwrap();
        arguments[network] = "bridge".to_owned();
        let image = arguments
            .iter()
            .position(|value| value.starts_with("localhost/vonk/"))
            .unwrap();
        arguments.splice(
            image..image,
            [
                "--publish".to_owned(),
                "192.168.1.211:8101:8000".to_owned(),
                "--device".to_owned(),
                "nvidia.com/gpu=all".to_owned(),
                "--env".to_owned(),
                "VONK_LISTEN_PORT=8000".to_owned(),
            ],
        );
        assert!(validate_docker_run(&arguments, &roots, None).is_ok());

        let mut wrong_device = arguments.clone();
        let device = wrong_device
            .iter()
            .position(|value| value == "nvidia.com/gpu=all")
            .unwrap();
        wrong_device[device] = "vendor.example/gpu=all".to_owned();
        assert!(validate_docker_run(&wrong_device, &roots, None).is_err());
        let mut fabric = arguments;
        let device = fabric
            .iter()
            .position(|value| value == "nvidia.com/gpu=all")
            .unwrap();
        fabric[device] = "/dev/infiniband:/dev/infiniband".to_owned();
        assert!(validate_docker_run(&fabric, &roots, None).is_err());
    }

    #[test]
    fn distributed_rank_zero_requires_only_signed_rendezvous_publication() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::create_dir_all(&model).unwrap();
        let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
        let network = arguments.iter().position(|value| value == "none").unwrap();
        arguments[network] = "bridge".to_owned();
        let image = arguments
            .iter()
            .position(|value| value.starts_with("localhost/vonk/"))
            .unwrap();
        arguments.splice(
            image..image,
            [
                "--publish".to_owned(),
                "192.168.1.211:29500:29500".to_owned(),
                "--env".to_owned(),
                "VONK_MASTER_PORT=29500".to_owned(),
                "--env".to_owned(),
                "VONK_RANK=0".to_owned(),
            ],
        );
        assert!(validate_docker_run(&arguments, &roots, None).is_ok());

        let mut wrong_port = arguments.clone();
        let publication = wrong_port
            .iter_mut()
            .find(|value| value.starts_with("192.168.1.211:"))
            .unwrap();
        *publication = "192.168.1.211:29500:29501".to_owned();
        assert!(validate_docker_run(&wrong_port, &roots, None).is_err());
        let mut missing_publication = arguments.clone();
        let publish = missing_publication
            .iter()
            .position(|value| value == "--publish")
            .unwrap();
        missing_publication.drain(publish..=publish + 1);
        assert!(validate_docker_run(&missing_publication, &roots, None).is_err());

        let mut worker_publication = arguments;
        let rank = worker_publication
            .iter()
            .position(|value| value == "VONK_RANK=0")
            .unwrap();
        worker_publication[rank] = "VONK_RANK=1".to_owned();
        let image = worker_publication
            .iter()
            .position(|value| value.starts_with("localhost/vonk/"))
            .unwrap();
        worker_publication.splice(
            image..image,
            [
                "--publish".to_owned(),
                "192.168.1.211:29500:29500".to_owned(),
            ],
        );
        assert!(validate_docker_run(&worker_publication, &roots, None).is_err());
    }

    #[test]
    fn runtime_access_grants_exact_model_output_cache_and_run_tmp_acls() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::create_dir_all(&model).unwrap();
        let arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
        let validated = validate_docker_run(&arguments, &roots, None).unwrap();
        let runner = RecordingAclRunner::default();
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
        executor.prepare_runtime_access(&validated).unwrap();

        let calls = runner.calls.lock().unwrap();
        let paths = calls
            .iter()
            .filter_map(|call| call.last())
            .map(PathBuf::from)
            .collect::<Vec<_>>();
        assert!(paths.iter().any(|path| path.ends_with(
            "models/sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )));
        assert!(paths.iter().any(|path| path.ends_with("outputs")));
        assert!(
            paths
                .iter()
                .any(|path| path.ends_with("installations/installation-1/runtime-cache/home"))
        );
        assert!(
            paths
                .iter()
                .any(|path| path.ends_with(format!("outputs/tmp/{RUN_ID}")))
        );
        assert!(
            paths
                .iter()
                .any(|path| path.ends_with("run-metadata/".to_owned() + RUN_ID + "/runtime.json"))
        );
        assert!(
            calls
                .iter()
                .all(|call| call.iter().all(|value| value != "777" && value != "chown"))
        );
    }

    #[test]
    fn runtime_image_receipt_keeps_registry_archive_config_and_local_reference_distinct() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        fs::create_dir_all(&roots.data).unwrap();
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
        let payload = [0_u8; 17];
        let archive_sha256 = hex_sha256(&payload);
        let registry_manifest = format!("sha256:{}", "b".repeat(64));
        let local_reference =
            format!("localhost/vonk/compiled-runtime-{archive_sha256}@{registry_manifest}");
        let config_id = format!("sha256:{}", "c".repeat(64));
        fs::create_dir_all(roots.agent_data.join("oci-archives")).unwrap();
        let archive = roots.agent_data.join("oci-archives").join(&archive_sha256);
        fs::write(&archive, payload).unwrap();
        fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
        executor
            .write_image_receipt(
                &format!("sha256:{}", "a".repeat(64)),
                &registry_manifest,
                &archive_sha256,
                payload.len() as u64,
                &local_reference,
                &config_id,
            )
            .unwrap();
        executor
            .require_image_receipt(
                &archive_sha256,
                &format!("sha256:{}", "a".repeat(64)),
                &registry_manifest,
                &local_reference,
                &config_id,
            )
            .unwrap();
        for (index, platform, config) in [
            (
                format!("sha256:{}", "b".repeat(64)),
                registry_manifest.clone(),
                config_id.clone(),
            ),
            (
                format!("sha256:{}", "a".repeat(64)),
                format!("sha256:{}", "d".repeat(64)),
                config_id.clone(),
            ),
            (
                format!("sha256:{}", "a".repeat(64)),
                registry_manifest.clone(),
                format!("sha256:{}", "e".repeat(64)),
            ),
        ] {
            assert!(
                executor
                    .require_image_receipt(
                        &archive_sha256,
                        &index,
                        &platform,
                        &local_reference,
                        &config,
                    )
                    .is_err()
            );
        }
        let receipt: RuntimeImageReceipt = serde_json::from_slice(
            &fs::read(roots.runtime_image_receipts.join(&archive_sha256)).unwrap(),
        )
        .unwrap();
        assert_eq!(receipt.archive_sha256, archive_sha256);
        assert_eq!(receipt.archive_bytes, 17);
        assert_eq!(receipt.platform_manifest_digest, registry_manifest);
        assert_eq!(receipt.image_config_id, config_id);
        assert_eq!(receipt.local_image_reference, local_reference);
    }

    #[test]
    fn runtime_image_import_uses_cached_archive_and_writes_bound_receipt() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        fs::create_dir_all(&roots.data).unwrap();
        let payload = b"tiny cached image";
        let archive_sha256 = hex_sha256(payload);
        let registry_manifest = format!("sha256:{}", "b".repeat(64));
        let local_reference =
            format!("localhost/vonk/compiled-runtime-{archive_sha256}@{registry_manifest}");
        let archive_root = roots.agent_data.join("oci-archives");
        fs::create_dir_all(&archive_root).unwrap();
        let archive = archive_root.join(&archive_sha256);
        fs::write(&archive, payload).unwrap();
        fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], RuntimeImportRunner, None).unwrap();
        executor
            .runtime_image_import(&[
                archive.display().to_string(),
                archive_sha256.clone(),
                payload.len().to_string(),
                format!("sha256:{}", "a".repeat(64)),
                registry_manifest.clone(),
                local_reference.clone(),
            ])
            .unwrap();
        executor
            .require_image_receipt(
                &archive_sha256,
                &format!("sha256:{}", "a".repeat(64)),
                &registry_manifest,
                &local_reference,
                &format!("sha256:{}", "c".repeat(64)),
            )
            .unwrap();
        assert!(roots.runtime_image_receipts.join(&archive_sha256).is_file());
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

        let too_many = (0..4097)
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
