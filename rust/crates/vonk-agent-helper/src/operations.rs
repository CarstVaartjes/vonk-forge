use std::collections::{BTreeSet, HashSet};
use std::ffi::{CStr, CString};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::mem::MaybeUninit;
use std::net::Ipv4Addr;
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
use vonk_agent_protocol::generated::{
    CompiledExecutionPlan, ConfirmPackageActivationOperation,
    ExecuteContainerRuntimeRequestOperation, HostHelperProcessLogs, InstallVonkDebOperation,
    RecipeJobRunRequest, RecipeStartPayload, RecipeStopPayload, RestartVonkUnitOperation,
    ScheduleRebootOperation,
};
use vonk_agent_protocol::{
    HostRuntimeAction, HostRuntimeRequest, PackageRollbackAuthority, RecipeReconciliationIdentity,
    RecipeRunObservationOutcome, canonical_json,
    compiled_oci::{
        CompiledOciError, CompiledOciPaths, ExecInvocationLimits, measure_exec_invocation,
        start_arguments_for_paths,
    },
    hex_sha256, parse_strict,
};
use wait_timeout::ChildExt;

use crate::protocol::{ContainerRuntimeAction, HostOperation, RestartUnit, artifact_signing_bytes};

const MAX_ARTIFACT_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_RUNTIME_ARCHIVE_BYTES: u64 = 1024 * 1024 * 1024 * 1024;
const MAX_COMMAND_OUTPUT_BYTES: u64 = 4096;
/// The byte ceiling on one canonical runtime-request document. Owned by the
/// wire contract, not by this helper: the agent enforces the same ceiling
/// before it writes the request file, so the helper's read cannot be stricter
/// than the request the agent was willing to send. A private `64 * 1024`
/// round number here once refused a legitimate many-mount command line with
/// the opaque `helper.unsafe_path`, after a successful install and after the
/// agent had admitted the same bytes.
const MAX_RUNTIME_REQUEST_BYTES: u64 = vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES as u64;
const MAX_RUNTIME_CONFIG_BYTES: usize = 16 * 1024 * 1024;
const MAX_COMPILED_MODEL_FILES: usize = 4096;
const MAX_COMPILED_MODEL_PATH_CHARS: usize = 512;
const MAX_COMPILED_MODEL_BYTES: u64 = 1024 * 1024 * 1024 * 1024;
const INSTALLATION_RECONCILIATION_DIRECTORY: &str = "installation-reconciliation";
const RUNTIME_GENERATION_FENCE_DIRECTORY: &str = "runtime-generation-fences";
const MAX_INSTALLATION_RECONCILIATION_IDENTITY_BYTES: u64 = 16 * 1024;
const MAX_RUNTIME_GENERATION_FENCE_BYTES: u64 = 1024;
const LINUX_ARG_MAX_FLOOR_BYTES: u64 = 128 * 1024;
const LINUX_STACK_LIMIT_BYTES: u64 = 8 * 1024 * 1024;
const LINUX_ARG_MAX_CEILING_BYTES: u64 = LINUX_STACK_LIMIT_BYTES / 4 * 3;
const DOCKER_FIREWALL: &str = "/usr/lib/vonk-forge/vonk-forge-docker-firewall";
const DOCKER_FIREWALL_CONFIG: &str = "/etc/vonk-forge-agent/docker-firewall.conf";
const NATIVE_FABRIC_ROOT: &str = "/sys/class/infiniband";
const RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION: u8 = 2;
const HELPER_COMMAND_ENV: [(&str, &str); 3] = [
    ("LANG", "C.UTF-8"),
    ("LC_ALL", "C.UTF-8"),
    ("PATH", ROOT_COMMAND_PATH),
];

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
    #[error("package activation prerequisites failed")]
    PackagePreflightFailed,
    #[error("package installation failed: {diagnostic}")]
    PackageInstallFailed {
        exit_code: Option<i32>,
        diagnostic: String,
    },
    #[error("compiled command failed")]
    CommandFailed,
    #[error("projected runtime invocation exceeds a host argument limit")]
    RuntimeInvocationLimitExceeded {
        string_limit: bool,
        limit_bytes: u64,
        observed_bytes: u64,
    },
    #[error("host runtime argument limits could not be determined")]
    RuntimeInvocationLimitsUnavailable,
    #[error("runtime image load failed")]
    RuntimeImageLoadFailed,
    #[error("runtime image inspection failed")]
    RuntimeImageInspectFailed,
    #[error("runtime image identity is invalid")]
    RuntimeImageIdentityInvalid,
    #[error("runtime image receipt could not be written")]
    RuntimeImageReceiptFailed,
    /// The exact inspected container had already exited.
    #[error("runtime process exited")]
    RuntimeProcessExited {
        /// The container's own retained output, per stream. Absent when the
        /// log could not be read; the reason is then reported instead.
        logs: Option<Box<HostHelperProcessLogs>>,
        capture_error: Option<&'static str>,
    },
    #[error("exact runtime container is absent")]
    RuntimeRunMissing,
    #[error("native fabric is unavailable or ambiguous")]
    RuntimeFabricUnavailable,
    #[error("native fabric firewall rejected the placement")]
    RuntimeFabricFirewallRejected,
    #[error("one-shot runtime could not be stopped safely")]
    StopUncertain,
    #[error("installation runtime reconciliation is busy")]
    InstallationReconciliationBusy,
    #[error("installation runtime storage is temporarily unavailable")]
    InstallationReconciliationStorageUnavailable,
    #[error("host mutation failed")]
    Io(#[from] std::io::Error),
}

impl OperationError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::InvalidOperation => "helper.operation_invalid",
            Self::UnsafePath => "helper.unsafe_path",
            Self::InvalidArtifact => "helper.artifact_invalid",
            Self::PackageMetadataInvalid => "helper.package_metadata_invalid",
            Self::PackagePreflightFailed => "helper.package_preflight_failed",
            Self::PackageInstallFailed { .. } => "helper.package_install_failed",
            Self::CommandFailed => "helper.command_failed",
            Self::RuntimeInvocationLimitExceeded {
                string_limit: false,
                ..
            } => "helper.runtime_invocation_limit_exceeded",
            Self::RuntimeInvocationLimitExceeded {
                string_limit: true, ..
            } => "helper.runtime_invocation_string_limit_exceeded",
            Self::RuntimeInvocationLimitsUnavailable => {
                "helper.runtime_invocation_limits_unavailable"
            }
            Self::RuntimeImageLoadFailed => "helper.runtime_image_load_failed",
            Self::RuntimeImageInspectFailed => "helper.runtime_image_inspect_failed",
            Self::RuntimeImageIdentityInvalid => "helper.runtime_image_identity_invalid",
            Self::RuntimeImageReceiptFailed => "helper.runtime_image_receipt_failed",
            Self::RuntimeProcessExited { .. } => "helper.runtime_process_exited",
            Self::RuntimeRunMissing => "helper.runtime_run_missing",
            Self::RuntimeFabricUnavailable => "helper.runtime_fabric_unavailable",
            Self::RuntimeFabricFirewallRejected => "helper.runtime_fabric_firewall_rejected",
            Self::StopUncertain => "helper.stop_uncertain",
            Self::InstallationReconciliationBusy => "helper.installation_reconciliation_busy",
            Self::InstallationReconciliationStorageUnavailable => {
                "helper.installation_reconciliation_storage_unavailable"
            }
            Self::Io(_) => "helper.io_failed",
        }
    }

    pub fn safe_detail(&self) -> &'static str {
        match self {
            Self::InvalidOperation => "managed operation is invalid",
            Self::UnsafePath => "managed path is unsafe",
            Self::InvalidArtifact => "artifact verification failed",
            Self::PackageMetadataInvalid => "package metadata verification failed",
            Self::PackagePreflightFailed => "package activation prerequisites failed",
            Self::PackageInstallFailed { .. } => "package installation failed",
            Self::CommandFailed => "compiled command failed",
            Self::RuntimeInvocationLimitExceeded {
                string_limit: false,
                ..
            } => "projected runtime invocation exceeds the host argument limit",
            Self::RuntimeInvocationLimitExceeded {
                string_limit: true, ..
            } => "projected runtime argument exceeds the host per-string limit",
            Self::RuntimeInvocationLimitsUnavailable => {
                "host runtime argument limits could not be determined"
            }
            Self::RuntimeImageLoadFailed => "runtime image load failed",
            Self::RuntimeImageInspectFailed => "runtime image inspection failed",
            Self::RuntimeImageIdentityInvalid => "runtime image identity is invalid",
            Self::RuntimeImageReceiptFailed => "runtime image receipt could not be written",
            Self::RuntimeProcessExited { .. } => "runtime process exited",
            Self::RuntimeRunMissing => "exact runtime container is absent",
            Self::RuntimeFabricUnavailable => "native fabric is unavailable or ambiguous",
            Self::RuntimeFabricFirewallRejected => "native fabric firewall rejected the placement",
            Self::StopUncertain => "one-shot runtime could not be stopped safely",
            Self::InstallationReconciliationBusy => "installation runtime reconciliation is busy",
            Self::InstallationReconciliationStorageUnavailable => {
                "installation runtime storage is temporarily unavailable"
            }
            Self::Io(_) => "host mutation I/O failed",
        }
    }
}

#[derive(Debug, Clone)]
pub struct ManagedRoots {
    pub data: PathBuf,
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
    /// The retained standard error of the same command, kept apart so a
    /// container failure can report both streams instead of one merged tail.
    pub stderr: Vec<u8>,
    pub exit_code: Option<i32>,
}

pub trait CommandRunner: Send + Sync {
    fn arm_package_rollback(
        &self,
        node: &str,
        source: &Path,
        candidate: &Path,
        candidate_sha256: &str,
        authority: &PackageRollbackAuthority,
    ) -> Result<(), String> {
        crate::package_rollback::Store::system().prepare(
            node,
            source,
            candidate,
            candidate_sha256,
            authority,
        )
    }
    fn package_activation_failed(&self) -> Result<(), String> {
        crate::package_rollback::Store::system().activation_failed()
    }

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
            Some("/usr/bin/dpkg-deb" | "/usr/bin/docker" | DOCKER_FIREWALL)
        ) && !(executable == Path::new("/usr/bin/docker")
            && arguments
                .iter()
                .any(|value| value.starts_with("VONK_JOB_TIMEOUT_SECONDS=")));
        // dpkg maintainer scripts and package configuration emit the details
        // needed to diagnose activation failures. Inherit both streams so the
        // service manager records them in its journal. These streams are not
        // consumed by the helper, so they must not be piped into its bounded
        // command-output reader.
        let inherit_output = executable == Path::new("/usr/bin/dpkg");
        let mut command = Command::new(executable);
        command
            .args(arguments)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("PATH", ROOT_COMMAND_PATH)
            .current_dir("/")
            .stdin(Stdio::null())
            .stderr(if inherit_output {
                Stdio::inherit()
            } else {
                Stdio::null()
            })
            .stdout(if inherit_output {
                Stdio::inherit()
            } else if capture_output {
                Stdio::piped()
            } else {
                Stdio::null()
            });
        if inherit_output {
            let result = crate::package_command::run(&mut command, timeout)?;
            return Ok(CommandOutput {
                success: result.status.success() && !result.timed_out,
                stdout: result.diagnostic(),
                stderr: Vec::new(),
                exit_code: result.status.code(),
            });
        }
        if executable == Path::new("/usr/bin/docker")
            && arguments.first().is_some_and(|value| value == "logs")
        {
            let result = crate::package_command::run_quiet(&mut command, timeout)?;
            return Ok(CommandOutput {
                success: result.status.success() && !result.timed_out,
                stdout: result.stdout,
                stderr: result.stderr,
                exit_code: result.status.code(),
            });
        }
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
            stderr: Vec::new(),
            exit_code: status.code(),
        })
    }
}

#[cfg(test)]
mod process_command_runner_tests {
    #[cfg(target_os = "linux")]
    use std::path::Path;
    #[cfg(target_os = "linux")]
    use std::time::Duration;

    use super::ROOT_COMMAND_PATH;
    #[cfg(target_os = "linux")]
    use super::{CommandRunner, ProcessCommandRunner};

    #[test]
    fn privileged_command_path_includes_debian_administrative_binaries() {
        let entries = ROOT_COMMAND_PATH.split(':').collect::<Vec<_>>();
        assert!(entries.contains(&"/usr/sbin"));
        assert!(entries.contains(&"/sbin"));
        assert!(entries.iter().all(|entry| entry.starts_with('/')));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn dpkg_diagnostics_use_service_output_without_bounded_capture() {
        let result = ProcessCommandRunner
            .run_with_timeout(
                Path::new("/usr/bin/dpkg"),
                &["--version".to_owned()],
                Duration::from_secs(5),
            )
            .expect("the Linux test host must provide dpkg");

        assert!(result.success);
        assert!(String::from_utf8_lossy(&result.stdout).contains("Debian"));
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

/// The helper's durable proof that one Controller-authorized archive delivery
/// was imported into one exact local image reference. These identities are
/// deliberately separate: the registry manifest identifies the signed image,
/// the archive digest identifies the enrolled delivery, the stable archive
/// metadata binds that delivery to its retained inode, and the config digest
/// identifies the archive config while the daemon image ID identifies the
/// loaded object. Docker-save and Docker's local store may expose different
/// values for those two identities.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct RuntimeImageReceipt {
    schema_version: u8,
    registry_index_digest: String,
    platform_manifest_digest: String,
    archive_sha256: String,
    archive_bytes: u64,
    archive_identity: RuntimeArchiveIdentity,
    archive_config_id: String,
    image_config_id: String,
    local_image_reference: String,
}

/// Stable metadata for the content-addressed archive after the agent has
/// completed its authenticated delivery.  The helper still checks the live
/// file type, owner, mode, link count, and length on every use; these values
/// bind the receipt to the same inode without rereading its payload.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct RuntimeArchiveIdentity {
    device: u64,
    inode: u64,
    bytes: u64,
    modified_seconds: i64,
    modified_nanoseconds: i64,
    changed_seconds: i64,
    changed_nanoseconds: i64,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "PascalCase")]
struct DockerSaveManifestEntry {
    config: String,
}

#[derive(Clone, Copy)]
struct RuntimeRequestGrantBinding<'a> {
    job_id: &'a uuid::Uuid,
    operation_id: &'a uuid::Uuid,
    attempt: u32,
    fence: &'a uuid::Uuid,
    installation_id: Option<&'a uuid::Uuid>,
    reconciliation_identity: Option<&'a RecipeReconciliationIdentity>,
    start_plan_sha256: Option<&'a str>,
    stop_plan_sha256: Option<&'a str>,
    run_generation: Option<u32>,
    runtime_run_id: Option<&'a uuid::Uuid>,
    runtime_target_id: Option<&'a uuid::Uuid>,
    runtime_installation_id: Option<&'a uuid::Uuid>,
}

enum AuthorizedRuntimeEffect {
    Start {
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: String,
    },
    Stop {
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: String,
        stop_timeout_seconds: u16,
        cancel_pending_start: bool,
    },
}

enum RuntimeStartLaunch {
    Service,
    Job { timeout_seconds: u16 },
    TimedOut,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct InstallationReconciliationReceipt {
    schema_version: u8,
    identity: RecipeReconciliationIdentity,
    installation_device: u64,
    installation_inode: u64,
}

const INSTALLATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION: u8 = 2;
const RUNTIME_GENERATION_FENCE_SCHEMA_VERSION: u8 = 2;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct RuntimeGenerationFence {
    schema_version: u8,
    installation_id: uuid::Uuid,
    runtime_id: uuid::Uuid,
    highest_generation: u32,
    cancelled: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct RuntimeEffectIdentity {
    runtime_id: uuid::Uuid,
    installation_id: uuid::Uuid,
    run_generation: u32,
}

#[derive(Clone, Copy)]
enum RuntimeGenerationFenceUse {
    Start,
    Stop { cancel_pending_start: bool },
}

#[derive(Default)]
struct JobCancellationState {
    active_starts: HashSet<RuntimeEffectIdentity>,
    cancelled: HashSet<RuntimeEffectIdentity>,
}

#[derive(Default)]
struct JobCancellationFence {
    state: Mutex<JobCancellationState>,
}

struct ActiveJobStart<'a> {
    fence: &'a JobCancellationFence,
    identity: RuntimeEffectIdentity,
}

impl JobCancellationFence {
    fn begin(&self, identity: RuntimeEffectIdentity) -> Result<ActiveJobStart<'_>, OperationError> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?;
        if state.cancelled.contains(&identity) || !state.active_starts.insert(identity) {
            return Err(OperationError::CommandFailed);
        }
        Ok(ActiveJobStart {
            fence: self,
            identity,
        })
    }

    fn cancel(&self, identity: RuntimeEffectIdentity) -> Result<(), OperationError> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?;
        if state.active_starts.contains(&identity) {
            state.cancelled.insert(identity);
        }
        Ok(())
    }

    fn is_active(&self, identity: RuntimeEffectIdentity) -> Result<bool, OperationError> {
        Ok(self
            .state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?
            .active_starts
            .contains(&identity))
    }

    fn was_cancelled(&self, identity: RuntimeEffectIdentity) -> Result<bool, OperationError> {
        Ok(self
            .state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?
            .cancelled
            .contains(&identity))
    }

    fn wait_for_active_start(
        &self,
        identity: RuntimeEffectIdentity,
        deadline: Instant,
    ) -> Result<(), OperationError> {
        while self.is_active(identity)? {
            if Instant::now() >= deadline {
                return Err(OperationError::StopUncertain);
            }
            thread::sleep(Duration::from_millis(50));
        }
        Ok(())
    }
}

impl Drop for ActiveJobStart<'_> {
    fn drop(&mut self) {
        if let Ok(mut state) = self.fence.state.lock() {
            state.active_starts.remove(&self.identity);
            state.cancelled.remove(&self.identity);
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
        if !roots.data.is_absolute() || !roots.incoming.starts_with(&roots.data) {
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
            HostOperation::InstallVonkDebOperation(InstallVonkDebOperation {
                package_sha256,
                package_signature,
                rollback,
                ..
            }) => {
                self.install_package(
                    package_sha256,
                    package_signature,
                    rollback,
                    observation_node_id.ok_or(OperationError::InvalidOperation)?,
                )?;
                ("package-installed", package_sha256.clone(), None, None)
            }
            HostOperation::ConfirmPackageActivationOperation(
                ConfirmPackageActivationOperation {
                    package_sha256,
                    attempt_nonce,
                    ..
                },
            ) => {
                crate::package_rollback::Store::system()
                    .acknowledge(
                        observation_node_id.ok_or(OperationError::InvalidOperation)?,
                        package_sha256,
                        attempt_nonce,
                    )
                    .map_err(|_| OperationError::PackagePreflightFailed)?;
                (
                    "package-activation-confirmed",
                    package_sha256.clone(),
                    None,
                    None,
                )
            }
            HostOperation::RestartVonkUnitOperation(RestartVonkUnitOperation { unit, .. }) => {
                let unit_name = self.restart_unit(unit)?;
                ("unit-restarted", unit_name.to_owned(), None, None)
            }
            HostOperation::ScheduleRebootOperation(ScheduleRebootOperation {
                delay_seconds,
                ..
            }) => {
                self.schedule_reboot(*delay_seconds)?;
                ("reboot-scheduled", delay_seconds.to_string(), None, None)
            }
            HostOperation::ExecuteContainerRuntimeRequestOperation(
                ExecuteContainerRuntimeRequestOperation {
                    action,
                    job_id,
                    operation_id,
                    attempt,
                    fence,
                    request_sha256,
                    observation_identity_sha256,
                    installation_id,
                    reconciliation_identity,
                    start_plan_sha256,
                    stop_plan_sha256,
                    run_generation,
                    runtime_run_id,
                    runtime_target_id,
                    runtime_installation_id,
                    ..
                },
            ) => {
                let outcome = self.execute_runtime_request(
                    action,
                    RuntimeRequestGrantBinding {
                        job_id,
                        operation_id,
                        attempt: *attempt,
                        fence,
                        installation_id: installation_id.as_ref(),
                        reconciliation_identity: reconciliation_identity.as_ref(),
                        start_plan_sha256: start_plan_sha256.as_deref(),
                        stop_plan_sha256: stop_plan_sha256.as_deref(),
                        run_generation: *run_generation,
                        runtime_run_id: runtime_run_id.as_ref(),
                        runtime_target_id: runtime_target_id.as_ref(),
                        runtime_installation_id: runtime_installation_id.as_ref(),
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

    fn install_package(
        &self,
        digest: &str,
        detached_signature: &str,
        rollback: &PackageRollbackAuthority,
        node_id: &str,
    ) -> Result<(), OperationError> {
        let _install_guard = self
            .package_install
            .lock()
            .map_err(|_| OperationError::CommandFailed)?;
        require_safe_directory(&self.roots.incoming, self.package_owner_uid)?;
        let incoming = self.roots.incoming.join(format!("{digest}.deb"));
        let package = self.take_package_custody(&incoming, digest, detached_signature)?;
        let source = self.take_package_custody(
            &self
                .roots
                .incoming
                .join(format!("{}.deb", rollback.source.package_sha256)),
            &rollback.source.package_sha256,
            &rollback.source.package_signature,
        )?;
        self.runner
            .arm_package_rollback(node_id, source.path(), package.path(), digest, rollback)
            .map_err(|_| OperationError::PackagePreflightFailed)?;
        source.cleanup()?;
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
            .map_err(|diagnostic| OperationError::PackageInstallFailed {
                exit_code: None,
                diagnostic,
            })?;
        if !result.success {
            let _ = self.runner.package_activation_failed();
            return Err(OperationError::PackageInstallFailed {
                exit_code: result.exit_code,
                diagnostic: String::from_utf8_lossy(&result.stdout).into_owned(),
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

    fn schedule_reboot(&self, delay_seconds: u32) -> Result<(), OperationError> {
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
            ContainerRuntimeAction::RuntimePreflight => HostRuntimeAction::RuntimePreflight,
            ContainerRuntimeAction::ImageImport => HostRuntimeAction::ImageImport,
            ContainerRuntimeAction::ImageInspect => HostRuntimeAction::ImageInspect,
            ContainerRuntimeAction::RunInspect => HostRuntimeAction::RunInspect,
            ContainerRuntimeAction::Start => HostRuntimeAction::Start,
            ContainerRuntimeAction::Stop => HostRuntimeAction::Stop,
            ContainerRuntimeAction::InstallationCleanup => HostRuntimeAction::InstallationCleanup,
        };
        if request.action != expected_action
            || &request.job_id != binding.job_id
            || &request.operation_id != binding.operation_id
            || request.attempt != binding.attempt
            || &request.fence != binding.fence
            || request.installation_id.as_ref() != binding.installation_id
            || request.reconciliation_identity.as_ref() != binding.reconciliation_identity
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
        let authorized_effect =
            self.authorize_runtime_effect(&request, binding, observation_node_id)?;
        match request.action {
            HostRuntimeAction::RuntimePreflight => {
                let code = crate::runtime_preflight::run(
                    Path::new("/var/lib/vonk-forge"),
                    Path::new(crate::runtime_preflight::PROBE_BINARY),
                    |arguments, timeout| {
                        self.run_docker_with_timeout(arguments, timeout)
                            .map(|output| (output.success, output.stdout))
                            .map_err(|_| std::io::Error::other("preflight runtime unavailable"))
                    },
                )
                .map_err(|_| OperationError::CommandFailed)?;
                Ok(RuntimeRequestOutcome {
                    exit_code: Some(code),
                    recipe_run_observation: None,
                })
            }
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
                let running =
                    self.runtime_run_inspect(&request.arguments, request.observation.is_none())?;
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
                let Some(AuthorizedRuntimeEffect::Start {
                    identity,
                    logical_run_id,
                    plan_digest,
                }) = authorized_effect
                else {
                    return Err(OperationError::InvalidOperation);
                };
                self.runtime_start_authorized(
                    &request.arguments,
                    identity,
                    logical_run_id,
                    &plan_digest,
                )
                .map(|exit_code| RuntimeRequestOutcome {
                    exit_code,
                    recipe_run_observation: None,
                })
            }
            HostRuntimeAction::Stop => {
                let Some(AuthorizedRuntimeEffect::Stop {
                    identity,
                    logical_run_id,
                    plan_digest,
                    stop_timeout_seconds,
                    cancel_pending_start,
                }) = authorized_effect
                else {
                    return Err(OperationError::InvalidOperation);
                };
                self.runtime_stop_authorized(
                    identity,
                    logical_run_id,
                    &plan_digest,
                    stop_timeout_seconds,
                    cancel_pending_start,
                )
                .map(|()| RuntimeRequestOutcome {
                    exit_code: None,
                    recipe_run_observation: None,
                })
            }
            HostRuntimeAction::InstallationCleanup => {
                let installation_id = request
                    .installation_id
                    .as_ref()
                    .ok_or(OperationError::InvalidOperation)?;
                if let Some(identity) = request.reconciliation_identity.as_ref() {
                    if identity.installation_id != *installation_id
                        || Some(identity.node_id.as_str()) != observation_node_id
                    {
                        return Err(OperationError::InvalidOperation);
                    }
                    self.runtime_reconcile_installation(identity)?;
                } else {
                    self.runtime_installation_cleanup(&installation_id.to_string())?;
                }
                Ok(RuntimeRequestOutcome {
                    exit_code: None,
                    recipe_run_observation: None,
                })
            }
        }
    }

    fn authorize_runtime_effect(
        &self,
        request: &HostRuntimeRequest,
        grant: RuntimeRequestGrantBinding<'_>,
        node_id: Option<&str>,
    ) -> Result<Option<AuthorizedRuntimeEffect>, OperationError> {
        match request.action {
            HostRuntimeAction::Start => {
                if request.installation_id.is_some()
                    || request.reconciliation_identity.is_some()
                    || request.observation.is_some()
                    || request.stop_plan.is_some()
                    || request.run_generation.is_none()
                    || (request.start_plan.is_some() == request.job_plan.is_some())
                    || grant.start_plan_sha256.is_none()
                    || grant.stop_plan_sha256.is_some()
                {
                    return Err(OperationError::InvalidOperation);
                }
                let (plan, compiled, logical_run_id, target_id, installation_id, generation) =
                    if let Some(plan) = request.start_plan.as_ref() {
                        validate_runtime_start_plan(plan)?;
                        if plan.compiled_execution_plan.job.is_some() {
                            return Err(OperationError::InvalidOperation);
                        }
                        (
                            canonical_json(plan).map_err(|_| OperationError::InvalidOperation)?,
                            &plan.compiled_execution_plan,
                            plan.run_id,
                            plan.run_id,
                            plan.installation_id,
                            plan.run_generation,
                        )
                    } else {
                        let plan = request
                            .job_plan
                            .as_ref()
                            .ok_or(OperationError::InvalidOperation)?;
                        validate_runtime_job_plan(plan)?;
                        (
                            canonical_json(plan).map_err(|_| OperationError::InvalidOperation)?,
                            &plan.compiled_execution_plan,
                            plan.run_id,
                            plan.job_id,
                            plan.installation_id,
                            plan.run_generation,
                        )
                    };
                let grant_target = grant
                    .runtime_target_id
                    .ok_or(OperationError::InvalidOperation)?;
                if generation == 0
                    || generation > i32::MAX as u32
                    || request.run_generation != Some(generation)
                    || grant.run_generation != Some(generation)
                    || grant.runtime_run_id != Some(&logical_run_id)
                    || grant_target != &target_id
                    || grant.runtime_installation_id != Some(&installation_id)
                    || grant.installation_id.is_some()
                    || grant.reconciliation_identity.is_some()
                    || grant.stop_plan_sha256.is_some()
                    || !grant
                        .start_plan_sha256
                        .is_some_and(|digest| lower_hex(digest, 64) && hex_sha256(&plan) == digest)
                    || !valid_oci_digest(&compiled.runtime_image.image_digest)
                    || !compiled.lifecycle.pre_start.is_empty()
                {
                    return Err(OperationError::InvalidOperation);
                }
                let expected_arguments =
                    self.projected_runtime_arguments(compiled, installation_id, target_id)?;
                if request.arguments != expected_arguments {
                    return Err(OperationError::InvalidOperation);
                }
                Ok(Some(AuthorizedRuntimeEffect::Start {
                    identity: RuntimeEffectIdentity {
                        runtime_id: target_id,
                        installation_id,
                        run_generation: generation,
                    },
                    logical_run_id,
                    plan_digest: if let Some(plan) = request.start_plan.as_ref() {
                        plan.plan_digest.clone()
                    } else {
                        request
                            .job_plan
                            .as_ref()
                            .ok_or(OperationError::InvalidOperation)?
                            .plan_digest
                            .clone()
                    },
                }))
            }
            HostRuntimeAction::Stop => {
                let plan = request
                    .stop_plan
                    .as_ref()
                    .ok_or(OperationError::InvalidOperation)?;
                validate_runtime_stop_plan(plan)?;
                if request.arguments.len() != 0
                    || request.installation_id.is_some()
                    || request.reconciliation_identity.is_some()
                    || request.start_plan.is_some()
                    || request.job_plan.is_some()
                    || request.observation.is_some()
                    || request.run_generation != Some(plan.run_generation)
                    || grant.run_generation != Some(plan.run_generation)
                    || grant.runtime_run_id != Some(&plan.run_id)
                    || grant.runtime_target_id != Some(&plan.target_runtime_id)
                    || grant.runtime_installation_id != Some(&plan.installation_id)
                    || grant.installation_id.is_some()
                    || grant.reconciliation_identity.is_some()
                    || grant.start_plan_sha256.is_some()
                    || !grant.stop_plan_sha256.is_some_and(|digest| {
                        lower_hex(digest, 64)
                            && canonical_json(plan)
                                .ok()
                                .is_some_and(|encoded| hex_sha256(&encoded) == digest)
                    })
                    || plan.node_id != node_id.ok_or(OperationError::InvalidOperation)?
                    || (plan.compiled_execution_plan.job.is_none()
                        && plan.target_runtime_id != plan.run_id)
                    || !plan.compiled_execution_plan.lifecycle.post_stop.is_empty()
                {
                    return Err(OperationError::InvalidOperation);
                }
                let stop_timeout_seconds =
                    u16::try_from(plan.compiled_execution_plan.lifecycle.stop_timeout_seconds)
                        .ok()
                        .filter(|seconds| (1..=600).contains(seconds))
                        .ok_or(OperationError::InvalidOperation)?;
                Ok(Some(AuthorizedRuntimeEffect::Stop {
                    identity: RuntimeEffectIdentity {
                        runtime_id: plan.target_runtime_id,
                        installation_id: plan.installation_id,
                        run_generation: plan.run_generation,
                    },
                    logical_run_id: plan.run_id,
                    plan_digest: plan.plan_digest.clone(),
                    stop_timeout_seconds,
                    cancel_pending_start: plan.cancel_pending_start,
                }))
            }
            _ => {
                if request.start_plan.is_some()
                    || request.job_plan.is_some()
                    || request.stop_plan.is_some()
                    || request.run_generation.is_some()
                    || grant.start_plan_sha256.is_some()
                    || grant.stop_plan_sha256.is_some()
                    || grant.run_generation.is_some()
                    || grant.runtime_run_id.is_some()
                    || grant.runtime_target_id.is_some()
                    || grant.runtime_installation_id.is_some()
                {
                    return Err(OperationError::InvalidOperation);
                }
                Ok(None)
            }
        }
    }

    fn projected_runtime_arguments(
        &self,
        plan: &CompiledExecutionPlan,
        installation_id: uuid::Uuid,
        target_runtime_id: uuid::Uuid,
    ) -> Result<Vec<String>, OperationError> {
        plan.validate()
            .map_err(|_| OperationError::InvalidOperation)?;
        let installation = installation_id.to_string();
        let target = target_runtime_id.to_string();
        let run_root = self.roots.agent_data.join("runs").join(&target);
        let main = start_arguments_for_paths(
            plan,
            &CompiledOciPaths {
                image_archive: self
                    .roots
                    .agent_data
                    .join("oci-archives")
                    .join(&plan.runtime_image.oci_layout_sha256),
                model_root: self
                    .roots
                    .agent_data
                    .join("installations")
                    .join(&installation)
                    .join("models"),
                input_root: plan.job.as_ref().map(|_| run_root.join("inputs")),
                output_root: run_root.join("outputs"),
                cache_root: self
                    .roots
                    .agent_data
                    .join("installations")
                    .join(&installation)
                    .join("runtime-cache"),
                runtime_spec: self
                    .roots
                    .agent_data
                    .join("run-metadata")
                    .join(&target)
                    .join("runtime.json"),
            },
            &target,
        )
        .map_err(|_| OperationError::InvalidOperation)?;
        Ok(runtime_plan_prefix(plan, main))
    }

    fn runtime_installation_cleanup(&self, installation_id: &str) -> Result<(), OperationError> {
        self.runtime_installation_cleanup_bound(installation_id, None)
    }

    fn runtime_installation_cleanup_bound(
        &self,
        installation_id: &str,
        expected_installation: Option<(u64, u64)>,
    ) -> Result<(), OperationError> {
        if !valid_artifact_id(installation_id) {
            return Err(OperationError::InvalidOperation);
        }
        let directory_flags = rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC;
        let agent_data = OpenOptions::new()
            .read(true)
            .custom_flags(
                (rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::NOFOLLOW).bits() as i32,
            )
            .open(&self.roots.agent_data)?;
        let installations = match rustix::fs::openat(
            &agent_data,
            "installations",
            directory_flags,
            rustix::fs::Mode::empty(),
        ) {
            Ok(installations) => installations,
            Err(rustix::io::Errno::NOENT) if expected_installation.is_none() => return Ok(()),
            Err(rustix::io::Errno::NOENT) => return Err(OperationError::InvalidArtifact),
            Err(error) => return Err(errno_io(error).into()),
        };
        let installation = match rustix::fs::openat(
            &installations,
            installation_id,
            directory_flags,
            rustix::fs::Mode::empty(),
        ) {
            Ok(installation) => installation,
            Err(rustix::io::Errno::NOENT) if expected_installation.is_none() => return Ok(()),
            Err(rustix::io::Errno::NOENT) => return Err(OperationError::InvalidArtifact),
            Err(error) => return Err(errno_io(error).into()),
        };
        if let Some((device, inode)) = expected_installation {
            let metadata = rustix::fs::fstat(&installation).map_err(errno_io)?;
            if (metadata.st_dev, metadata.st_ino) != (device, inode) {
                return Err(OperationError::InvalidArtifact);
            }
        }
        let cache = match rustix::fs::openat(
            &installation,
            "runtime-cache",
            directory_flags,
            rustix::fs::Mode::empty(),
        ) {
            Ok(cache) => cache,
            Err(rustix::io::Errno::NOENT) => return Ok(()),
            Err(error) => return Err(errno_io(error).into()),
        };
        let cache_device = rustix::fs::fstat(&cache).map_err(errno_io)?.st_dev;
        remove_directory_contents(&cache, cache_device)?;
        rustix::fs::unlinkat(
            &installation,
            "runtime-cache",
            rustix::fs::AtFlags::REMOVEDIR,
        )
        .map_err(errno_io)?;
        Ok(())
    }

    fn runtime_reconcile_installation(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<(), OperationError> {
        self.runtime_reconcile_installation_inner(identity)
            .map_err(|error| match error {
                OperationError::Io(error) if retryable_reconciliation_storage_io(&error) => {
                    OperationError::InstallationReconciliationStorageUnavailable
                }
                // Permission failures, missing identity and other unexpected
                // I/O refusals stay terminal rather than masquerading as a
                // wait that could become successful after retry.
                OperationError::Io(_) => OperationError::UnsafePath,
                error => error,
            })
    }

    fn runtime_reconcile_installation_inner(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<(), OperationError> {
        identity
            .validate()
            .map_err(|_| OperationError::InvalidOperation)?;
        let installation_id = identity.installation_id.to_string();
        let _lock = self.lock_installation_runtime(&installation_id)?;
        let root = self.installation_reconciliation_root()?;
        let receipt_path = root.join(format!("{installation_id}.json"));
        let existing = read_helper_reconciliation_receipt(
            &receipt_path,
            Some(rustix::process::geteuid().as_raw()),
        )?;
        self.require_no_unclassified_installation_runtime(&installation_id)?;
        if let Some(receipt) = existing {
            if receipt.schema_version != INSTALLATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION
                || receipt.identity != *identity
            {
                return Err(OperationError::InvalidArtifact);
            }
            let installation = self
                .roots
                .agent_data
                .join("installations")
                .join(&installation_id);
            match self.validate_reconciliation_installation_metadata(identity) {
                Ok((device, inode))
                    if (device, inode)
                        == (receipt.installation_device, receipt.installation_inode) =>
                {
                    match fs::symlink_metadata(installation.join("runtime-cache")) {
                        Ok(_) => return Err(OperationError::InvalidArtifact),
                        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                        Err(error) => return Err(OperationError::Io(error)),
                    }
                }
                Err(OperationError::UnsafePath)
                    if matches!(
                        fs::symlink_metadata(&installation),
                        Err(ref error) if error.kind() == std::io::ErrorKind::NotFound
                    ) => {}
                _ => return Err(OperationError::InvalidArtifact),
            }
        } else {
            let (installation_device, installation_inode) =
                self.validate_reconciliation_installation_metadata(identity)?;
            // The helper owns the root-only runtime-cache ACL. Clear only this
            // private installation cache under the same start/reconcile fence,
            // before publishing the durable runtime tombstone. Shared model
            // cache data lives outside `installations` and is preserved.
            self.runtime_installation_cleanup_bound(
                &installation_id,
                Some((installation_device, installation_inode)),
            )?;
            write_helper_reconciliation_receipt(
                &root,
                &receipt_path,
                &InstallationReconciliationReceipt {
                    schema_version: INSTALLATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION,
                    identity: identity.clone(),
                    installation_device,
                    installation_inode,
                },
            )?;
        }
        Ok(())
    }

    fn validate_reconciliation_installation_metadata(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<(u64, u64), OperationError> {
        let installation_id = identity.installation_id.to_string();
        let installations = self.roots.agent_data.join("installations");
        let installation = installations.join(&installation_id);
        require_safe_directory(&self.roots.agent_data, self.runtime_request_owner_uid)?;
        require_safe_directory(&installations, self.runtime_request_owner_uid)?;
        let (installation_owner, installation_group) =
            require_exact_directory(&installation, self.runtime_request_owner_uid, 0o700)?;
        let installation_metadata =
            fs::symlink_metadata(&installation).map_err(|_| OperationError::UnsafePath)?;
        if installation_metadata.uid() != installation_owner
            || installation_metadata.gid() != installation_group
        {
            return Err(OperationError::UnsafePath);
        }
        let canonical_agent_data = self
            .roots
            .agent_data
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        let canonical_installations = installations
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        let canonical_installation = installation
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        if canonical_installations.parent() != Some(canonical_agent_data.as_path())
            || canonical_installation.parent() != Some(canonical_installations.as_path())
        {
            return Err(OperationError::UnsafePath);
        }

        let spec_path = installation.join("spec.json");
        let spec_bytes = read_agent_installation_file(
            &spec_path,
            self.runtime_request_owner_uid,
            vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES as u64,
        )?;
        let spec: serde_json::Value =
            serde_json::from_slice(&spec_bytes).map_err(|_| OperationError::InvalidArtifact)?;
        let canonical_spec = canonical_json(&spec).map_err(|_| OperationError::InvalidArtifact)?;
        if hex_sha256(&canonical_spec) != identity.compiled_spec_canonical_sha256 {
            return Err(OperationError::InvalidArtifact);
        }
        let embedded_digest = spec
            .get("identity")
            .and_then(|value| value.get("recipe_revision_sha256"))
            .and_then(serde_json::Value::as_str);
        let recipe_bytes = read_agent_installation_file(
            &installation.join("recipe-content.sha256"),
            self.runtime_request_owner_uid,
            64,
        )?;
        let recipe_digest =
            std::str::from_utf8(&recipe_bytes).map_err(|_| OperationError::InvalidArtifact)?;
        if recipe_digest != identity.recipe_content_sha256
            || embedded_digest != Some(identity.recipe_content_sha256.as_str())
        {
            return Err(OperationError::InvalidArtifact);
        }
        Ok((installation_metadata.dev(), installation_metadata.ino()))
    }

    fn installation_reconciliation_root(&self) -> Result<PathBuf, OperationError> {
        let root = self.roots.data.join(INSTALLATION_RECONCILIATION_DIRECTORY);
        ensure_runtime_directory(&root)?;
        require_exact_directory(&root, Some(rustix::process::geteuid().as_raw()), 0o700)?;
        Ok(root)
    }

    fn runtime_generation_fence_root(&self) -> Result<PathBuf, OperationError> {
        let root = self.roots.data.join(RUNTIME_GENERATION_FENCE_DIRECTORY);
        let created = match fs::symlink_metadata(&root) {
            Ok(_) => false,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => true,
            Err(error) => return Err(error.into()),
        };
        ensure_runtime_directory(&root)?;
        require_exact_directory(&root, Some(rustix::process::geteuid().as_raw()), 0o700)?;
        if created {
            let parent = root.parent().ok_or(OperationError::UnsafePath)?;
            sync_directory(parent)?;
        }
        Ok(root)
    }

    fn read_runtime_generation_fence(
        &self,
        installation_id: uuid::Uuid,
        runtime_id: uuid::Uuid,
    ) -> Result<Option<RuntimeGenerationFence>, OperationError> {
        let root = self.runtime_generation_fence_root()?;
        let path = root.join(runtime_generation_fence_filename(
            installation_id,
            runtime_id,
        ));
        let path_metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if path_metadata.file_type().is_symlink()
            || !path_metadata.is_file()
            || path_metadata.nlink() != 1
            || path_metadata.len() == 0
            || path_metadata.len() > MAX_RUNTIME_GENERATION_FENCE_BYTES
            || path_metadata.uid() != rustix::process::geteuid().as_raw()
            || path_metadata.mode() & 0o777 != 0o600
        {
            return Err(OperationError::InvalidArtifact);
        }
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(&path)?;
        let opened = file.metadata()?;
        if artifact_identity(&opened) != artifact_identity(&path_metadata) {
            return Err(OperationError::InvalidArtifact);
        }
        let mut bytes = Vec::with_capacity(path_metadata.len() as usize);
        Read::by_ref(&mut file)
            .take(MAX_RUNTIME_GENERATION_FENCE_BYTES + 1)
            .read_to_end(&mut bytes)?;
        let after = file.metadata()?;
        if bytes.len() as u64 != path_metadata.len()
            || artifact_identity(&after) != artifact_identity(&path_metadata)
        {
            return Err(OperationError::InvalidArtifact);
        }
        let fence: RuntimeGenerationFence =
            parse_strict(&bytes).map_err(|_| OperationError::InvalidArtifact)?;
        if fence.schema_version != RUNTIME_GENERATION_FENCE_SCHEMA_VERSION
            || fence.installation_id != installation_id
            || fence.runtime_id != runtime_id
            || fence.highest_generation == 0
            || fence.highest_generation > i32::MAX as u32
            || canonical_json(&fence).map_err(|_| OperationError::InvalidArtifact)? != bytes
        {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(Some(fence))
    }

    fn write_runtime_generation_fence(
        &self,
        fence: &RuntimeGenerationFence,
    ) -> Result<(), OperationError> {
        let root = self.runtime_generation_fence_root()?;
        let path = root.join(runtime_generation_fence_filename(
            fence.installation_id,
            fence.runtime_id,
        ));
        let bytes = canonical_json(fence).map_err(|_| OperationError::InvalidOperation)?;
        if fence.schema_version != RUNTIME_GENERATION_FENCE_SCHEMA_VERSION
            || fence.highest_generation == 0
            || fence.highest_generation > i32::MAX as u32
            || bytes.len() as u64 > MAX_RUNTIME_GENERATION_FENCE_BYTES
        {
            return Err(OperationError::InvalidOperation);
        }
        let temporary = root.join(format!(
            ".runtime-generation-fence-{}.tmp",
            uuid::Uuid::new_v4()
        ));
        let result = (|| {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
                .mode(0o600)
                .open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temporary, &path)?;
            sync_directory(&root)
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }

    fn update_runtime_generation_fence(
        &self,
        identity: &RuntimeEffectIdentity,
        use_kind: RuntimeGenerationFenceUse,
    ) -> Result<(), OperationError> {
        if identity.run_generation == 0 || identity.run_generation > i32::MAX as u32 {
            return Err(OperationError::InvalidOperation);
        }
        let current =
            self.read_runtime_generation_fence(identity.installation_id, identity.runtime_id)?;
        if let Some(mut current) = current {
            if identity.run_generation < current.highest_generation {
                return match use_kind {
                    // An exact older Stop may clean up the container whose
                    // labels carry this generation. It cannot rewind the
                    // high-water mark, so a delayed older Start remains
                    // fenced after restart.
                    RuntimeGenerationFenceUse::Stop { .. } => Ok(()),
                    RuntimeGenerationFenceUse::Start => Err(OperationError::InvalidOperation),
                };
            }
            if identity.run_generation == current.highest_generation {
                match use_kind {
                    RuntimeGenerationFenceUse::Start if current.cancelled => {
                        return Err(OperationError::InvalidOperation);
                    }
                    RuntimeGenerationFenceUse::Stop {
                        cancel_pending_start: true,
                    } if !current.cancelled => {
                        current.cancelled = true;
                    }
                    RuntimeGenerationFenceUse::Start | RuntimeGenerationFenceUse::Stop { .. } => {
                        return Ok(());
                    }
                }
            } else {
                current.highest_generation = identity.run_generation;
                current.cancelled = matches!(
                    use_kind,
                    RuntimeGenerationFenceUse::Stop {
                        cancel_pending_start: true
                    }
                );
            }
            self.write_runtime_generation_fence(&current)
        } else {
            self.write_runtime_generation_fence(&RuntimeGenerationFence {
                schema_version: RUNTIME_GENERATION_FENCE_SCHEMA_VERSION,
                installation_id: identity.installation_id,
                runtime_id: identity.runtime_id,
                highest_generation: identity.run_generation,
                cancelled: matches!(
                    use_kind,
                    RuntimeGenerationFenceUse::Stop {
                        cancel_pending_start: true
                    }
                ),
            })
        }
    }

    fn lock_installation_runtime(&self, installation_id: &str) -> Result<File, OperationError> {
        if !valid_artifact_id(installation_id) {
            return Err(OperationError::InvalidOperation);
        }
        let root = self.installation_reconciliation_root()?;
        let lock_path = root.join(format!("{installation_id}.lock"));
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .mode(0o600)
            .open(lock_path)?;
        let metadata = lock.metadata()?;
        if !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.uid() != rustix::process::geteuid().as_raw()
            || metadata.mode() & 0o777 != 0o600
        {
            return Err(OperationError::UnsafePath);
        }
        match rustix::fs::flock(&lock, rustix::fs::FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => Ok(lock),
            Err(error)
                if error == rustix::io::Errno::AGAIN || error == rustix::io::Errno::WOULDBLOCK =>
            {
                Err(OperationError::InstallationReconciliationBusy)
            }
            Err(error) => Err(errno_io(error).into()),
        }
    }

    fn refuse_reconciled_runtime(&self, installation_id: &str) -> Result<(), OperationError> {
        let root = self.installation_reconciliation_root()?;
        let path = root.join(format!("{installation_id}.json"));
        if read_helper_reconciliation_receipt(&path, Some(rustix::process::geteuid().as_raw()))?
            .is_some()
        {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(())
    }

    fn require_no_unclassified_installation_runtime(
        &self,
        installation_id: &str,
    ) -> Result<(), OperationError> {
        let output = self.run_docker(&[
            "container".to_owned(),
            "ls".to_owned(),
            "--all".to_owned(),
            "--no-trunc".to_owned(),
            "--format".to_owned(),
            "{{.ID}}\t{{.State}}\t{{.Names}}\t{{.Label \"ai.vonkforge.managed\"}}\t{{.Label \"ai.vonkforge.installation-id\"}}".to_owned(),
        ])?;
        if !output.success || output.stdout.len() as u64 > MAX_COMMAND_OUTPUT_BYTES {
            return Err(OperationError::CommandFailed);
        }
        let rows =
            std::str::from_utf8(&output.stdout).map_err(|_| OperationError::InvalidArtifact)?;
        for row in rows.lines().filter(|row| !row.trim().is_empty()) {
            let fields = row.split('\t').collect::<Vec<_>>();
            if fields.len() != 5
                || !lower_hex(fields[0], 64)
                || !matches!(
                    fields[1],
                    "created"
                        | "running"
                        | "restarting"
                        | "paused"
                        | "exited"
                        | "dead"
                        | "removing"
                )
            {
                return Err(OperationError::InvalidArtifact);
            }
            let is_vonk_named = fields[2].split(',').any(|name| name.starts_with("vonk-"));
            let managed = fields[3] == "true";
            if !managed && is_vonk_named {
                // An older or damaged container can retain its Vonk name while
                // losing the labels that bind it to an installation. Do not
                // infer that it is unrelated just because the target label is
                // missing.
                return Err(OperationError::InvalidArtifact);
            }
            if !managed {
                continue;
            }
            let found_installation = uuid::Uuid::parse_str(fields[4])
                .ok()
                .map(|value| value.to_string());
            let Some(found_installation) = found_installation else {
                // Older agents could leave a stopped managed container without
                // an installation binding. It cannot be attributed to the
                // current installation, but its exact Vonk run name and
                // stopped state prove that it is a retired attempt rather
                // than a live effect. Retire only that narrow shape; running,
                // malformed, or ambiguously named containers remain blockers.
                let run_name = fields[2]
                    .split(',')
                    .map(str::trim)
                    .find(|name| name.strip_prefix("vonk-").is_some());
                let Some(run_name) = run_name else {
                    return Err(OperationError::InvalidArtifact);
                };
                let Some(run_id) = run_name.strip_prefix("vonk-") else {
                    return Err(OperationError::InvalidArtifact);
                };
                let Ok(parsed_run_id) = uuid::Uuid::parse_str(run_id) else {
                    return Err(OperationError::InvalidArtifact);
                };
                if parsed_run_id.to_string() != run_id || !matches!(fields[1], "exited" | "dead") {
                    return Err(OperationError::InvalidArtifact);
                }
                let removed = self.run_docker(&[
                    "container".to_owned(),
                    "rm".to_owned(),
                    fields[0].to_owned(),
                ])?;
                if !removed.success || removed.exit_code != Some(0) {
                    return Err(OperationError::CommandFailed);
                }
                continue;
            };
            if found_installation != fields[4] {
                return Err(OperationError::InvalidArtifact);
            }
            if found_installation == installation_id {
                return Err(OperationError::InvalidArtifact);
            }
        }
        Ok(())
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
        // The agent has already authenticated this content-addressed archive
        // while delivering it from the enrolled Controller.  Keep the
        // boundary checks here, but do not hash a potentially multi-terabyte
        // archive again before loading it.
        let archive_identity = self.inspect_runtime_archive(archive, expected_bytes)?;
        // A previous authorized import is reusable only when the exact
        // archive, image reference, Docker config and receipt still agree.
        // A present but invalid receipt is an integrity failure, not a cache
        // miss that can silently trigger another import.
        let receipt_path = self.roots.runtime_image_receipts.join(archive_sha256);
        match fs::symlink_metadata(&receipt_path) {
            Ok(_) => {
                let receipt = self.read_image_receipt(archive_sha256)?;
                self.require_image_receipt(
                    archive_sha256,
                    registry_index_digest,
                    platform_manifest_digest,
                    image_reference,
                    &receipt.image_config_id,
                )?;
                match self.inspect_runtime_image_for_reference_if_present(image_reference)? {
                    Some((inspected, _)) => {
                        if inspected.1 != "linux"
                            || inspected.2 != "arm64"
                            || inspected.3 != "v1"
                            || !numeric_non_root_user(&inspected.4)
                            || inspected.0 != receipt.image_config_id
                            || archive_identity
                                != self.inspect_runtime_archive(archive, expected_bytes)?
                        {
                            return Err(OperationError::RuntimeImageIdentityInvalid);
                        }
                        return Ok(());
                    }
                    None if self.runtime_image_missing(&local_image)? => {}
                    None => return Err(OperationError::InvalidArtifact),
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        let archive_config_id = runtime_archive_config_digest(archive)?;
        if archive_identity != self.inspect_runtime_archive(archive, expected_bytes)? {
            return Err(OperationError::InvalidArtifact);
        }
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
        // Docker's human-readable load output varies across Docker releases
        // and archive producers. Accept only a single explicit source image
        // or image ID below; without one, the helper cannot prove which
        // object was loaded and fails closed.
        if !loaded.success {
            return Err(OperationError::RuntimeImageLoadFailed);
        }
        // Docker archives retain either the source tag or image ID that the
        // producer exported. Bind that explicit loaded object to the
        // controller-derived local reference before inspecting it.
        let source_image = loaded_image_source(&loaded.stdout)
            .map_err(|_| OperationError::RuntimeImageIdentityInvalid)?
            .ok_or(OperationError::RuntimeImageIdentityInvalid)?;
        let source_inspected =
            self.inspect_runtime_image(&source_image)
                .map_err(|error| match error {
                    OperationError::CommandFailed => OperationError::RuntimeImageInspectFailed,
                    OperationError::InvalidArtifact => OperationError::RuntimeImageIdentityInvalid,
                    other => other,
                })?;
        if source_inspected.1 != "linux"
            || source_inspected.2 != "arm64"
            || source_inspected.3 != "v1"
            || !numeric_non_root_user(&source_inspected.4)
        {
            return Err(OperationError::RuntimeImageIdentityInvalid);
        }
        let tagged = self
            .run_docker(&["tag".to_owned(), source_image, local_image])
            .map_err(|error| match error {
                OperationError::CommandFailed => OperationError::RuntimeImageInspectFailed,
                other => other,
            })?;
        if !tagged.success {
            return Err(OperationError::RuntimeImageInspectFailed);
        }
        let (inspected, _) = self
            .inspect_runtime_image_for_reference(image_reference)
            .map_err(|error| match error {
                OperationError::CommandFailed => OperationError::RuntimeImageInspectFailed,
                OperationError::InvalidArtifact => OperationError::RuntimeImageIdentityInvalid,
                other => other,
            })?;
        if inspected.0 != source_inspected.0 {
            return Err(OperationError::RuntimeImageIdentityInvalid);
        }
        if archive_identity != self.inspect_runtime_archive(archive, expected_bytes)? {
            return Err(OperationError::InvalidArtifact);
        }
        self.write_image_receipt(RuntimeImageReceipt {
            schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
            registry_index_digest: registry_index_digest.to_owned(),
            platform_manifest_digest: platform_manifest_digest.to_owned(),
            archive_sha256: archive_sha256.to_owned(),
            archive_bytes: expected_bytes,
            archive_identity,
            archive_config_id,
            image_config_id: inspected.0,
            local_image_reference: image_reference.to_owned(),
        })
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
        let (inspected, _) = self.inspect_runtime_image_for_reference(image_reference)?;
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

    fn runtime_start_authorized(
        &self,
        arguments: &[String],
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
    ) -> Result<Option<i32>, OperationError> {
        let installation_id = identity.installation_id.to_string();
        let started_at = Instant::now();
        let (launch, active_start) = {
            let _installation_guard = self.lock_installation_runtime(&installation_id)?;
            self.refuse_reconciled_runtime(&installation_id)?;
            self.update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start)?;
            let active_start = self.job_cancellation.begin(identity)?;
            let launch =
                self.runtime_start_locked(arguments, identity, logical_run_id, plan_digest)?;
            (launch, active_start)
        };
        let outcome = match launch {
            RuntimeStartLaunch::Job { timeout_seconds } => self.runtime_wait_for_job(
                identity,
                logical_run_id,
                plan_digest,
                timeout_seconds,
                started_at,
            ),
            RuntimeStartLaunch::TimedOut => Ok(Some(124)),
            RuntimeStartLaunch::Service => Ok(None),
        };
        drop(active_start);
        outcome
    }

    fn runtime_start_locked(
        &self,
        arguments: &[String],
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
    ) -> Result<RuntimeStartLaunch, OperationError> {
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
        let mut validated = validate_docker_run_with_archive(
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
            || validated.run_id != identity.runtime_id.to_string()
            || validated.installation_id != identity.installation_id.to_string()
            || !lower_hex(plan_digest, 64)
        {
            return Err(OperationError::InvalidOperation);
        }
        self.bind_native_fabric(&mut validated, Path::new(NATIVE_FABRIC_ROOT))?;
        let (inspected, operational_image) =
            self.inspect_runtime_image_for_reference(&validated.local_image_reference)?;
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
        let target = format!("vonk-{}", identity.runtime_id);
        let expected_labels = "{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.runtime-request-sha256\"}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}\t{{index .Config.Labels \"ai.vonkforge.target-id\"}}\t{{index .Config.Labels \"ai.vonkforge.installation-id\"}}\t{{index .Config.Labels \"ai.vonkforge.run-generation\"}}\t{{index .Config.Labels \"ai.vonkforge.plan-digest\"}}";
        let existing = self.run_docker_with_timeout(
            &[
                "container".to_owned(),
                "inspect".to_owned(),
                "--format".to_owned(),
                expected_labels.to_owned(),
                target.clone(),
            ],
            Duration::from_secs(15),
        )?;
        if existing.success {
            let expected = format!(
                "true\t{semantic_digest}\ttrue\t{logical_run_id}\t{}\t{}\t{}\t{plan_digest}",
                identity.runtime_id, identity.installation_id, identity.run_generation
            );
            let fields = std::str::from_utf8(&existing.stdout)
                .ok()
                .map(str::trim)
                .unwrap_or("");
            if fields != expected {
                return Err(OperationError::InvalidArtifact);
            }
            return if validated.job_timeout_seconds.is_some() {
                Ok(RuntimeStartLaunch::Job {
                    timeout_seconds: validated.job_timeout_seconds.unwrap(),
                })
            } else if validated.detached {
                Ok(RuntimeStartLaunch::Service)
            } else {
                Err(OperationError::InvalidArtifact)
            };
        }
        if !self.prove_container_absent(&target, &existing)? {
            return Err(OperationError::CommandFailed);
        }
        self.reset_runtime_tmp_if_requested(&validated.run_id)?;
        self.prepare_runtime_access(&validated)?;
        // The signed wire shape includes the executable once after the image
        // as a validation marker; Docker already receives it via --entrypoint.
        let mut compiled = validated.docker_arguments()?;
        compiled[validated.image_index] = operational_image;
        let labels = vec![
            "--label".to_owned(),
            format!("ai.vonkforge.runtime-request-sha256={semantic_digest}"),
            "--label".to_owned(),
            "ai.vonkforge.managed=true".to_owned(),
            "--label".to_owned(),
            format!("ai.vonkforge.run-id={logical_run_id}"),
            "--label".to_owned(),
            format!("ai.vonkforge.target-id={}", identity.runtime_id),
            "--label".to_owned(),
            format!("ai.vonkforge.installation-id={}", identity.installation_id),
            "--label".to_owned(),
            format!("ai.vonkforge.run-generation={}", identity.run_generation),
            "--label".to_owned(),
            format!("ai.vonkforge.plan-digest={plan_digest}"),
        ];
        if validated.job_timeout_seconds.is_some() && !validated.detached {
            // JobRun retains its canonical attached contract; detached Docker
            // launch is only the helper transport that permits concurrent Stop.
            compiled.insert(1, "--detach".to_owned());
        }
        compiled.splice(validated.image_index..validated.image_index, labels);
        validate_runtime_invocation(&compiled)?;
        let timeout = if validated.job_timeout_seconds.is_some() {
            Duration::from_secs(30)
        } else {
            Duration::from_secs(60)
        };
        let output = match self.run_docker_with_timeout(&compiled, timeout) {
            Ok(output) => output,
            Err(_) if validated.job_timeout_seconds.is_some() => {
                self.update_runtime_generation_fence(
                    &identity,
                    RuntimeGenerationFenceUse::Stop {
                        cancel_pending_start: true,
                    },
                )?;
                self.job_cancellation.cancel(identity)?;
                self.runtime_stop_once(identity, logical_run_id, plan_digest, 30)
                    .map_err(|_| OperationError::StopUncertain)?;
                return Ok(RuntimeStartLaunch::TimedOut);
            }
            Err(_) => return Err(OperationError::CommandFailed),
        };
        let identifier = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .unwrap_or("");
        if !output.success || !lower_hex(identifier, 64) {
            return Err(OperationError::CommandFailed);
        }
        if let Some(timeout_seconds) = validated.job_timeout_seconds {
            Ok(RuntimeStartLaunch::Job { timeout_seconds })
        } else {
            Ok(RuntimeStartLaunch::Service)
        }
    }

    fn runtime_wait_for_job(
        &self,
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        timeout_seconds: u16,
        started_at: Instant,
    ) -> Result<Option<i32>, OperationError> {
        let remaining =
            Duration::from_secs(u64::from(timeout_seconds)).saturating_sub(started_at.elapsed());
        let target = format!("vonk-{}", identity.runtime_id);
        let waited = if remaining.is_zero() {
            Err("runtime job deadline elapsed".to_owned())
        } else {
            self.runner.run_with_timeout(
                Path::new("/usr/bin/docker"),
                &["wait".to_owned(), target],
                remaining,
            )
        };
        match waited {
            Ok(output) if output.success => {
                let exit_code = bounded_container_wait_exit_code(&output);
                self.cleanup_runtime_after_job(identity, logical_run_id, plan_digest, false)?;
                if self.job_cancellation.was_cancelled(identity)? {
                    Ok(Some(124))
                } else {
                    Ok(Some(exit_code))
                }
            }
            Ok(_) => {
                let cancelled = self.job_cancellation.was_cancelled(identity)?;
                self.cleanup_runtime_after_job(identity, logical_run_id, plan_digest, !cancelled)?;
                if cancelled {
                    Ok(Some(124))
                } else {
                    Err(OperationError::CommandFailed)
                }
            }
            Err(_) => {
                self.cleanup_runtime_after_job(identity, logical_run_id, plan_digest, true)?;
                Ok(Some(124))
            }
        }
    }

    fn cleanup_runtime_after_job(
        &self,
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        cancel_pending_start: bool,
    ) -> Result<(), OperationError> {
        let installation_id = identity.installation_id.to_string();
        let _installation_guard = self.lock_installation_runtime(&installation_id)?;
        self.refuse_reconciled_runtime(&installation_id)?;
        self.update_runtime_generation_fence(
            &identity,
            RuntimeGenerationFenceUse::Stop {
                cancel_pending_start,
            },
        )?;
        if cancel_pending_start {
            self.job_cancellation.cancel(identity)?;
        }
        self.runtime_stop_once(identity, logical_run_id, plan_digest, 30)
            .map_err(|_| OperationError::StopUncertain)
    }

    fn bind_native_fabric(
        &self,
        run: &mut ValidatedDockerRun,
        sysfs_root: &Path,
    ) -> Result<(), OperationError> {
        let Some(fabric) = &run.native_fabric else {
            return Ok(());
        };
        let output = self
            .runner
            .run_with_timeout(
                Path::new(DOCKER_FIREWALL),
                &[
                    "--config".to_owned(),
                    DOCKER_FIREWALL_CONFIG.to_owned(),
                    "check-fabric-run".to_owned(),
                    fabric.local.to_string(),
                    fabric.master.to_string(),
                    fabric.port.to_string(),
                    run.host_endpoint_port
                        .map_or_else(|| "none".to_owned(), |port| port.to_string()),
                ],
                Duration::from_secs(10),
            )
            .map_err(|_| OperationError::RuntimeFabricFirewallRejected)?;
        if !output.success {
            return Err(OperationError::RuntimeFabricFirewallRejected);
        }
        let interface = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .filter(|value| crate::runtime_fabric::valid_interface(value))
            .ok_or(OperationError::RuntimeFabricUnavailable)?;
        let environment = crate::runtime_fabric::resolve(sysfs_root, interface, fabric.local)
            .map_err(|error| match error {
                crate::runtime_fabric::FabricError::Io(error) => OperationError::Io(error),
                crate::runtime_fabric::FabricError::Unavailable => {
                    OperationError::RuntimeFabricUnavailable
                }
            })?;
        let arguments = environment
            .into_iter()
            .flat_map(|value| ["--env".to_owned(), value])
            .collect::<Vec<_>>();
        let added = arguments.len();
        run.arguments
            .splice(run.image_index..run.image_index, arguments);
        run.image_index += added;
        Ok(())
    }

    /// The agent cannot traverse private directories created by root or the
    /// workload UID. Reset only this authorized run's disposable tmp tree,
    /// after exact container absence, without following any path component or
    /// descendant symlink. Outputs and the installation cache are untouched.
    fn reset_runtime_tmp_if_requested(&self, run_id: &str) -> Result<(), OperationError> {
        if uuid::Uuid::parse_str(run_id)
            .ok()
            .map(|value| value.to_string())
            .as_deref()
            != Some(run_id)
        {
            return Err(OperationError::InvalidOperation);
        }
        let flags = rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC;
        let root: std::os::fd::OwnedFd = OpenOptions::new()
            .read(true)
            .custom_flags(flags.bits() as i32)
            .open(&self.roots.agent_data)?
            .into();
        let metadata_root =
            rustix::fs::openat(&root, "run-metadata", flags, rustix::fs::Mode::empty())
                .map_err(errno_io)?;
        let metadata = rustix::fs::openat(&metadata_root, run_id, flags, rustix::fs::Mode::empty())
            .map_err(errno_io)?;
        let marker = match rustix::fs::openat(
            &metadata,
            "tmp-reset-required",
            // Inspect the descriptor before accepting its type. A malformed
            // FIFO must not block the helper while open waits for a writer.
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::NONBLOCK
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        ) {
            Ok(marker) => marker,
            Err(rustix::io::Errno::NOENT) => return Ok(()),
            Err(error) => return Err(errno_io(error).into()),
        };
        let marker_state = rustix::fs::fstat(&marker).map_err(errno_io)?;
        if rustix::fs::FileType::from_raw_mode(marker_state.st_mode)
            != rustix::fs::FileType::RegularFile
            || marker_state.st_mode & 0o777 != 0o600
            || marker_state.st_size != 0
            || marker_state.st_nlink != 1
            || self
                .runtime_request_owner_uid
                .is_some_and(|owner| marker_state.st_uid != owner)
        {
            return Err(OperationError::UnsafePath);
        }
        let device = rustix::fs::fstat(&root).map_err(errno_io)?.st_dev;
        let mut directory = Some(root);
        for component in ["runs", run_id, "outputs", "tmp"] {
            let Some(parent) = directory.take() else {
                break;
            };
            directory =
                match rustix::fs::openat(&parent, component, flags, rustix::fs::Mode::empty()) {
                    Ok(directory) => Some(directory),
                    Err(rustix::io::Errno::NOENT) => None,
                    Err(error) => return Err(errno_io(error).into()),
                };
            if let Some(directory) = &directory
                && rustix::fs::fstat(directory).map_err(errno_io)?.st_dev != device
            {
                return Err(OperationError::UnsafePath);
            }
        }
        if let Some(directory) = &directory {
            remove_directory_contents(directory, device)?;
            rustix::fs::fsync(directory).map_err(errno_io)?;
        }
        rustix::fs::unlinkat(
            &metadata,
            "tmp-reset-required",
            rustix::fs::AtFlags::empty(),
        )
        .map_err(errno_io)?;
        rustix::fs::fsync(&metadata).map_err(errno_io)?;
        Ok(())
    }

    fn runtime_run_inspect(
        &self,
        arguments: &[String],
        capture_failure: bool,
    ) -> Result<bool, OperationError> {
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
        let mut validated = validate_docker_run_with_archive(
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
        self.bind_native_fabric(&mut validated, Path::new(NATIVE_FABRIC_ROOT))?;
        let (inspected, _) =
            self.inspect_runtime_image_for_reference(&validated.local_image_reference)?;
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
            "{{.Id}}\t{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.runtime-request-sha256\"}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}".to_owned(),
            format!("vonk-{}", validated.run_id),
        ])?;
        if !existing.success {
            if capture_failure
                && self.prove_container_absent(&format!("vonk-{}", validated.run_id), &existing)?
            {
                return Err(OperationError::RuntimeRunMissing);
            }
            return Err(OperationError::CommandFailed);
        }
        let fields = std::str::from_utf8(&existing.stdout)
            .ok()
            .map(str::trim)
            .map(|text| text.split('\t').collect::<Vec<_>>())
            .unwrap_or_default();
        let [container_id, running, digest, managed, run_id] = fields.as_slice() else {
            return Ok(false);
        };
        if !lower_hex(container_id, 64)
            || *digest != semantic_digest
            || *managed != "true"
            || *run_id != validated.run_id
        {
            return Ok(false);
        }
        if *running == "true" {
            return Ok(true);
        }
        if *running == "false" && capture_failure {
            // Read only the exact inspected container, never a reusable name.
            // Capture before the agent removes it; failed or foreign identity
            // checks above must never grant access to container logs.
            let logs = self.runner.run_with_timeout(
                Path::new("/usr/bin/docker"),
                &[
                    "logs".into(),
                    "--tail".into(),
                    crate::runtime_logs::CAPTURE_LINES.into(),
                    (*container_id).into(),
                ],
                Duration::from_secs(30),
            );
            return Err(match logs {
                Ok(logs) if logs.success => OperationError::RuntimeProcessExited {
                    logs: Some(Box::new(crate::runtime_logs::retain_container(
                        &logs.stdout,
                        &logs.stderr,
                    ))),
                    capture_error: None,
                },
                // An unread log is reported as unread; it never becomes an
                // empty tail that reads like a container with nothing to say.
                Ok(_) => OperationError::RuntimeProcessExited {
                    logs: None,
                    capture_error: Some("the container log command failed"),
                },
                Err(_) => OperationError::RuntimeProcessExited {
                    logs: None,
                    capture_error: Some("the container log command did not run"),
                },
            });
        }
        Ok(false)
    }

    fn prepare_runtime_access(&self, run: &ValidatedDockerRun) -> Result<(), OperationError> {
        for path in run.models.iter().chain(run.inputs.iter()) {
            // Reapplying setfacl changes ctime even when the named runtime
            // entry already grants precisely the intended read access. That
            // invalidates the agent's immutable installation receipt before
            // collective readiness. Inspect the whole selected tree and skip
            // the recursive write only when every ACL is already exact.
            // Installation and run inputs are owned by the unprivileged
            // agent, as checked while validating the signed Docker run.
            // The helper's own root-owned custody is a separate boundary.
            if runtime_read_tree_acl_ready(path, run.uid, self.runtime_request_owner_uid)? {
                continue;
            }
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

    fn runtime_stop_authorized(
        &self,
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        timeout: u16,
        cancel_pending_start: bool,
    ) -> Result<(), OperationError> {
        let installation_id = identity.installation_id.to_string();
        let deadline = Instant::now() + Duration::from_secs(u64::from(timeout) + 15);
        {
            let _installation_guard = self.lock_installation_runtime(&installation_id)?;
            self.refuse_reconciled_runtime(&installation_id)?;
            // Advance the durable generation fence before even inspecting for
            // absence. A delayed old Start therefore cannot recreate a run
            // after this Stop has been acknowledged or the helper restarts.
            self.update_runtime_generation_fence(
                &identity,
                RuntimeGenerationFenceUse::Stop {
                    cancel_pending_start,
                },
            )?;
            if cancel_pending_start {
                self.job_cancellation.cancel(identity)?;
            }
            self.runtime_stop_once(identity, logical_run_id, plan_digest, timeout)?;
        }
        if cancel_pending_start {
            self.job_cancellation
                .wait_for_active_start(identity, deadline)?;
        }
        Ok(())
    }

    fn runtime_stop_once(
        &self,
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        timeout: u16,
    ) -> Result<(), OperationError> {
        if !lower_hex(plan_digest, 64)
            || identity.run_generation == 0
            || identity.run_generation > i32::MAX as u32
        {
            return Err(OperationError::InvalidOperation);
        }
        let name = format!("vonk-{}", identity.runtime_id);
        let existing = self.run_docker_with_timeout(
            &[
            "container".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}\t{{index .Config.Labels \"ai.vonkforge.target-id\"}}\t{{index .Config.Labels \"ai.vonkforge.installation-id\"}}\t{{index .Config.Labels \"ai.vonkforge.run-generation\"}}\t{{index .Config.Labels \"ai.vonkforge.plan-digest\"}}".to_owned(),
            name.clone(),
            ],
            Duration::from_secs(15),
        )?;
        if !existing.success {
            // A failed inspect is not proof of absence. Docker access and
            // daemon errors can share its exit code, so require an independent
            // exact empty listing before acknowledging a repeated stop.
            return if self.prove_container_absent(&name, &existing)? {
                Ok(())
            } else {
                Err(OperationError::CommandFailed)
            };
        }
        let expected = format!(
            "true\t{logical_run_id}\t{}\t{}\t{}\t{plan_digest}",
            identity.runtime_id, identity.installation_id, identity.run_generation
        );
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

    fn prove_container_absent(
        &self,
        name: &str,
        inspected: &CommandOutput,
    ) -> Result<bool, OperationError> {
        if inspected.exit_code != Some(1) || !inspected.stdout.iter().all(u8::is_ascii_whitespace) {
            return Ok(false);
        }
        let listing = self.run_docker_with_timeout(
            &[
                "container".to_owned(),
                "ls".to_owned(),
                "--all".to_owned(),
                "--quiet".to_owned(),
                "--no-trunc".to_owned(),
                "--filter".to_owned(),
                format!("name=^/{name}$"),
            ],
            Duration::from_secs(15),
        )?;
        Ok(listing.success
            && listing.exit_code == Some(0)
            && listing.stdout.iter().all(u8::is_ascii_whitespace))
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

    fn inspect_runtime_image(&self, image: &str) -> Result<RuntimeImageInspection, OperationError> {
        self.inspect_runtime_image_if_present(image)?
            .ok_or(OperationError::InvalidArtifact)
    }

    fn inspect_runtime_image_if_present(
        &self,
        image: &str,
    ) -> Result<Option<RuntimeImageInspection>, OperationError> {
        let output = self.run_docker(&[
            "image".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{.Id}}\t{{.Os}}\t{{.Architecture}}\t{{index .Config.Labels \"ai.vonkforge.runtime-interface\"}}\t{{.Config.User}}".to_owned(),
            image.to_owned(),
        ])?;
        if !output.success {
            // Docker writes a newline to stdout for some missing image
            // references. A miss remains provisional here: receipt reuse
            // additionally requires a successful empty image listing.
            return if output.exit_code == Some(1)
                && output.stdout.iter().all(u8::is_ascii_whitespace)
            {
                Ok(None)
            } else {
                Err(OperationError::RuntimeImageInspectFailed)
            };
        }
        let fields = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .map(|value| value.split('\t').map(str::to_owned).collect::<Vec<_>>())
            .unwrap_or_default();
        if output.exit_code != Some(0) || fields.len() != 5 || !valid_oci_digest(&fields[0]) {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(Some((
            fields[0].clone(),
            fields[1].clone(),
            fields[2].clone(),
            fields[3].clone(),
            fields[4].clone(),
        )))
    }

    fn inspect_runtime_image_for_reference(
        &self,
        image_reference: &str,
    ) -> Result<(RuntimeImageInspection, String), OperationError> {
        self.inspect_runtime_image_for_reference_if_present(image_reference)?
            .ok_or(OperationError::InvalidArtifact)
    }

    fn inspect_runtime_image_for_reference_if_present(
        &self,
        image_reference: &str,
    ) -> Result<Option<(RuntimeImageInspection, String)>, OperationError> {
        let (local_image, _) = parse_local_image_reference(image_reference)?;
        match self.inspect_runtime_image_if_present(image_reference)? {
            Some(inspected) => Ok(Some((inspected, image_reference.to_owned()))),
            None => {
                // Classic Docker may discard RepoDigests while loading an OCI
                // archive. The signed logical reference remains receipt-bound;
                // use the verified local config ID as the daemon reference so
                // launch stays pinned to the inspected image object.
                let Some(inspected) = self.inspect_runtime_image_if_present(&local_image)? else {
                    return Ok(None);
                };
                let operational_image = inspected.0.clone();
                Ok(Some((inspected, operational_image)))
            }
        }
    }

    fn runtime_image_missing(&self, local_image: &str) -> Result<bool, OperationError> {
        let output = self.run_docker(&[
            "image".to_owned(),
            "ls".to_owned(),
            "--quiet".to_owned(),
            "--no-trunc".to_owned(),
            "--filter".to_owned(),
            format!("reference={local_image}"),
        ])?;
        if !output.success || output.exit_code != Some(0) {
            return Err(OperationError::RuntimeImageInspectFailed);
        }
        let body = std::str::from_utf8(&output.stdout)
            .map_err(|_| OperationError::RuntimeImageInspectFailed)?;
        if body.trim().is_empty() {
            return Ok(true);
        }
        if body.lines().all(|line| valid_oci_digest(line.trim())) {
            return Ok(false);
        }
        Err(OperationError::RuntimeImageInspectFailed)
    }

    fn write_image_receipt(&self, receipt: RuntimeImageReceipt) -> Result<(), OperationError> {
        fs::create_dir_all(&self.roots.runtime_image_receipts)?;
        fs::set_permissions(
            &self.roots.runtime_image_receipts,
            fs::Permissions::from_mode(0o700),
        )?;
        if receipt.schema_version != RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION
            || !valid_oci_digest(&receipt.registry_index_digest)
            || !valid_oci_digest(&receipt.platform_manifest_digest)
            || !lower_hex(&receipt.archive_sha256, 64)
            || !valid_local_image_reference(&receipt.local_image_reference)
            || !valid_oci_digest(&receipt.archive_config_id)
            || !valid_oci_digest(&receipt.image_config_id)
        {
            return Err(OperationError::InvalidArtifact);
        }
        let path = self
            .roots
            .runtime_image_receipts
            .join(&receipt.archive_sha256);
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
        let receipt = self.read_image_receipt(archive_sha256)?;
        if receipt.schema_version != RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION
            || receipt.archive_sha256 != archive_sha256
            || receipt.registry_index_digest != registry_index_digest
            || receipt.platform_manifest_digest != platform_manifest_digest
            || receipt.local_image_reference != local_image_reference
            || !valid_oci_digest(&receipt.archive_config_id)
            || receipt.image_config_id != image_config_id
        {
            return Err(OperationError::InvalidArtifact);
        }
        let (archive_root, _) = self.canonical_archive_root()?;
        let archive = archive_root.join(archive_sha256);
        if self.inspect_runtime_archive(&archive, receipt.archive_bytes)?
            != receipt.archive_identity
        {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(())
    }

    fn read_image_receipt(
        &self,
        archive_sha256: &str,
    ) -> Result<RuntimeImageReceipt, OperationError> {
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
        Ok(receipt)
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

    fn inspect_runtime_archive(
        &self,
        path: &Path,
        expected_bytes: u64,
    ) -> Result<RuntimeArchiveIdentity, OperationError> {
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
        // The path belongs to the unprivileged agent.  Do not let a final
        // component replacement turn the checked regular file into a
        // symlink before the helper opens it.
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(path)
            .map_err(|_| OperationError::InvalidArtifact)?;
        let before = file
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        let after = file
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        if artifact_identity(&metadata) != artifact_identity(&before)
            || before.len() != expected_bytes
            || artifact_identity(&before) != artifact_identity(&after)
        {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(runtime_archive_identity(&before))
    }

    fn require_directory(&self, path: &Path) -> Result<(), OperationError> {
        require_safe_directory(path, self.required_owner_uid)
    }
}

fn runtime_read_tree_acl_ready(
    path: &Path,
    runtime_uid: u32,
    required_owner_uid: Option<u32>,
) -> Result<bool, OperationError> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink()
        || !(metadata.is_file() || metadata.is_dir())
        || metadata.mode() & 0o022 != 0
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
    {
        return Err(OperationError::UnsafePath);
    }
    if metadata.is_dir() && xattr::get(path, "system.posix_acl_default")?.is_some() {
        return Err(OperationError::InvalidArtifact);
    }
    let mut ready = exact_runtime_read_acl(path, &metadata, runtime_uid)?;
    if metadata.is_dir() {
        for entry in fs::read_dir(path)? {
            ready &= runtime_read_tree_acl_ready(&entry?.path(), runtime_uid, required_owner_uid)?;
        }
    }
    Ok(ready)
}

fn exact_runtime_read_acl(
    path: &Path,
    metadata: &fs::Metadata,
    runtime_uid: u32,
) -> Result<bool, OperationError> {
    let Some(value) = xattr::get(path, "system.posix_acl_access")? else {
        return Ok(false);
    };
    if value.len() != 44 || u32::from_le_bytes(value[..4].try_into().unwrap()) != 2 {
        return Err(OperationError::InvalidArtifact);
    }
    let mut user_object = None;
    let mut runtime_user = None;
    let mut group_object = None;
    let mut mask = None;
    let mut other = None;
    for entry in value[4..].as_chunks::<8>().0.iter() {
        let (tag, permissions, identifier) = acl_entry(entry);
        match tag {
            0x0001 if identifier == u32::MAX && user_object.replace(permissions).is_none() => {}
            0x0002 if identifier == runtime_uid && runtime_user.replace(permissions).is_none() => {}
            0x0004 if identifier == u32::MAX && group_object.replace(permissions).is_none() => {}
            0x0010 if identifier == u32::MAX && mask.replace(permissions).is_none() => {}
            0x0020 if identifier == u32::MAX && other.replace(permissions).is_none() => {}
            _ => return Err(OperationError::InvalidArtifact),
        }
    }
    let (Some(user_object), Some(runtime_user), Some(group_object), Some(mask), Some(other)) =
        (user_object, runtime_user, group_object, mask, other)
    else {
        return Err(OperationError::InvalidArtifact);
    };
    let expected_runtime = if metadata.is_dir() || (user_object | group_object | other) & 0o1 != 0 {
        0o5
    } else {
        0o4
    };
    if user_object > 0o7
        || group_object & 0o2 != 0
        || other & 0o2 != 0
        || runtime_user != expected_runtime
        || mask != (group_object | runtime_user)
        || metadata.mode() & 0o777
            != (u32::from(user_object) << 6 | u32::from(mask) << 3 | u32::from(other))
    {
        return Err(OperationError::InvalidArtifact);
    }
    Ok(true)
}

fn runtime_archive_config_digest(path: &Path) -> Result<String, OperationError> {
    let manifest = read_runtime_archive_member(path, Path::new("manifest.json"))?;
    let entries: Vec<DockerSaveManifestEntry> =
        serde_json::from_slice(&manifest).map_err(|_| OperationError::InvalidArtifact)?;
    if entries.len() != 1 {
        return Err(OperationError::InvalidArtifact);
    }
    let config_path = Path::new(&entries[0].config);
    let config = config_member_name(config_path).ok_or(OperationError::InvalidArtifact)?;
    let bytes = read_runtime_archive_member(path, config_path)?;
    if hex_sha256(&bytes) != config {
        return Err(OperationError::InvalidArtifact);
    }
    Ok(format!("sha256:{config}"))
}

fn read_runtime_archive_member(path: &Path, target: &Path) -> Result<Vec<u8>, OperationError> {
    let file = File::open(path).map_err(|_| OperationError::InvalidArtifact)?;
    let mut archive = tar::Archive::new(file);
    let mut found = None;
    // A Docker-save archive can contain many layer and manifest blobs. Read
    // only the exact member selected by manifest.json, seeking past payloads
    // so config verification does not reread every multi-gigabyte layer.
    for entry in archive
        .entries_with_seek()
        .map_err(|_| OperationError::InvalidArtifact)?
    {
        let mut entry = entry.map_err(|_| OperationError::InvalidArtifact)?;
        if entry.path().map_err(|_| OperationError::InvalidArtifact)? != target {
            continue;
        }
        if found.is_some()
            || !entry.header().entry_type().is_file()
            || entry.size() > MAX_RUNTIME_CONFIG_BYTES as u64
        {
            return Err(OperationError::InvalidArtifact);
        }
        let size = entry.size() as usize;
        let mut bytes = Vec::with_capacity(size);
        entry
            .read_to_end(&mut bytes)
            .map_err(|_| OperationError::InvalidArtifact)?;
        if bytes.len() != size {
            return Err(OperationError::InvalidArtifact);
        }
        found = Some(bytes);
    }
    found.ok_or(OperationError::InvalidArtifact)
}

fn config_member_name(path: &Path) -> Option<String> {
    let value = path.to_str()?;
    let digest = if let Some(value) = value.strip_prefix("blobs/sha256/") {
        value.strip_suffix(".json").unwrap_or(value)
    } else {
        if path.components().count() != 1 {
            return None;
        }
        value.strip_suffix(".json")?
    };
    lower_hex(digest, 64).then(|| digest.to_owned())
}

fn bounded_container_wait_exit_code(output: &CommandOutput) -> i32 {
    std::str::from_utf8(&output.stdout)
        .ok()
        .map(str::trim)
        .and_then(|value| value.parse::<i32>().ok())
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
    installation_id: String,
    uid: u32,
    models: Vec<PathBuf>,
    inputs: Option<PathBuf>,
    outputs: PathBuf,
    cache_root: PathBuf,
    cache_home: PathBuf,
    tmp_root: PathBuf,
    runtime_contract: PathBuf,
    host_endpoint_port: Option<u16>,
    native_fabric: Option<NativeFabric>,
    job_timeout_seconds: Option<u16>,
}

struct NativeFabric {
    local: Ipv4Addr,
    master: Ipv4Addr,
    port: u16,
}

type RuntimeImageInspection = (String, String, String, String, String);

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
    let mut infiniband = false;
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
    let mut world_size = None;
    let mut local_address = None;
    let mut master_address = None;
    let mut caller_fabric_environment = false;
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
                    Some("host") => Some("host"),
                    _ => return Err(OperationError::InvalidOperation),
                };
            }
            "--device" if !gpu || !infiniband => {
                index += 1;
                match arguments.get(index).map(String::as_str) {
                    Some("nvidia.com/gpu=all") if !gpu => gpu = true,
                    Some("/dev/infiniband:/dev/infiniband") if !infiniband => infiniband = true,
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
                caller_fabric_environment |= value.split_once('=').is_some_and(|(name, _)| {
                    crate::runtime_fabric::ENVIRONMENT_NAMES.contains(&name)
                });
                if let Some(value) = value.strip_prefix("VONK_WORLD_SIZE=") {
                    let parsed = value
                        .parse::<u32>()
                        .map_err(|_| OperationError::InvalidOperation)?;
                    if parsed == 0 || world_size.replace(parsed).is_some() {
                        return Err(OperationError::InvalidOperation);
                    }
                }
                for (name, target) in [
                    ("VONK_LOCAL_ADDR=", &mut local_address),
                    ("VONK_MASTER_ADDR=", &mut master_address),
                ] {
                    if let Some(value) = value.strip_prefix(name) {
                        let parsed = value
                            .parse::<Ipv4Addr>()
                            .map_err(|_| OperationError::InvalidOperation)?;
                        if target.replace(parsed).is_some() {
                            return Err(OperationError::InvalidOperation);
                        }
                    }
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
    require_runtime_directory(&cache_root, agent_data_owner_uid, uid)?;
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
        || (network != Some("host") && (infiniband || memlock || stack))
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
    let bridge_workload = endpoint_workload && !distributed_workload;
    let expected_network = if distributed_workload {
        "host"
    } else if bridge_workload {
        "bridge"
    } else {
        "none"
    };
    let endpoint_published = listen_port.is_some_and(|port| published_ports.contains(&port));
    if network != Some(expected_network)
        || publishes != published_ports.len()
        || (!bridge_workload && publishes != 0)
        || (bridge_workload && (publishes != 1 || !endpoint_published))
    {
        return Err(OperationError::InvalidOperation);
    }
    let native_fabric = if distributed_workload {
        let (Some(local), Some(master), Some(port), Some(rank), Some(world_size)) =
            (local_address, master_address, master_port, rank, world_size)
        else {
            return Err(OperationError::InvalidOperation);
        };
        if !detach
            || !gpu
            || !infiniband
            || !memlock
            || !stack
            || world_size < 2
            || rank >= world_size
            || caller_fabric_environment
            || endpoint_workload != (local == master)
        {
            return Err(OperationError::InvalidOperation);
        }
        Some(NativeFabric {
            local,
            master,
            port,
        })
    } else {
        None
    };
    if models.is_empty()
        || models.len() > MAX_COMPILED_MODEL_FILES
        || models.len() > 1 && model_targets.contains("/models")
    {
        return Err(OperationError::InvalidOperation);
    }
    let canonical_model_root = canonical_model_root(roots, &models, agent_data_owner_uid)?;
    // The writable cache belongs to the same installation as the validated
    // models. Checking the leaf alone would permit another installation or
    // an installation symlink to redirect the privileged mount and ACLs.
    let cache_installation = cache_root.parent().ok_or(OperationError::UnsafePath)?;
    require_safe_directory(cache_installation, agent_data_owner_uid)?;
    let installation_id = cache_installation
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| valid_artifact_id(value))
        .ok_or(OperationError::UnsafePath)?
        .to_owned();
    let canonical_cache = cache_root
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    if canonical_cache.parent() != canonical_model_root.parent() {
        return Err(OperationError::UnsafePath);
    }
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
        installation_id,
        uid,
        models,
        inputs,
        cache_root: cache_root.clone(),
        outputs,
        cache_home: cache_root.join("home"),
        tmp_root,
        runtime_contract,
        host_endpoint_port: (network == Some("host")).then_some(listen_port).flatten(),
        native_fabric,
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

fn validate_runtime_start_plan(plan: &RecipeStartPayload) -> Result<(), OperationError> {
    let encoded_plan = canonical_json(&plan.compiled_execution_plan)
        .map_err(|_| OperationError::InvalidOperation)?;
    let encoded_claim = canonical_json(plan).map_err(|_| OperationError::InvalidOperation)?;
    let compiled = &plan.compiled_execution_plan;
    compiled
        .validate()
        .map_err(|_| OperationError::InvalidOperation)?;
    let placement = &compiled.runtime.placement;
    let expected_endpoint = placement.endpoint_address.or_else(|| {
        (plan.world_size > 1)
            .then_some(placement.local_address)
            .flatten()
    });
    let phase_binding_valid = match (plan.phase, plan.start_deadline.as_deref()) {
        (None, None) => true,
        (Some(_), Some(deadline)) => !deadline.is_empty() && deadline.len() <= 64,
        _ => false,
    };
    if plan.schema_version != 2
        || plan.run_generation == 0
        || plan.run_generation > i32::MAX as u32
        || plan.mapping_generation == 0
        || plan.rank >= plan.world_size
        || plan.world_size == 0
        || !valid_recipe_alias(&plan.alias)
        || !lower_hex(&plan.recipe_content_sha256, 64)
        || !lower_hex(&plan.plan_digest, 64)
        || !valid_oci_digest(&plan.image_digest)
        || compiled.job.is_some()
        || compiled.endpoint.is_none()
        || compiled.identity.recipe_revision_sha256 != plan.recipe_content_sha256
        || compiled.runtime.image_digest != plan.image_digest
        || compiled.runtime_image.image_digest != plan.image_digest
        || plan.rank != placement.rank
        || plan.role != placement.role
        || plan.world_size != placement.world_size
        || expected_endpoint != Some(plan.endpoint_address)
        || plan.port != placement.port.unwrap_or_default()
        || plan.local_address != placement.local_address
        || plan.master_address != placement.master_address
        || plan.master_port != placement.master_port
        || plan.reserved_memory_bytes != placement.reserved_memory_bytes
        || plan.memory_floor_bytes != placement.memory_floor_bytes
        || plan.memory_kind.to_string() != placement.memory_kind.to_string()
        || (plan.world_size == 1
            && (plan.rank != 0
                || plan.local_address.is_some()
                || plan.master_address.is_some()
                || plan.master_port.is_some()
                || plan.phase.is_some()
                || plan.start_deadline.is_some()))
        || (plan.world_size > 1
            && (plan.local_address.is_none()
                || plan.master_address.is_none()
                || plan.master_port.is_none()))
        || !phase_binding_valid
        || encoded_plan.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
        || encoded_claim.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES
    {
        return Err(OperationError::InvalidOperation);
    }
    Ok(())
}

fn validate_runtime_job_plan(plan: &RecipeJobRunRequest) -> Result<(), OperationError> {
    let encoded_plan = canonical_json(&plan.compiled_execution_plan)
        .map_err(|_| OperationError::InvalidOperation)?;
    let encoded_claim = canonical_json(plan).map_err(|_| OperationError::InvalidOperation)?;
    let compiled = &plan.compiled_execution_plan;
    compiled
        .validate()
        .map_err(|_| OperationError::InvalidOperation)?;
    let placement = &compiled.runtime.placement;
    let job = compiled
        .job
        .as_ref()
        .ok_or(OperationError::InvalidOperation)?;
    if plan.schema_version != 1
        || plan.run_generation == 0
        || plan.run_generation > i32::MAX as u32
        || plan.mapping_generation == 0
        || !lower_hex(&plan.recipe_content_sha256, 64)
        || !lower_hex(&plan.plan_digest, 64)
        || !lower_hex(&plan.contract_sha256, 64)
        || !valid_oci_digest(&plan.image_digest)
        || plan.recipe_content_sha256 != compiled.identity.recipe_revision_sha256
        || plan.image_digest != compiled.runtime.image_digest
        || plan.image_digest != compiled.runtime_image.image_digest
        || job.interface.to_string() != plan.interface.to_string()
        || job.timeout_seconds != plan.timeout_seconds
        || !(1..=3600).contains(&plan.timeout_seconds)
        || plan.rank != 0
        || plan.role != "entrypoint"
        || u64::from(plan.rank) != placement.rank
        || plan.role != placement.role
        || plan.reserved_memory_bytes != placement.reserved_memory_bytes
        || plan.memory_floor_bytes != placement.memory_floor_bytes
        || plan.memory_kind.to_string() != placement.memory_kind.to_string()
        || plan.input_total_bytes > 1024 * 1024 * 1024
        || plan.inputs.iter().try_fold(0_u64, |total, file| {
            total.checked_add(u64::from(file.size_bytes))
        }) != Some(u64::from(plan.input_total_bytes))
        || encoded_plan.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
        || encoded_claim.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES
    {
        return Err(OperationError::InvalidOperation);
    }
    Ok(())
}

fn validate_runtime_stop_plan(plan: &RecipeStopPayload) -> Result<(), OperationError> {
    let encoded_plan = canonical_json(&plan.compiled_execution_plan)
        .map_err(|_| OperationError::InvalidOperation)?;
    let encoded_claim = canonical_json(plan).map_err(|_| OperationError::InvalidOperation)?;
    let compiled = &plan.compiled_execution_plan;
    compiled
        .validate_storage()
        .map_err(|_| OperationError::InvalidOperation)?;
    let placement = &compiled.runtime.placement;
    if plan.schema_version != 2
        || plan.run_generation == 0
        || plan.run_generation > i32::MAX as u32
        || plan.mapping_generation == 0
        || plan.world_size == 0
        || plan.rank >= plan.world_size
        || !lower_hex(&plan.recipe_content_sha256, 64)
        || !lower_hex(&plan.plan_digest, 64)
        || plan.recipe_content_sha256 != compiled.identity.recipe_revision_sha256
        || plan.rank != placement.rank
        || plan.role != placement.role
        || plan.world_size != placement.world_size
        || !valid_recipe_node_id(&plan.node_id)
        || (compiled.job.is_none() && plan.target_runtime_id != plan.run_id)
        || !(1..=600).contains(&compiled.lifecycle.stop_timeout_seconds)
        || encoded_plan.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
        || encoded_claim.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES
    {
        return Err(OperationError::InvalidOperation);
    }
    Ok(())
}

fn valid_recipe_node_id(value: &str) -> bool {
    value
        .strip_prefix("spk_")
        .is_some_and(|suffix| lower_hex(suffix, 32))
}

fn valid_recipe_alias(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 63
        && value
            .as_bytes()
            .first()
            .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
        })
        && value
            .as_bytes()
            .last()
            .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
}

fn runtime_plan_prefix(plan: &CompiledExecutionPlan, command: Vec<String>) -> Vec<String> {
    let mut arguments = vec![
        plan.runtime_image.oci_layout_sha256.clone(),
        plan.runtime_image
            .registry_manifest_digest
            .clone()
            .unwrap_or_else(|| plan.runtime_image.platform_manifest_digest.clone()),
        plan.runtime_image.platform_manifest_digest.clone(),
        plan.runtime_image.local_image_reference.clone(),
    ];
    arguments.extend(command);
    arguments
}

/// Compute Linux exec limits from this helper process' current stack limit
/// and page size. The kernel's aggregate rule is
/// `max(ARG_MAX, min(_STK_LIM * 3/4, RLIMIT_STACK / 4))`; `ARG_MAX` is the
/// documented 128 KiB floor and `_STK_LIM` is the Linux 8 MiB stack ceiling.
/// Each individual argument/environment string is separately limited to 32
/// pages. This measures the invocation the helper will actually launch.
fn current_linux_exec_invocation_limits() -> Result<ExecInvocationLimits, OperationError> {
    let page_size = u64::try_from(rustix::param::page_size())
        .ok()
        .filter(|value| *value > 0)
        .ok_or(OperationError::RuntimeInvocationLimitsUnavailable)?;
    let string_bytes = page_size
        .checked_mul(32)
        .ok_or(OperationError::RuntimeInvocationLimitsUnavailable)?;
    let stack_bytes = rustix::process::getrlimit(rustix::process::Resource::Stack)
        .current
        .unwrap_or(u64::MAX);
    let total_bytes =
        LINUX_ARG_MAX_FLOOR_BYTES.max((stack_bytes / 4).min(LINUX_ARG_MAX_CEILING_BYTES));
    Ok(ExecInvocationLimits {
        total_bytes,
        string_bytes,
    })
}

fn validate_runtime_invocation(arguments: &[String]) -> Result<(), OperationError> {
    match measure_exec_invocation(
        "/usr/bin/docker",
        arguments,
        &HELPER_COMMAND_ENV,
        current_linux_exec_invocation_limits()?,
    ) {
        Ok(_) => Ok(()),
        Err(CompiledOciError::InvocationBytes { limit, observed }) => {
            Err(OperationError::RuntimeInvocationLimitExceeded {
                string_limit: false,
                limit_bytes: limit,
                observed_bytes: observed,
            })
        }
        Err(CompiledOciError::InvocationStringBytes { limit, observed }) => {
            Err(OperationError::RuntimeInvocationLimitExceeded {
                string_limit: true,
                limit_bytes: limit,
                observed_bytes: observed,
            })
        }
        Err(_) => Err(OperationError::InvalidOperation),
    }
}

fn valid_model_mount(source: &Path, target: &str, roots: &ManagedRoots) -> bool {
    if target.chars().count() > MAX_COMPILED_MODEL_PATH_CHARS
        || (target != "/models"
            && (!target.starts_with("/models/")
                || target.ends_with('/')
                || !target.split('/').skip(1).all(valid_model_path_component)))
    {
        return false;
    }
    let Some(model_root) = runtime_model_root(source, roots) else {
        return false;
    };
    let relative = source.strip_prefix(model_root).ok();
    let components = relative
        .into_iter()
        .flat_map(Path::components)
        .collect::<Vec<_>>();
    let new_layout = components.len() >= 3
        && matches!(components[0], Component::Normal(value) if lower_hex(&value.to_string_lossy(), 64))
        && matches!(components[1], Component::Normal(value) if valid_artifact_id(&value.to_string_lossy()))
        && valid_model_file_path_components(&components[2..]);
    let selection_layout = components.len() >= 2
        && matches!(components[0], Component::Normal(value) if valid_artifact_id(&value.to_string_lossy()) && value != "sha256")
        && valid_model_file_path_components(&components[1..]);
    if !(new_layout || selection_layout) {
        return false;
    }
    if target == "/models" {
        return true;
    }
    target
        .strip_prefix("/models/")
        .is_some_and(|value| value.split('/').all(valid_model_path_component))
}

fn valid_model_file_path_components(components: &[Component<'_>]) -> bool {
    let mut chars = 0_usize;
    components.iter().enumerate().all(|(index, component)| {
        let Component::Normal(value) = component else {
            return false;
        };
        let Some(value) = value.to_str() else {
            return false;
        };
        chars = chars.saturating_add(value.chars().count());
        if index > 0 {
            chars = chars.saturating_add(1);
        }
        chars <= MAX_COMPILED_MODEL_PATH_CHARS && valid_model_path_component(value)
    })
}

fn valid_model_path_component(value: &str) -> bool {
    !value.is_empty() && !matches!(value, "." | "..") && !value.contains(['\\', '\0'])
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

fn errno_io(error: rustix::io::Errno) -> std::io::Error {
    std::io::Error::from_raw_os_error(error.raw_os_error())
}

fn retryable_reconciliation_storage_io(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::Interrupted
            | std::io::ErrorKind::WouldBlock
            | std::io::ErrorKind::TimedOut
    ) || error.raw_os_error().is_some_and(|code| {
        code == rustix::io::Errno::IO.raw_os_error()
            || code == rustix::io::Errno::NOSPC.raw_os_error()
    })
}

fn remove_directory_contents(
    directory: &impl std::os::fd::AsFd,
    expected_device: u64,
) -> Result<(), OperationError> {
    let mut buffer = [MaybeUninit::uninit(); 8192];
    let mut entries = Vec::new();
    {
        let mut directory_entries = rustix::fs::RawDir::new(directory, &mut buffer);
        while let Some(entry) = directory_entries.next() {
            let entry = entry.map_err(errno_io)?;
            let name = entry.file_name();
            if name.to_bytes() == b"." || name.to_bytes() == b".." {
                continue;
            }
            entries.push(CString::new(name.to_bytes()).map_err(|_| OperationError::UnsafePath)?);
        }
    }
    for name in entries {
        remove_directory_entry(directory, &name, expected_device)?;
    }
    Ok(())
}

fn remove_directory_entry(
    parent: &impl std::os::fd::AsFd,
    name: &CStr,
    expected_device: u64,
) -> Result<(), OperationError> {
    let metadata = rustix::fs::statat(parent, name, rustix::fs::AtFlags::SYMLINK_NOFOLLOW)
        .map_err(errno_io)?;
    if rustix::fs::FileType::from_raw_mode(metadata.st_mode) == rustix::fs::FileType::Directory {
        if metadata.st_dev != expected_device {
            return Err(OperationError::UnsafePath);
        }
        let child = rustix::fs::openat(
            parent,
            name,
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::DIRECTORY
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )
        .map_err(errno_io)?;
        let opened = rustix::fs::fstat(&child).map_err(errno_io)?;
        if opened.st_dev != metadata.st_dev || opened.st_ino != metadata.st_ino {
            return Err(OperationError::UnsafePath);
        }
        remove_directory_contents(&child, expected_device)?;
        rustix::fs::unlinkat(parent, name, rustix::fs::AtFlags::REMOVEDIR).map_err(errno_io)?;
    } else {
        rustix::fs::unlinkat(parent, name, rustix::fs::AtFlags::empty()).map_err(errno_io)?;
    }
    Ok(())
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
    models: &[PathBuf],
    agent_data_owner_uid: Option<u32>,
) -> Result<PathBuf, OperationError> {
    let agent_data = &roots.agent_data;
    let installations = agent_data.join("installations");
    let model_root = models
        .first()
        .and_then(|path| runtime_model_root(path, roots))
        .ok_or(OperationError::UnsafePath)?;
    if models
        .iter()
        .any(|path| runtime_model_root(path, roots).as_deref() != Some(model_root.as_path()))
    {
        return Err(OperationError::UnsafePath);
    }
    let installation = model_root.parent().ok_or(OperationError::UnsafePath)?;
    for path in [
        agent_data.as_path(),
        installations.as_path(),
        installation,
        model_root.as_path(),
    ] {
        require_safe_directory(path, agent_data_owner_uid)?;
    }
    let canonical_agent_data = agent_data
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_installations = installations
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_installation = installation
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_models = model_root
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    if canonical_installations.parent() != Some(canonical_agent_data.as_path())
        || canonical_installation.parent() != Some(canonical_installations.as_path())
        || canonical_models.parent() != Some(canonical_installation.as_path())
    {
        return Err(OperationError::UnsafePath);
    }
    Ok(canonical_models)
}

fn runtime_model_root(source: &Path, roots: &ManagedRoots) -> Option<PathBuf> {
    let installations = roots.agent_data.join("installations");
    let relative = source.strip_prefix(&installations).ok()?;
    let mut components = relative.components();
    let installation = match components.next()? {
        Component::Normal(value) if valid_artifact_id(&value.to_string_lossy()) => value,
        _ => return None,
    };
    if !matches!(components.next(), Some(Component::Normal(value)) if value == "models") {
        return None;
    }
    Some(installations.join(installation).join("models"))
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

fn require_runtime_directory(
    path: &Path,
    required_owner_uid: Option<u32>,
    runtime_uid: u32,
) -> Result<(), OperationError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.mode() & 0o002 != 0
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
        || metadata.mode() & 0o020 != 0 && !exact_runtime_acl(path, runtime_uid)
    {
        return Err(OperationError::UnsafePath);
    }
    Ok(())
}

fn exact_runtime_acl(path: &Path, runtime_uid: u32) -> bool {
    const ACL_VERSION: u32 = 0x0002;
    const USER_OBJ: u16 = 0x0001;
    const USER: u16 = 0x0002;
    const GROUP_OBJ: u16 = 0x0004;
    const MASK: u16 = 0x0010;
    const OTHER: u16 = 0x0020;
    let Ok(Some(value)) = xattr::get(path, "system.posix_acl_access") else {
        return false;
    };
    if value.len() < 4 || u32::from_le_bytes(value[..4].try_into().unwrap()) != ACL_VERSION {
        return false;
    }
    let entries = &value[4..];
    if entries.len() % 8 != 0 || entries.len() / 8 != 5 {
        return false;
    }
    let mut user_object = false;
    let mut runtime_user = false;
    let mut group_object = false;
    let mut mask = false;
    let mut other = false;
    for entry in entries.as_chunks::<8>().0.iter() {
        let (tag, permissions, identifier) = acl_entry(entry);
        match tag {
            USER_OBJ => user_object = permissions == 0o7,
            USER if identifier == runtime_uid => runtime_user = permissions == 0o7,
            GROUP_OBJ => group_object = permissions == 0,
            MASK => mask = permissions == 0o7,
            OTHER => other = permissions == 0,
            _ => return false,
        }
    }
    user_object && runtime_user && group_object && mask && other
}

/// Split one eight-byte POSIX ACL entry into its tag, permissions, and id.
fn acl_entry(entry: &[u8; 8]) -> (u16, u16, u32) {
    let [tag, permissions, low, high] = entry.as_chunks::<2>().0 else {
        unreachable!("an eight-byte entry yields four two-byte fields")
    };
    (
        u16::from_le_bytes(*tag),
        u16::from_le_bytes(*permissions),
        u32::from_le_bytes([low[0], low[1], high[0], high[1]]),
    )
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

fn runtime_generation_fence_filename(
    installation_id: uuid::Uuid,
    runtime_id: uuid::Uuid,
) -> String {
    format!("{installation_id}-{runtime_id}.json")
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

fn loaded_image_source(stdout: &[u8]) -> Result<Option<String>, ()> {
    let text = std::str::from_utf8(stdout).map_err(|_| ())?;
    let mut source = None;
    for line in text.lines() {
        let candidate = if let Some(value) = line.strip_prefix("Loaded image: ") {
            let value = value.trim();
            if value.is_empty()
                || value.len() > 256
                || value.starts_with('-')
                || value
                    .bytes()
                    .any(|byte| byte.is_ascii_control() || byte.is_ascii_whitespace())
            {
                return Err(());
            }
            value.to_owned()
        } else if let Some(value) = line.strip_prefix("Loaded image ID: ") {
            let value = value.trim();
            if !valid_oci_digest(value) {
                return Err(());
            }
            value.to_owned()
        } else {
            continue;
        };
        if source.replace(candidate).is_some() {
            return Err(());
        }
    }
    Ok(source)
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

fn runtime_archive_identity(metadata: &fs::Metadata) -> RuntimeArchiveIdentity {
    RuntimeArchiveIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
        bytes: metadata.len(),
        modified_seconds: metadata.mtime(),
        modified_nanoseconds: metadata.mtime_nsec(),
        changed_seconds: metadata.ctime(),
        changed_nanoseconds: metadata.ctime_nsec(),
    }
}

fn sync_directory(path: &Path) -> Result<(), OperationError> {
    OpenOptions::new().read(true).open(path)?.sync_all()?;
    Ok(())
}

fn read_agent_installation_file(
    path: &Path,
    owner_uid: Option<u32>,
    maximum_bytes: u64,
) -> Result<Vec<u8>, OperationError> {
    let path_metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
    if path_metadata.file_type().is_symlink()
        || !path_metadata.is_file()
        || path_metadata.nlink() != 1
        || path_metadata.mode() & 0o777 != 0o600
        || owner_uid.is_some_and(|uid| path_metadata.uid() != uid)
        || path_metadata.len() == 0
        || path_metadata.len() > maximum_bytes
    {
        return Err(OperationError::InvalidArtifact);
    }
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)
        .map_err(|_| OperationError::UnsafePath)?;
    let opened = file.metadata().map_err(|_| OperationError::UnsafePath)?;
    if artifact_identity(&opened) != artifact_identity(&path_metadata) {
        return Err(OperationError::InvalidArtifact);
    }
    let mut bytes = Vec::with_capacity(path_metadata.len() as usize);
    Read::by_ref(&mut file)
        .take(maximum_bytes.saturating_add(1))
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 != path_metadata.len() {
        return Err(OperationError::InvalidArtifact);
    }
    Ok(bytes)
}

fn read_helper_reconciliation_receipt(
    path: &Path,
    owner_uid: Option<u32>,
) -> Result<Option<InstallationReconciliationReceipt>, OperationError> {
    let mut file = match OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    let metadata = file.metadata()?;
    if !safe_custody_file(&metadata, owner_uid, metadata.len())
        || metadata.len() > MAX_INSTALLATION_RECONCILIATION_IDENTITY_BYTES
    {
        return Err(OperationError::InvalidArtifact);
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    Read::by_ref(&mut file)
        .take(MAX_INSTALLATION_RECONCILIATION_IDENTITY_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 != metadata.len() {
        return Err(OperationError::InvalidArtifact);
    }
    let receipt: InstallationReconciliationReceipt =
        parse_strict(&bytes).map_err(|_| OperationError::InvalidArtifact)?;
    receipt
        .identity
        .validate()
        .map_err(|_| OperationError::InvalidArtifact)?;
    if receipt.schema_version != INSTALLATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION
        || receipt.installation_inode == 0
        || canonical_json(&receipt).map_err(|_| OperationError::InvalidArtifact)? != bytes
    {
        return Err(OperationError::InvalidArtifact);
    }
    Ok(Some(receipt))
}

fn write_helper_reconciliation_receipt(
    root: &Path,
    path: &Path,
    receipt: &InstallationReconciliationReceipt,
) -> Result<(), OperationError> {
    receipt
        .identity
        .validate()
        .map_err(|_| OperationError::InvalidOperation)?;
    let bytes = canonical_json(receipt).map_err(|_| OperationError::InvalidOperation)?;
    if receipt.schema_version != INSTALLATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION
        || receipt.installation_inode == 0
        || bytes.len() as u64 > MAX_INSTALLATION_RECONCILIATION_IDENTITY_BYTES
    {
        return Err(OperationError::InvalidOperation);
    }
    let temporary = root.join(format!(".receipt-{}.tmp", uuid::Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    fs::rename(&temporary, path)?;
    sync_directory(root)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
    use std::path::{Path, PathBuf};
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};

    use tar::Builder;
    use tempfile::TempDir;

    use super::{
        AuthorizedRuntimeEffect, CommandOutput, CommandRunner, HostRuntimeAction,
        HostRuntimeRequest, INSTALLATION_RECONCILIATION_DIRECTORY, JobCancellationFence,
        MAX_COMMAND_OUTPUT_BYTES, MAX_COMPILED_MODEL_PATH_CHARS, ManagedRoots, OperationError,
        OperationExecutor, RUNTIME_GENERATION_FENCE_DIRECTORY,
        RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION, RuntimeEffectIdentity, RuntimeGenerationFenceUse,
        RuntimeImageReceipt, RuntimeRequestGrantBinding, bounded_container_wait_exit_code,
        hex_sha256, loaded_image_source, parse_publication, validate_docker_run,
    };
    use vonk_agent_protocol::generated::{
        CompiledExecutionPlan, RecipeStartPayload, RecipeStartPayloadMemoryKind, RecipeStopPayload,
    };
    use vonk_agent_protocol::{RecipeReconciliationIdentity, canonical_json};

    const RUN_ID: &str = "40000000-0000-4000-8000-000000000004";

    #[derive(Clone, Copy)]
    struct MissingContainerRunner;

    impl CommandRunner for MissingContainerRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new("/usr/bin/docker"));
            let daemon_probe = arguments.first().map(String::as_str) == Some("version");
            let missing_listing = arguments.first().map(String::as_str) == Some("container")
                && arguments.get(1).map(String::as_str) == Some("ls");
            Ok(CommandOutput {
                success: daemon_probe || missing_listing,
                stdout: Vec::new(),
                stderr: Vec::new(),
                exit_code: Some(if daemon_probe || missing_listing {
                    0
                } else {
                    1
                }),
            })
        }
    }

    struct ExistingRuntimeGenerationRunner {
        logical_run_id: uuid::Uuid,
        target_id: uuid::Uuid,
        installation_id: uuid::Uuid,
        run_generation: u32,
        plan_digest: String,
        calls: Arc<Mutex<Vec<Vec<String>>>>,
    }

    impl CommandRunner for ExistingRuntimeGenerationRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new("/usr/bin/docker"));
            self.calls.lock().unwrap().push(arguments.to_vec());
            let (success, stdout, exit_code) = match arguments.first().map(String::as_str) {
                Some("container") if arguments.get(1).map(String::as_str) == Some("inspect") => (
                    true,
                    format!(
                        "true\t{}\t{}\t{}\t{}\t{}\n",
                        self.logical_run_id,
                        self.target_id,
                        self.installation_id,
                        self.run_generation,
                        self.plan_digest
                    )
                    .into_bytes(),
                    0,
                ),
                Some("stop" | "rm") => (true, b"container-id\n".to_vec(), 0),
                _ => return Err("unexpected command".to_owned()),
            };
            Ok(CommandOutput {
                success,
                stdout,
                stderr: Vec::new(),
                exit_code: Some(exit_code),
            })
        }
    }

    #[derive(Clone)]
    struct ReconciliationListingRunner {
        response: CommandOutput,
        calls: Arc<Mutex<Vec<Vec<String>>>>,
    }

    impl ReconciliationListingRunner {
        fn new(response: CommandOutput) -> Self {
            Self {
                response,
                calls: Arc::new(Mutex::new(Vec::new())),
            }
        }
    }

    impl CommandRunner for ReconciliationListingRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new("/usr/bin/docker"));
            self.calls.lock().unwrap().push(arguments.to_vec());
            assert_eq!(arguments.first().map(String::as_str), Some("container"));
            assert_eq!(arguments.get(1).map(String::as_str), Some("ls"));
            Ok(self.response.clone())
        }
    }

    #[derive(Clone)]
    struct ReconciliationCleanupRunner {
        listing: CommandOutput,
        calls: Arc<Mutex<Vec<Vec<String>>>>,
    }

    impl ReconciliationCleanupRunner {
        fn new(listing: CommandOutput) -> Self {
            Self {
                listing,
                calls: Arc::new(Mutex::new(Vec::new())),
            }
        }
    }

    impl CommandRunner for ReconciliationCleanupRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new("/usr/bin/docker"));
            self.calls.lock().unwrap().push(arguments.to_vec());
            match arguments.get(1).map(String::as_str) {
                Some("ls") => Ok(self.listing.clone()),
                Some("rm") => Ok(CommandOutput {
                    success: true,
                    stdout: arguments
                        .get(2)
                        .map(|id| format!("{id}\n").into_bytes())
                        .unwrap_or_default(),
                    stderr: Vec::new(),
                    exit_code: Some(0),
                }),
                _ => panic!("unexpected Docker operation: {arguments:?}"),
            }
        }
    }

    fn helper_reconciliation_fixture() -> (
        TempDir,
        ManagedRoots,
        RecipeReconciliationIdentity,
        PathBuf,
        PathBuf,
    ) {
        let temp = tempfile::tempdir().unwrap();
        let data = temp.path().join("controller-data");
        let agent_data = temp.path().join("spark-agent-data");
        let roots = ManagedRoots::under(&data).with_agent_data(&agent_data);
        fs::create_dir_all(&roots.data).unwrap();
        let installation_id = uuid::Uuid::new_v4();
        let installation = agent_data
            .join("installations")
            .join(installation_id.to_string());
        let runtime_cache = installation.join("runtime-cache");
        let shared_cache = agent_data.join("shared-model-cache").join("model.bin");
        fs::create_dir_all(runtime_cache.join("home/private")).unwrap();
        fs::create_dir_all(shared_cache.parent().unwrap()).unwrap();
        fs::set_permissions(&installation, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(&runtime_cache, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(
            runtime_cache.join("home"),
            fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        fs::set_permissions(
            runtime_cache.join("home/private"),
            fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        fs::write(
            runtime_cache.join("home/private/private-cache.bin"),
            b"private",
        )
        .unwrap();
        fs::write(&shared_cache, b"shared model cache").unwrap();

        let recipe_digest = "e".repeat(64);
        let spec = serde_json::json!({
            "identity": {"recipe_revision_sha256": recipe_digest},
            "invalid legacy-shaped plan": {"opaque": [false, null]},
        });
        let spec_canonical = canonical_json(&spec).unwrap();
        let identity = RecipeReconciliationIdentity {
            compiled_spec_canonical_sha256: hex_sha256(&spec_canonical),
            install_operation_id: uuid::Uuid::new_v4(),
            install_operation_payload_sha256: "a".repeat(64),
            installation_id,
            node_id: format!("spk_{}", "b".repeat(32)),
            plan_digest: "c".repeat(64),
            recipe_content_sha256: recipe_digest.clone(),
            recipe_revision_id: uuid::Uuid::new_v4(),
            schema_version: 1,
        };
        fs::write(
            installation.join("spec.json"),
            serde_json::to_vec(&spec).unwrap(),
        )
        .unwrap();
        fs::set_permissions(
            installation.join("spec.json"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        fs::write(
            installation.join("recipe-content.sha256"),
            recipe_digest.as_bytes(),
        )
        .unwrap();
        fs::set_permissions(
            installation.join("recipe-content.sha256"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        (temp, roots, identity, runtime_cache, shared_cache)
    }

    #[derive(Clone, Copy)]
    struct DeniedContainerInspectRunner;

    impl CommandRunner for DeniedContainerInspectRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new("/usr/bin/docker"));
            let daemon_probe = arguments.first().map(String::as_str) == Some("version");
            Ok(CommandOutput {
                success: daemon_probe,
                stdout: Vec::new(),
                stderr: Vec::new(),
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
                stderr: Vec::new(),
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
            let digest_lookup =
                inspect && arguments.last().is_some_and(|image| image.contains('@'));
            Ok(CommandOutput {
                success: !digest_lookup,
                stdout: if arguments.first().map(String::as_str) == Some("load") {
                    b"Loaded image: localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002:latest\n"
                        .to_vec()
                } else if digest_lookup {
                    b"\n".to_vec()
                } else if inspect && !digest_lookup {
                    format!("sha256:{}\tlinux\tarm64\tv1\t10001:10001\n", "b".repeat(64))
                        .into_bytes()
                } else {
                    Vec::new()
                },
                stderr: Vec::new(),
                exit_code: Some(if digest_lookup { 1 } else { 0 }),
            })
        }
    }

    #[derive(Clone, Default)]
    struct CountingRuntimeImportRunner {
        loads: Arc<std::sync::atomic::AtomicUsize>,
        image_missing: Arc<std::sync::atomic::AtomicBool>,
        wrong_image: Arc<std::sync::atomic::AtomicBool>,
        malformed_image: Arc<std::sync::atomic::AtomicBool>,
        empty_listing: Arc<std::sync::atomic::AtomicBool>,
        deny_listing: Arc<std::sync::atomic::AtomicBool>,
    }

    impl CommandRunner for CountingRuntimeImportRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            use std::sync::atomic::Ordering::SeqCst;
            let inspect_compiled = arguments.first().map(String::as_str) == Some("image")
                && arguments.get(1).map(String::as_str) == Some("inspect")
                && arguments
                    .last()
                    .is_some_and(|image| image.contains("compiled-runtime-"));
            if inspect_compiled && self.image_missing.load(SeqCst) {
                return Ok(CommandOutput {
                    success: false,
                    stdout: b"\n".to_vec(),
                    exit_code: Some(1),
                    stderr: Vec::new(),
                });
            }
            if inspect_compiled && self.wrong_image.load(SeqCst) {
                return Ok(CommandOutput {
                    success: true,
                    stdout: format!("sha256:{}\tlinux\tarm64\tv1\t10001:10001\n", "c".repeat(64))
                        .into_bytes(),
                    exit_code: Some(0),
                    stderr: Vec::new(),
                });
            }
            if inspect_compiled && self.malformed_image.load(SeqCst) {
                return Ok(CommandOutput {
                    success: true,
                    stdout: b"malformed\n".to_vec(),
                    exit_code: Some(0),
                    stderr: Vec::new(),
                });
            }
            if arguments.first().map(String::as_str) == Some("image")
                && arguments.get(1).map(String::as_str) == Some("ls")
            {
                if self.deny_listing.load(SeqCst) {
                    return Err("docker access denied".to_owned());
                }
                return Ok(CommandOutput {
                    success: true,
                    stdout: if self.image_missing.load(SeqCst) || self.empty_listing.load(SeqCst) {
                        Vec::new()
                    } else {
                        format!("sha256:{}\n", "b".repeat(64)).into_bytes()
                    },
                    exit_code: Some(0),
                    stderr: Vec::new(),
                });
            }
            if arguments.first().map(String::as_str) == Some("load") {
                self.loads.fetch_add(1, SeqCst);
                self.image_missing.store(false, SeqCst);
            }
            RuntimeImportRunner.run(executable, arguments)
        }
    }

    #[derive(Clone, Default)]
    struct KernelAclRunner {
        calls: Arc<Mutex<Vec<Vec<String>>>>,
    }

    impl CommandRunner for KernelAclRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            if executable != Path::new("/usr/bin/setfacl") {
                return Err("unexpected command".to_owned());
            }
            self.calls.lock().unwrap().push(arguments.to_vec());
            if arguments.first().map(String::as_str) == Some("-R") {
                let uid = arguments
                    .get(2)
                    .and_then(|value| value.split(':').nth(1))
                    .and_then(|value| value.parse::<u32>().ok())
                    .ok_or("invalid ACL user")?;
                let root = Path::new(arguments.last().ok_or("missing ACL path")?);
                apply_kernel_read_acl(root, uid)?;
            }
            Ok(CommandOutput {
                success: true,
                stdout: Vec::new(),
                stderr: Vec::new(),
                exit_code: Some(0),
            })
        }
    }

    fn apply_kernel_read_acl(path: &Path, uid: u32) -> Result<(), String> {
        let metadata = fs::symlink_metadata(path).map_err(|error| error.to_string())?;
        let mode = metadata.mode() & 0o777;
        let user_object = ((mode >> 6) & 0o7) as u16;
        let group_object = ((mode >> 3) & 0o7) as u16;
        let other = (mode & 0o7) as u16;
        let runtime = if metadata.is_dir() || mode & 0o111 != 0 {
            0o5
        } else {
            0o4
        };
        let mut acl = 2_u32.to_le_bytes().to_vec();
        for (tag, permissions, identifier) in [
            (0x0001_u16, user_object, u32::MAX),
            (0x0002, runtime, uid),
            (0x0004, group_object, u32::MAX),
            (0x0010, group_object | runtime, u32::MAX),
            (0x0020, other, u32::MAX),
        ] {
            acl.extend_from_slice(&tag.to_le_bytes());
            acl.extend_from_slice(&permissions.to_le_bytes());
            acl.extend_from_slice(&identifier.to_le_bytes());
        }
        xattr::set(path, "system.posix_acl_access", &acl).map_err(|error| error.to_string())?;
        if metadata.is_dir() {
            for entry in fs::read_dir(path).map_err(|error| error.to_string())? {
                apply_kernel_read_acl(&entry.map_err(|error| error.to_string())?.path(), uid)?;
            }
        }
        Ok(())
    }

    #[derive(Clone, Default)]
    struct NoLoadIdentityRunner {
        calls: Arc<Mutex<Vec<Vec<String>>>>,
    }

    impl CommandRunner for NoLoadIdentityRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            self.calls.lock().unwrap().push(arguments.to_vec());
            if arguments.first().map(String::as_str) == Some("load") {
                return Ok(CommandOutput {
                    success: true,
                    stdout: Vec::new(),
                    exit_code: Some(0),
                    stderr: Vec::new(),
                });
            }
            RuntimeImportRunner.run(executable, arguments)
        }
    }

    #[test]
    fn loaded_image_source_accepts_only_a_single_safe_load_line() {
        assert_eq!(
            loaded_image_source(
                b"Loaded image: localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002:latest\n"
            ),
            Ok(Some(
                "localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002:latest"
                    .to_owned()
            ))
        );
        assert_eq!(
            loaded_image_source(
                b"Loaded image ID: sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662\n"
            ),
            Ok(Some(
                "sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
                    .to_owned()
            ))
        );
        assert_eq!(loaded_image_source(b"Loaded image: unsafe tag\n"), Err(()));
        assert_eq!(
            loaded_image_source(b"Loaded image ID: sha256:deadbeef\n"),
            Err(())
        );
        assert_eq!(loaded_image_source(b"Loaded image: --help\n"), Err(()));
        assert_eq!(
            loaded_image_source(
                b"Loaded image: localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002:latest\nLoaded image ID: sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662\n"
            ),
            Err(())
        );
        assert_eq!(
            loaded_image_source(
                b"Loaded image: localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002:latest\nLoaded image: localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000003:latest\n"
            ),
            Err(())
        );
        assert_eq!(loaded_image_source(b"docker load completed\n"), Ok(None));
    }

    #[test]
    fn job_cancellation_fence_tracks_only_the_exact_runtime_generation() {
        let fence = JobCancellationFence::default();
        let identity = runtime_effect_identity(1);
        let active = fence.begin(identity).unwrap();
        assert!(fence.is_active(identity).unwrap());
        fence.cancel(identity).unwrap();
        assert!(fence.was_cancelled(identity).unwrap());
        assert!(fence.begin(identity).is_err());
        drop(active);
        assert!(!fence.is_active(identity).unwrap());
        assert!(!fence.was_cancelled(identity).unwrap());
    }

    fn runtime_effect_identity(run_generation: u32) -> RuntimeEffectIdentity {
        RuntimeEffectIdentity {
            runtime_id: uuid::Uuid::parse_str(RUN_ID).unwrap(),
            installation_id: uuid::Uuid::parse_str("50000000-0000-4000-8000-000000000005").unwrap(),
            run_generation,
        }
    }

    fn compiled_plan_for_runtime_authority() -> CompiledExecutionPlan {
        let mut value: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/compiled_workload_v2.json"))
                .unwrap();
        value["runtime"]["placement"]["endpoint_address"] = serde_json::json!("100.100.20.30");
        value["security"]["network_mode"] = serde_json::json!("bridge");
        let compiled: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        compiled.validate().unwrap();
        compiled
    }

    fn recipe_start_plan_for_authority(run_generation: u32) -> RecipeStartPayload {
        let compiled = compiled_plan_for_runtime_authority();
        let placement = &compiled.runtime.placement;
        let memory_kind = match placement.memory_kind.to_string().as_str() {
            "unified" => RecipeStartPayloadMemoryKind::Unified,
            "host" => RecipeStartPayloadMemoryKind::Host,
            "accelerator" => RecipeStartPayloadMemoryKind::Accelerator,
            other => panic!("unexpected compiled memory kind {other}"),
        };
        RecipeStartPayload {
            alias: "test-model".to_owned(),
            compiled_execution_plan: compiled.clone(),
            endpoint_address: "100.100.20.30".parse().unwrap(),
            image_digest: compiled.runtime.image_digest.clone(),
            installation_id: runtime_effect_identity(run_generation).installation_id,
            local_address: placement.local_address,
            mapping_generation: 1,
            mapping_id: uuid::Uuid::parse_str("70000000-0000-4000-8000-000000000007").unwrap(),
            master_address: placement.master_address,
            master_port: placement.master_port,
            memory_floor_bytes: placement.memory_floor_bytes,
            memory_kind,
            phase: None,
            plan_digest: "c".repeat(64),
            port: placement.port.unwrap(),
            rank: placement.rank,
            recipe_content_sha256: compiled.identity.recipe_revision_sha256.clone(),
            recipe_revision_id: uuid::Uuid::parse_str("60000000-0000-4000-8000-000000000006")
                .unwrap(),
            reserved_memory_bytes: placement.reserved_memory_bytes,
            role: placement.role.clone(),
            run_generation,
            run_id: uuid::Uuid::parse_str(RUN_ID).unwrap(),
            schema_version: 2,
            start_deadline: None,
            world_size: placement.world_size,
        }
    }

    fn recipe_stop_plan_for_authority(
        run_generation: u32,
        cancel_pending_start: bool,
    ) -> RecipeStopPayload {
        let compiled = compiled_plan_for_runtime_authority();
        let placement = &compiled.runtime.placement;
        RecipeStopPayload {
            cancel_pending_start,
            compiled_execution_plan: compiled.clone(),
            installation_id: runtime_effect_identity(run_generation).installation_id,
            mapping_generation: 1,
            mapping_id: uuid::Uuid::parse_str("70000000-0000-4000-8000-000000000007").unwrap(),
            node_id: "spk_11111111111111111111111111111111".to_owned(),
            plan_digest: "c".repeat(64),
            rank: placement.rank,
            recipe_content_sha256: compiled.identity.recipe_revision_sha256.clone(),
            recipe_revision_id: uuid::Uuid::parse_str("60000000-0000-4000-8000-000000000006")
                .unwrap(),
            role: placement.role.clone(),
            run_generation,
            run_id: uuid::Uuid::parse_str(RUN_ID).unwrap(),
            schema_version: 2,
            target_runtime_id: uuid::Uuid::parse_str(RUN_ID).unwrap(),
            world_size: placement.world_size,
        }
    }

    fn runtime_request_identity(
        job_id: &uuid::Uuid,
        operation_id: &uuid::Uuid,
        fence: &uuid::Uuid,
        action: HostRuntimeAction,
        arguments: Vec<String>,
        run_generation: u32,
        start_plan: Option<RecipeStartPayload>,
        stop_plan: Option<RecipeStopPayload>,
    ) -> HostRuntimeRequest {
        HostRuntimeRequest {
            schema_version: 1,
            action,
            job_id: *job_id,
            operation_id: *operation_id,
            attempt: 1,
            fence: *fence,
            arguments,
            observation: None,
            installation_id: None,
            reconciliation_identity: None,
            job_plan: None,
            run_generation: Some(run_generation),
            start_plan,
            stop_plan,
        }
    }

    #[test]
    fn start_authority_binds_generation_plan_identity_and_projected_arguments() {
        let temp = tempfile::tempdir().unwrap();
        let executor = OperationExecutor::new(
            ManagedRoots::under(temp.path()),
            &[0; 32],
            MissingContainerRunner,
            None,
        )
        .unwrap();
        let start_plan = recipe_start_plan_for_authority(1);
        let arguments = executor
            .projected_runtime_arguments(
                &start_plan.compiled_execution_plan,
                start_plan.installation_id,
                start_plan.run_id,
            )
            .unwrap();
        let job_id = uuid::Uuid::new_v4();
        let operation_id = uuid::Uuid::new_v4();
        let fence = uuid::Uuid::new_v4();
        let request = runtime_request_identity(
            &job_id,
            &operation_id,
            &fence,
            HostRuntimeAction::Start,
            arguments,
            start_plan.run_generation,
            Some(start_plan.clone()),
            None,
        );
        let plan_sha256 = hex_sha256(&canonical_json(&start_plan).unwrap());
        let grant = RuntimeRequestGrantBinding {
            job_id: &job_id,
            operation_id: &operation_id,
            attempt: 1,
            fence: &fence,
            installation_id: None,
            reconciliation_identity: None,
            start_plan_sha256: Some(&plan_sha256),
            stop_plan_sha256: None,
            run_generation: Some(start_plan.run_generation),
            runtime_run_id: Some(&start_plan.run_id),
            runtime_target_id: Some(&start_plan.run_id),
            runtime_installation_id: Some(&start_plan.installation_id),
        };
        let authorized = executor
            .authorize_runtime_effect(
                &request,
                grant,
                Some("spk_11111111111111111111111111111111"),
            )
            .unwrap()
            .unwrap();
        assert!(matches!(
            authorized,
            AuthorizedRuntimeEffect::Start {
                identity: RuntimeEffectIdentity {
                    runtime_id,
                    installation_id,
                    run_generation: 1,
                },
                logical_run_id,
                plan_digest,
            } if runtime_id == start_plan.run_id
                && installation_id == start_plan.installation_id
                && logical_run_id == start_plan.run_id
                && plan_digest == start_plan.plan_digest
        ));

        let mut caller_argv = request.clone();
        caller_argv.arguments.push("--privileged".to_owned());
        assert!(matches!(
            executor.authorize_runtime_effect(&caller_argv, grant, None),
            Err(OperationError::InvalidOperation)
        ));

        let mut stale_grant = request.clone();
        stale_grant.run_generation = Some(2);
        assert!(matches!(
            executor.authorize_runtime_effect(&stale_grant, grant, None),
            Err(OperationError::InvalidOperation)
        ));

        let mut mutated_plan = request.clone();
        mutated_plan.start_plan.as_mut().unwrap().plan_digest = "d".repeat(64);
        assert!(matches!(
            executor.authorize_runtime_effect(&mutated_plan, grant, None),
            Err(OperationError::InvalidOperation)
        ));

        let mut hook_plan = start_plan.clone();
        hook_plan
            .compiled_execution_plan
            .lifecycle
            .pre_start
            .push(vec!["/usr/bin/true".to_owned()]);
        let hook_arguments = executor
            .projected_runtime_arguments(
                &hook_plan.compiled_execution_plan,
                hook_plan.installation_id,
                hook_plan.run_id,
            )
            .unwrap();
        let hook_request = runtime_request_identity(
            &job_id,
            &operation_id,
            &fence,
            HostRuntimeAction::Start,
            hook_arguments,
            hook_plan.run_generation,
            Some(hook_plan.clone()),
            None,
        );
        let hook_sha256 = hex_sha256(&canonical_json(&hook_plan).unwrap());
        let hook_grant = RuntimeRequestGrantBinding {
            start_plan_sha256: Some(&hook_sha256),
            ..grant
        };
        assert!(matches!(
            executor.authorize_runtime_effect(&hook_request, hook_grant, None),
            Err(OperationError::InvalidOperation)
        ));
    }

    #[test]
    fn stop_authority_binds_exact_target_node_plan_and_cancellation_semantics() {
        let temp = tempfile::tempdir().unwrap();
        let executor = OperationExecutor::new(
            ManagedRoots::under(temp.path()),
            &[0; 32],
            MissingContainerRunner,
            None,
        )
        .unwrap();
        let stop_plan = recipe_stop_plan_for_authority(1, true);
        let job_id = uuid::Uuid::new_v4();
        let operation_id = uuid::Uuid::new_v4();
        let fence = uuid::Uuid::new_v4();
        let request = runtime_request_identity(
            &job_id,
            &operation_id,
            &fence,
            HostRuntimeAction::Stop,
            Vec::new(),
            stop_plan.run_generation,
            None,
            Some(stop_plan.clone()),
        );
        let plan_sha256 = hex_sha256(&canonical_json(&stop_plan).unwrap());
        let grant = RuntimeRequestGrantBinding {
            job_id: &job_id,
            operation_id: &operation_id,
            attempt: 1,
            fence: &fence,
            installation_id: None,
            reconciliation_identity: None,
            start_plan_sha256: None,
            stop_plan_sha256: Some(&plan_sha256),
            run_generation: Some(stop_plan.run_generation),
            runtime_run_id: Some(&stop_plan.run_id),
            runtime_target_id: Some(&stop_plan.target_runtime_id),
            runtime_installation_id: Some(&stop_plan.installation_id),
        };
        let authorized = executor
            .authorize_runtime_effect(&request, grant, Some(&stop_plan.node_id))
            .unwrap()
            .unwrap();
        assert!(matches!(
            authorized,
            AuthorizedRuntimeEffect::Stop {
                identity: RuntimeEffectIdentity {
                    runtime_id,
                    installation_id,
                    run_generation: 1,
                },
                logical_run_id,
                cancel_pending_start: true,
                ..
            } if runtime_id == stop_plan.target_runtime_id
                && installation_id == stop_plan.installation_id
                && logical_run_id == stop_plan.run_id
        ));
        assert!(matches!(
            executor.authorize_runtime_effect(
                &request,
                grant,
                Some("spk_22222222222222222222222222222222")
            ),
            Err(OperationError::InvalidOperation)
        ));

        let mut empty_argv = request.clone();
        empty_argv.arguments.push("ignored-argv".to_owned());
        assert!(matches!(
            executor.authorize_runtime_effect(&empty_argv, grant, Some(&stop_plan.node_id)),
            Err(OperationError::InvalidOperation)
        ));

        let mut different_generation = request.clone();
        different_generation.run_generation = Some(2);
        assert!(matches!(
            executor.authorize_runtime_effect(
                &different_generation,
                grant,
                Some(&stop_plan.node_id)
            ),
            Err(OperationError::InvalidOperation)
        ));

        let mut different_target = request.clone();
        different_target
            .stop_plan
            .as_mut()
            .unwrap()
            .target_runtime_id = uuid::Uuid::new_v4();
        assert!(matches!(
            executor.authorize_runtime_effect(&different_target, grant, Some(&stop_plan.node_id)),
            Err(OperationError::InvalidOperation)
        ));

        let mut hook_plan = stop_plan.clone();
        hook_plan
            .compiled_execution_plan
            .lifecycle
            .post_stop
            .push(vec!["/usr/bin/true".to_owned()]);
        let hook_request = runtime_request_identity(
            &job_id,
            &operation_id,
            &fence,
            HostRuntimeAction::Stop,
            Vec::new(),
            hook_plan.run_generation,
            None,
            Some(hook_plan.clone()),
        );
        let hook_sha256 = hex_sha256(&canonical_json(&hook_plan).unwrap());
        let hook_grant = RuntimeRequestGrantBinding {
            stop_plan_sha256: Some(&hook_sha256),
            ..grant
        };
        assert!(matches!(
            executor.authorize_runtime_effect(&hook_request, hook_grant, Some(&hook_plan.node_id)),
            Err(OperationError::InvalidOperation)
        ));
    }

    #[test]
    fn cancelled_generation_fence_survives_helper_restart_and_allows_newer_start() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        let first_helper =
            OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
        let old = runtime_effect_identity(1);

        first_helper
            .update_runtime_generation_fence(
                &old,
                RuntimeGenerationFenceUse::Stop {
                    cancel_pending_start: true,
                },
            )
            .expect("Stop(true) durably cancels its generation before checking effects");
        assert!(
            temp.path()
                .join(RUNTIME_GENERATION_FENCE_DIRECTORY)
                .is_dir()
        );
        let stored = first_helper
            .read_runtime_generation_fence(old.installation_id, old.runtime_id)
            .unwrap()
            .unwrap();
        assert_eq!(stored.highest_generation, old.run_generation);
        assert!(stored.cancelled);

        let restarted_helper =
            OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
        assert!(matches!(
            restarted_helper
                .update_runtime_generation_fence(&old, RuntimeGenerationFenceUse::Start),
            Err(OperationError::InvalidOperation)
        ));
        let current = RuntimeEffectIdentity {
            run_generation: 2,
            ..old
        };
        restarted_helper
            .update_runtime_generation_fence(&current, RuntimeGenerationFenceUse::Start)
            .expect("a newer authorized generation replaces the cancellation fence");
        let stored = restarted_helper
            .read_runtime_generation_fence(current.installation_id, current.runtime_id)
            .unwrap()
            .unwrap();
        assert_eq!(stored.highest_generation, 2);
        assert!(!stored.cancelled);
    }

    #[test]
    fn ordinary_stop_keeps_same_generation_retry_available_after_restart() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        let first_helper =
            OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
        let identity = runtime_effect_identity(1);

        first_helper
            .update_runtime_generation_fence(
                &identity,
                RuntimeGenerationFenceUse::Stop {
                    cancel_pending_start: false,
                },
            )
            .unwrap();
        let restarted_helper =
            OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
        restarted_helper
            .update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start)
            .expect("ordinary Stop must not cancel a retry in its authorized generation");
        let stored = restarted_helper
            .read_runtime_generation_fence(identity.installation_id, identity.runtime_id)
            .unwrap()
            .unwrap();
        assert_eq!(stored.highest_generation, identity.run_generation);
        assert!(!stored.cancelled);
    }

    #[test]
    fn newer_generation_survives_failed_start_and_old_exact_stop_without_rewinding_fence() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        let old = runtime_effect_identity(1);
        let current = runtime_effect_identity(2);
        let old_plan_digest = "a".repeat(64);
        let current_plan_digest = "b".repeat(64);
        let runner = ExistingRuntimeGenerationRunner {
            logical_run_id: old.runtime_id,
            target_id: old.runtime_id,
            installation_id: old.installation_id,
            run_generation: old.run_generation,
            plan_digest: old_plan_digest.clone(),
            calls: Arc::default(),
        };
        let calls = runner.calls.clone();
        let executor = OperationExecutor::new(roots, &[0; 32], runner, None).unwrap();
        // A new authorized Start reserves its generation before validating or
        // invoking Docker. Force it to fail at the malformed launch boundary,
        // then prove the old named container is still rejected as generation
        // 1 and can only be removed by its own exact Stop.
        assert!(matches!(
            executor.runtime_start_authorized(
                &[],
                current,
                current.runtime_id,
                &current_plan_digest,
            ),
            Err(OperationError::InvalidOperation)
        ));

        // The old container cannot be mistaken for generation 2 or removed
        // by a Stop for that generation.
        assert!(matches!(
            executor.runtime_stop_once(current, current.runtime_id, &current_plan_digest, 1,),
            Err(OperationError::InvalidArtifact)
        ));
        executor
            .runtime_stop_authorized(old, old.runtime_id, &old_plan_digest, 1, true)
            .expect("a stale exact Stop may clean its own generation");
        let recorded_calls = calls.lock().unwrap();
        assert!(
            recorded_calls
                .iter()
                .any(|call| call.first().map(String::as_str) == Some("stop"))
        );
        assert!(
            recorded_calls
                .iter()
                .any(|call| call.first().map(String::as_str) == Some("rm"))
        );
        drop(recorded_calls);
        let stored = executor
            .read_runtime_generation_fence(current.installation_id, current.runtime_id)
            .unwrap()
            .unwrap();
        assert_eq!(stored.highest_generation, 2);
        assert!(!stored.cancelled);
        assert!(matches!(
            executor.update_runtime_generation_fence(&old, RuntimeGenerationFenceUse::Start),
            Err(OperationError::InvalidOperation)
        ));
        executor
            .update_runtime_generation_fence(&current, RuntimeGenerationFenceUse::Start)
            .expect("the newer generation remains eligible for retry");
    }

    #[test]
    fn stop_deadline_never_reports_success_while_a_slow_start_remains_active() {
        let fence = JobCancellationFence::default();
        let identity = runtime_effect_identity(1);
        let _active = fence.begin(identity).unwrap();
        fence.cancel(identity).unwrap();

        assert!(matches!(
            fence.wait_for_active_start(identity, Instant::now()),
            Err(OperationError::StopUncertain)
        ));
    }

    #[test]
    fn exact_cancel_stop_waits_for_active_start_then_blocks_late_start() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
        let identity = runtime_effect_identity(1);
        executor
            .update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start)
            .unwrap();
        let active = executor.job_cancellation.begin(identity).unwrap();
        std::thread::scope(|scope| {
            let stop = scope.spawn(|| {
                executor.runtime_stop_authorized(
                    identity,
                    identity.runtime_id,
                    &"a".repeat(64),
                    1,
                    true,
                )
            });
            while !executor.job_cancellation.was_cancelled(identity).unwrap() {
                std::thread::yield_now();
            }
            assert!(!stop.is_finished(), "stop acknowledged an active START");
            drop(active);
            stop.join().unwrap().unwrap();
        });
        let restarted_helper =
            OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
        assert!(matches!(
            restarted_helper
                .update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start),
            Err(OperationError::InvalidOperation)
        ));
    }

    #[test]
    fn ordinary_recovery_stop_does_not_fence_a_same_run_restart() {
        let temp = tempfile::tempdir().unwrap();
        let executor = OperationExecutor::new(
            ManagedRoots::under(temp.path()),
            &[0; 32],
            MissingContainerRunner,
            None,
        )
        .unwrap();

        let identity = runtime_effect_identity(1);
        executor
            .runtime_stop_authorized(identity, identity.runtime_id, &"a".repeat(64), 1, false)
            .unwrap();
        executor
            .update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start)
            .expect("ordinary Stop does not cancel a same-generation retry");
    }

    #[test]
    fn stop_rejects_unproven_container_absence_even_when_daemon_is_healthy() {
        let temp = tempfile::tempdir().unwrap();
        let executor = OperationExecutor::new(
            ManagedRoots::under(temp.path()),
            &[0; 32],
            DeniedContainerInspectRunner,
            None,
        )
        .unwrap();

        assert!(matches!(
            executor.runtime_stop_once(
                runtime_effect_identity(1),
                uuid::Uuid::parse_str(RUN_ID).unwrap(),
                &"a".repeat(64),
                5,
            ),
            Err(OperationError::CommandFailed)
        ));
    }

    #[test]
    fn job_wait_preserves_only_bounded_container_exit_statuses() {
        assert_eq!(
            bounded_container_wait_exit_code(&CommandOutput {
                success: true,
                stdout: b"37\n".to_vec(),
                exit_code: Some(0),
                stderr: Vec::new(),
            }),
            37
        );
        for output in [b"".as_slice(), b"-1", b"256", b"invalid"] {
            assert_eq!(
                bounded_container_wait_exit_code(&CommandOutput {
                    success: true,
                    stdout: output.to_vec(),
                    exit_code: Some(0),
                    stderr: Vec::new(),
                }),
                1
            );
        }
    }

    fn runtime_fixture() -> (TempDir, ManagedRoots) {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(&temp.path().join("data"));
        initialize_runtime_fixture(&roots);
        (temp, roots)
    }

    fn runtime_fixture_with_separate_agent_data() -> (TempDir, ManagedRoots) {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(&temp.path().join("data"))
            .with_agent_data(&temp.path().join("agent-data"));
        initialize_runtime_fixture(&roots);
        (temp, roots)
    }

    fn initialize_runtime_fixture(roots: &ManagedRoots) {
        fs::create_dir_all(runtime_models(roots).join("primary")).unwrap();
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
    }

    fn artifact_path(roots: &ManagedRoots, key: char) -> PathBuf {
        runtime_models(roots)
            .join("primary")
            .join(format!("artifact-{key}.bin"))
    }

    fn runtime_models(roots: &ManagedRoots) -> PathBuf {
        roots
            .agent_data
            .join("installations")
            .join("installation-1")
            .join("models")
    }

    fn docker_save_archive(root_config: bool) -> (Vec<u8>, String) {
        let config = br#"{"config":{"User":"10001:10001"}}"#;
        let config_digest = hex_sha256(config);
        let config_member = if root_config {
            format!("{config_digest}.json")
        } else {
            format!("blobs/sha256/{config_digest}")
        };
        let unrelated_blob = format!("blobs/sha256/{}", "d".repeat(64));
        let other_manifest = format!("blobs/sha256/{}", "e".repeat(64));
        let archive = docker_config_archive(
            &config_member,
            &[
                (&unrelated_blob, b"layer payload"),
                (&config_member, config),
                (&other_manifest, b"unrelated OCI manifest"),
            ],
        );
        (archive, format!("sha256:{config_digest}"))
    }

    fn docker_config_archive(config_member: &str, members: &[(&str, &[u8])]) -> Vec<u8> {
        let manifest = serde_json::to_vec(&serde_json::json!([
            {
                "Config": config_member,
                "RepoTags": ["localhost/vonk/test:latest"],
                "Layers": [],
            }
        ]))
        .unwrap();
        let mut builder = Builder::new(Vec::new());
        let mut header = tar::Header::new_gnu();
        header.set_size(manifest.len() as u64);
        header.set_mode(0o600);
        header.set_cksum();
        builder
            .append_data(&mut header, "manifest.json", manifest.as_slice())
            .unwrap();
        for (name, bytes) in members {
            let mut header = tar::Header::new_gnu();
            header.set_size(bytes.len() as u64);
            header.set_mode(0o600);
            header.set_cksum();
            builder.append_data(&mut header, name, *bytes).unwrap();
        }
        builder.into_inner().unwrap()
    }

    #[test]
    fn runtime_archive_config_digest_selects_exact_config_in_both_docker_layouts() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("image.tar");
        for root_config in [true, false] {
            let (payload, config_id) = docker_save_archive(root_config);
            fs::write(&archive, payload).unwrap();
            assert_eq!(
                super::runtime_archive_config_digest(&archive).unwrap(),
                config_id
            );
        }
    }

    #[test]
    fn runtime_archive_config_digest_rejects_ambiguous_or_tampered_members() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("image.tar");
        let config = b"{}";
        let config_name = format!("{}.json", hex_sha256(config));
        let valid_member = (config_name.as_str(), config.as_slice());
        let duplicate_config = [valid_member, valid_member];
        let duplicate_manifest = [valid_member, ("manifest.json", b"[]".as_slice())];
        let tampered_config = [(config_name.as_str(), b"changed".as_slice())];
        for members in [
            duplicate_config.as_slice(),
            duplicate_manifest.as_slice(),
            tampered_config.as_slice(),
        ] {
            fs::write(&archive, docker_config_archive(&config_name, members)).unwrap();
            assert!(super::runtime_archive_config_digest(&archive).is_err());
        }
        fs::write(
            &archive,
            docker_config_archive(&format!("../{config_name}"), &[valid_member]),
        )
        .unwrap();
        assert!(super::runtime_archive_config_digest(&archive).is_err());
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
        let model_set = runtime_models(&roots).join("a".repeat(64));
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
    fn runtime_accepts_mixed_case_long_model_filename() {
        let (_temp, roots) = runtime_fixture_with_separate_agent_data();
        let filename =
            "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf";
        let source = runtime_models(&roots).join("primary").join(filename);
        fs::create_dir_all(source.parent().unwrap()).unwrap();
        fs::write(&source, b"model fixture").unwrap();
        let target = format!("/models/primary/{filename}");
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(source, &target, true)]),
                &roots,
                None,
            )
            .is_ok()
        );
    }

    #[test]
    fn runtime_accepts_canonical_unicode_space_and_underscore_model_filename() {
        let (_temp, roots) = runtime_fixture_with_separate_agent_data();
        let filename = "模型 weights_file.safetensors";
        let source = runtime_models(&roots).join("primary").join(filename);
        fs::create_dir_all(source.parent().unwrap()).unwrap();
        fs::write(&source, b"model fixture").unwrap();
        let target = format!("/models/primary/{filename}");
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(source, &target, true)]),
                &roots,
                None,
            )
            .is_ok()
        );
    }

    #[test]
    fn compiled_workload_fixture_reaches_helper_validation_with_scoped_receipts() {
        let plan: vonk_agent_protocol::compiled_execution_plan::CompiledExecutionPlan =
            serde_json::from_str(include_str!("../tests/fixtures/compiled_workload_v2.json"))
                .unwrap();
        plan.validate().unwrap();
        assert_eq!(plan.schema_version, 2);
        assert_eq!(plan.runtime.executable, "/opt/vonk/bin/vllm");
        assert_eq!(plan.runtime_image.distribution_object.kind, "oci-archive");
        assert!(!plan.security.host_network);

        let (_temp, roots) = runtime_fixture();
        let model_set = runtime_models(&roots).join(&plan.identity.model_artifact_set_sha256);
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
            plan.runtime_image.oci_layout_sha256, plan.runtime_image.image_digest,
        );
        let validated = validate_docker_run(&arguments, &roots, None).unwrap();
        assert_eq!(validated.models.len(), 2);
        assert_eq!(
            validated.platform_manifest_digest,
            plan.runtime.image_digest
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
    fn native_fabric_requires_complete_bounded_shape_without_publications() {
        let (temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::create_dir_all(&model).unwrap();
        let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
        let network = arguments.iter().position(|value| value == "none").unwrap();
        arguments[network] = "host".to_owned();
        let image = arguments
            .iter()
            .position(|value| value.starts_with("localhost/vonk/"))
            .unwrap();
        arguments.splice(
            image..image,
            [
                "--device",
                "nvidia.com/gpu=all",
                "--device",
                "/dev/infiniband:/dev/infiniband",
                "--ulimit",
                "memlock=-1:-1",
                "--ulimit",
                "stack=67108864:67108864",
                "--env",
                "VONK_MASTER_PORT=29500",
                "--env",
                "VONK_RANK=0",
                "--env",
                "VONK_WORLD_SIZE=2",
                "--env",
                "VONK_LOCAL_ADDR=192.168.100.10",
                "--env",
                "VONK_MASTER_ADDR=192.168.100.10",
                "--env",
                "VONK_LISTEN_PORT=8000",
            ]
            .map(str::to_owned),
        );
        assert!(validate_docker_run(&arguments, &roots, None).is_ok());
        let mut nonzero_owner = arguments.clone();
        let rank = nonzero_owner
            .iter()
            .position(|arg| arg == "VONK_RANK=0")
            .unwrap();
        nonzero_owner[rank] = "VONK_RANK=1".to_owned();
        assert!(validate_docker_run(&nonzero_owner, &roots, None).is_ok());
        let mut worker = arguments.clone();
        let local = worker
            .iter()
            .position(|arg| arg == "VONK_LOCAL_ADDR=192.168.100.10")
            .unwrap();
        worker[local] = "VONK_LOCAL_ADDR=192.168.100.11".to_owned();
        let endpoint = worker
            .iter()
            .position(|arg| arg == "VONK_LISTEN_PORT=8000")
            .unwrap();
        worker.drain(endpoint - 1..=endpoint);
        assert!(validate_docker_run(&worker, &roots, None).is_ok());
        struct FabricRunner;
        impl CommandRunner for FabricRunner {
            fn run(
                &self,
                executable: &Path,
                arguments: &[String],
            ) -> Result<CommandOutput, String> {
                assert_eq!(executable, Path::new(super::DOCKER_FIREWALL));
                assert_eq!(
                    &arguments[2..],
                    [
                        "check-fabric-run",
                        "192.168.100.10",
                        "192.168.100.10",
                        "29500",
                        "8000"
                    ]
                );
                Ok(CommandOutput {
                    success: true,
                    stdout: b"enp1s0f1np1\n".to_vec(),
                    exit_code: Some(0),
                    stderr: Vec::new(),
                })
            }
        }
        let sysfs = temp.path().join("sysfs");
        crate::runtime_fabric::tests::gid(
            &sysfs,
            "rocep1s0f1",
            "3",
            "::ffff:192.168.100.10",
            "RoCE v2",
        );
        let executor = OperationExecutor::new(roots.clone(), &[0; 32], FabricRunner, None).unwrap();
        let mut started = validate_docker_run(&arguments, &roots, None).unwrap();
        executor.bind_native_fabric(&mut started, &sysfs).unwrap();
        let mut inspected = validate_docker_run(&arguments, &roots, None).unwrap();
        executor.bind_native_fabric(&mut inspected, &sysfs).unwrap();
        assert_eq!(started.arguments, inspected.arguments);
        assert!(
            started
                .arguments
                .iter()
                .any(|arg| arg == "NCCL_IB_HCA==rocep1s0f1:1")
        );
        assert!(
            started
                .arguments
                .iter()
                .any(|arg| arg == "NCCL_SOCKET_IFNAME==enp1s0f1np1")
        );
        let compiled = started.docker_arguments().unwrap();
        assert_eq!(compiled[started.image_index], started.local_image_reference);
        assert_eq!(
            compiled
                .iter()
                .filter(|arg| *arg == &started.entrypoint)
                .count(),
            1
        );
        for required in [
            "/dev/infiniband:/dev/infiniband",
            "memlock=-1:-1",
            "stack=67108864:67108864",
            "VONK_WORLD_SIZE=2",
            "VONK_LOCAL_ADDR=192.168.100.10",
        ] {
            let mut incomplete = arguments.clone();
            let index = incomplete
                .iter()
                .position(|value| value == required)
                .unwrap();
            incomplete.drain(index - 1..=index);
            assert!(
                validate_docker_run(&incomplete, &roots, None).is_err(),
                "missing {required}"
            );
        }
        for forbidden in [
            ["--publish", "192.168.1.211:8000:8000"],
            ["--ipc", "host"],
            ["--env", "NCCL_SOCKET_IFNAME=wlP9s9"],
        ] {
            let mut polluted = arguments.clone();
            let image = polluted
                .iter()
                .position(|value| value.starts_with("localhost/vonk/"))
                .unwrap();
            polluted.splice(image..image, forbidden.map(str::to_owned));
            assert!(validate_docker_run(&polluted, &roots, None).is_err());
        }
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
        assert!(
            paths
                .iter()
                .any(|path| path.ends_with("models/primary/artifact-a.bin"))
        );
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
    #[cfg(target_os = "linux")]
    fn runtime_access_skips_exact_model_acl_and_rejects_unexpected_acl() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::write(&model, b"model").unwrap();
        fs::set_permissions(&model, fs::Permissions::from_mode(0o600)).unwrap();
        let mut acl = 2_u32.to_le_bytes().to_vec();
        for (tag, permissions, identifier) in [
            (0x0001_u16, 0o6_u16, u32::MAX),
            (0x0002, 0o4, 10_001),
            (0x0004, 0, u32::MAX),
            (0x0010, 0o4, u32::MAX),
            (0x0020, 0, u32::MAX),
        ] {
            acl.extend_from_slice(&tag.to_le_bytes());
            acl.extend_from_slice(&permissions.to_le_bytes());
            acl.extend_from_slice(&identifier.to_le_bytes());
        }
        xattr::set(&model, "system.posix_acl_access", &acl).unwrap();
        let arguments = runtime_arguments(&roots, &[(model.clone(), "/models", true)]);
        let validated = validate_docker_run(&arguments, &roots, None).unwrap();
        let runner = RecordingAclRunner::default();
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
        executor.prepare_runtime_access(&validated).unwrap();
        assert!(
            !runner
                .calls
                .lock()
                .unwrap()
                .iter()
                .any(|call| call.last() == Some(&model.display().to_string()))
        );

        // An additional named ACL entry cannot be silently transformed into
        // a runtime grant by the helper.
        let mut unexpected = acl[..4].to_vec();
        unexpected.extend_from_slice(&acl[4..20]);
        unexpected.extend_from_slice(&0x0002_u16.to_le_bytes());
        unexpected.extend_from_slice(&0o4_u16.to_le_bytes());
        unexpected.extend_from_slice(&10_002_u32.to_le_bytes());
        unexpected.extend_from_slice(&acl[20..]);
        xattr::set(&model, "system.posix_acl_access", &unexpected).unwrap();
        assert!(executor.prepare_runtime_access(&validated).is_err());
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn repeated_model_and_input_prepare_reuses_kernel_acls() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::write(&model, b"model").unwrap();
        fs::set_permissions(&model, fs::Permissions::from_mode(0o600)).unwrap();
        let inputs = roots.agent_data.join("runs").join(RUN_ID).join("inputs");
        fs::set_permissions(&inputs, fs::Permissions::from_mode(0o700)).unwrap();
        let data = inputs.join("data.bin");
        let manifest = inputs.join("manifest.json");
        let readable = inputs.join("readable.txt");
        for (path, mode) in [(&data, 0o600), (&manifest, 0o400), (&readable, 0o644)] {
            fs::write(path, b"input").unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
        }
        let arguments = job_runtime_arguments(&roots, model.clone());
        let agent_uid = fs::metadata(&model).unwrap().uid();
        let validated = validate_docker_run(&arguments, &roots, Some(agent_uid)).unwrap();
        let runner = KernelAclRunner::default();
        let executor = OperationExecutor::new(
            roots,
            &[0; 32],
            runner.clone(),
            Some(agent_uid.wrapping_add(1)),
        )
        .unwrap()
        .with_runtime_request_owner(agent_uid);
        executor.prepare_runtime_access(&validated).unwrap();
        let first_writes = runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|call| call.first().map(String::as_str) == Some("-R"))
            .count();
        assert_eq!(first_writes, 2);
        let before_second = [&model, &inputs, &data, &manifest, &readable].map(|path| {
            let metadata = fs::metadata(path).unwrap();
            (metadata.ctime(), metadata.ctime_nsec())
        });
        executor.prepare_runtime_access(&validated).unwrap();
        let second_writes = runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|call| call.first().map(String::as_str) == Some("-R"))
            .count();
        assert_eq!(second_writes, first_writes);
        let after_second = [&model, &inputs, &data, &manifest, &readable].map(|path| {
            let metadata = fs::metadata(path).unwrap();
            (metadata.ctime(), metadata.ctime_nsec())
        });
        assert_eq!(after_second, before_second);
        assert_eq!(fs::read(model).unwrap(), b"model");
        for path in [&data, &manifest, &readable] {
            assert_eq!(fs::read(path).unwrap(), b"input");
        }
    }

    #[test]
    fn runtime_image_receipt_keeps_registry_archive_config_and_local_reference_distinct() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        fs::create_dir_all(&roots.data).unwrap();
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
        let (payload, config_id) = docker_save_archive(false);
        let archive_sha256 = hex_sha256(&payload);
        let registry_manifest = format!("sha256:{}", "b".repeat(64));
        let local_reference =
            format!("localhost/vonk/compiled-runtime-{archive_sha256}@{registry_manifest}");
        fs::create_dir_all(roots.agent_data.join("oci-archives")).unwrap();
        let archive = roots.agent_data.join("oci-archives").join(&archive_sha256);
        fs::write(&archive, &payload).unwrap();
        fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
        let archive_identity = executor
            .inspect_runtime_archive(&archive, payload.len() as u64)
            .unwrap();
        executor
            .write_image_receipt(RuntimeImageReceipt {
                schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
                registry_index_digest: format!("sha256:{}", "a".repeat(64)),
                platform_manifest_digest: registry_manifest.clone(),
                archive_sha256: archive_sha256.clone(),
                archive_bytes: payload.len() as u64,
                archive_identity,
                archive_config_id: config_id.clone(),
                image_config_id: config_id.clone(),
                local_image_reference: local_reference.clone(),
            })
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
        assert_ne!(config_id, registry_manifest);
        assert_eq!(receipt.archive_sha256, archive_sha256);
        assert_eq!(receipt.archive_bytes, payload.len() as u64);
        assert_eq!(receipt.platform_manifest_digest, registry_manifest);
        assert_eq!(receipt.image_config_id, config_id);
        assert_eq!(receipt.local_image_reference, local_reference);
    }

    #[test]
    fn runtime_image_receipt_rejects_archive_replacement_by_stable_metadata() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        fs::create_dir_all(&roots.data).unwrap();
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
        let (payload, config_id) = docker_save_archive(false);
        let archive_sha256 = hex_sha256(&payload);
        let registry_manifest = format!("sha256:{}", "b".repeat(64));
        let local_reference =
            format!("localhost/vonk/compiled-runtime-{archive_sha256}@{registry_manifest}");
        let archive_root = roots.agent_data.join("oci-archives");
        fs::create_dir_all(&archive_root).unwrap();
        let archive = archive_root.join(&archive_sha256);
        fs::write(&archive, &payload).unwrap();
        fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
        let archive_identity = executor
            .inspect_runtime_archive(&archive, payload.len() as u64)
            .unwrap();
        executor
            .write_image_receipt(RuntimeImageReceipt {
                schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
                registry_index_digest: format!("sha256:{}", "a".repeat(64)),
                platform_manifest_digest: registry_manifest.clone(),
                archive_sha256: archive_sha256.clone(),
                archive_bytes: payload.len() as u64,
                archive_identity,
                archive_config_id: config_id.clone(),
                image_config_id: config_id.clone(),
                local_image_reference: local_reference.clone(),
            })
            .unwrap();

        let mut replacement = payload;
        replacement[0] ^= 1;
        fs::write(&archive, replacement).unwrap();
        assert!(
            executor
                .require_image_receipt(
                    &archive_sha256,
                    &format!("sha256:{}", "a".repeat(64)),
                    &registry_manifest,
                    &local_reference,
                    &config_id,
                )
                .is_err()
        );
    }

    #[test]
    fn runtime_archive_rejects_final_component_symlinks() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        fs::create_dir_all(&roots.data).unwrap();
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
        let archive_root = roots.agent_data.join("oci-archives");
        fs::create_dir_all(&archive_root).unwrap();
        let target = archive_root.join("target");
        let link = archive_root.join("link");
        fs::write(&target, b"archive").unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap();
        symlink(&target, &link).unwrap();

        assert!(executor.inspect_runtime_archive(&link, 7).is_err());
    }

    #[test]
    fn runtime_image_import_uses_cached_archive_and_writes_bound_receipt() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        fs::create_dir_all(&roots.data).unwrap();
        let (payload, config_id) = docker_save_archive(false);
        let archive_sha256 = hex_sha256(&payload);
        let registry_manifest = format!("sha256:{}", "b".repeat(64));
        let local_reference =
            format!("localhost/vonk/compiled-runtime-{archive_sha256}@{registry_manifest}");
        let archive_root = roots.agent_data.join("oci-archives");
        fs::create_dir_all(&archive_root).unwrap();
        let archive = archive_root.join(&archive_sha256);
        fs::write(&archive, &payload).unwrap();
        fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
        let runner = CountingRuntimeImportRunner::default();
        let loads = runner.loads.clone();
        let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner, None).unwrap();
        let arguments = [
            archive.display().to_string(),
            archive_sha256.clone(),
            payload.len().to_string(),
            format!("sha256:{}", "a".repeat(64)),
            registry_manifest.clone(),
            local_reference.clone(),
        ];
        executor.runtime_image_import(&arguments).unwrap();
        executor.runtime_image_import(&arguments).unwrap();
        assert_eq!(loads.load(std::sync::atomic::Ordering::SeqCst), 1);
        executor
            .require_image_receipt(
                &archive_sha256,
                &format!("sha256:{}", "a".repeat(64)),
                &registry_manifest,
                &local_reference,
                &format!("sha256:{}", "b".repeat(64)),
            )
            .unwrap();
        let receipt: RuntimeImageReceipt = serde_json::from_slice(
            &fs::read(roots.runtime_image_receipts.join(&archive_sha256)).unwrap(),
        )
        .unwrap();
        assert_eq!(receipt.archive_config_id, config_id);
        assert_eq!(
            receipt.image_config_id,
            format!("sha256:{}", "b".repeat(64))
        );
        // A digest-named archive changed in place cannot turn a stale image
        // receipt into another load request.
        fs::write(&archive, vec![0_u8; payload.len()]).unwrap();
        assert!(executor.runtime_image_import(&arguments).is_err());
        assert_eq!(loads.load(std::sync::atomic::Ordering::SeqCst), 1);
    }

    #[test]
    fn runtime_image_import_rehydrates_only_proven_missing_image() {
        use std::sync::atomic::Ordering::SeqCst;
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        fs::create_dir_all(&roots.data).unwrap();
        let (payload, _config_id) = docker_save_archive(false);
        let archive_sha256 = hex_sha256(&payload);
        let registry_manifest = format!("sha256:{}", "b".repeat(64));
        let local_reference =
            format!("localhost/vonk/compiled-runtime-{archive_sha256}@{registry_manifest}");
        let archive_root = roots.agent_data.join("oci-archives");
        fs::create_dir_all(&archive_root).unwrap();
        let archive = archive_root.join(&archive_sha256);
        fs::write(&archive, &payload).unwrap();
        fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
        let runner = CountingRuntimeImportRunner::default();
        let executor = OperationExecutor::new(roots, &[0; 32], runner.clone(), None).unwrap();
        let arguments = [
            archive.display().to_string(),
            archive_sha256,
            payload.len().to_string(),
            format!("sha256:{}", "a".repeat(64)),
            registry_manifest,
            local_reference,
        ];
        executor.runtime_image_import(&arguments).unwrap();
        assert_eq!(runner.loads.load(SeqCst), 1);

        runner.image_missing.store(true, SeqCst);
        executor.runtime_image_import(&arguments).unwrap();
        executor.runtime_image_import(&arguments).unwrap();
        assert_eq!(runner.loads.load(SeqCst), 2);

        runner.wrong_image.store(true, SeqCst);
        assert!(executor.runtime_image_import(&arguments).is_err());
        assert_eq!(runner.loads.load(SeqCst), 2);
        runner.wrong_image.store(false, SeqCst);
        runner.malformed_image.store(true, SeqCst);
        runner.empty_listing.store(true, SeqCst);
        assert!(executor.runtime_image_import(&arguments).is_err());
        assert_eq!(runner.loads.load(SeqCst), 2);
        runner.malformed_image.store(false, SeqCst);
        runner.empty_listing.store(false, SeqCst);
        runner.image_missing.store(true, SeqCst);
        runner.deny_listing.store(true, SeqCst);
        assert!(executor.runtime_image_import(&arguments).is_err());
        assert_eq!(runner.loads.load(SeqCst), 2);
    }

    #[test]
    fn runtime_image_import_rejects_load_without_an_identity() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        fs::create_dir_all(&roots.data).unwrap();
        let (payload, _config_id) = docker_save_archive(false);
        let archive_sha256 = hex_sha256(&payload);
        let registry_manifest = format!("sha256:{}", "b".repeat(64));
        let local_reference =
            format!("localhost/vonk/compiled-runtime-{archive_sha256}@{registry_manifest}");
        let archive_root = roots.agent_data.join("oci-archives");
        fs::create_dir_all(&archive_root).unwrap();
        let archive = archive_root.join(&archive_sha256);
        fs::write(&archive, &payload).unwrap();
        fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
        // The runner reports a valid pre-existing local image if asked; the
        // helper must reject before inspecting it because docker load gave no
        // authoritative identity for the newly imported archive.
        let runner = NoLoadIdentityRunner::default();
        let calls = runner.calls.clone();
        let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner, None).unwrap();
        let error = executor
            .runtime_image_import(&[
                archive.display().to_string(),
                archive_sha256.clone(),
                payload.len().to_string(),
                format!("sha256:{}", "a".repeat(64)),
                registry_manifest,
                local_reference,
            ])
            .unwrap_err();
        assert!(matches!(error, OperationError::RuntimeImageIdentityInvalid));
        assert!(
            calls
                .lock()
                .unwrap()
                .iter()
                .all(|arguments| { arguments.first().map(String::as_str) != Some("image") })
        );
    }

    #[test]
    fn runtime_rejects_noncanonical_or_unsafe_artifact_mounts() {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        let tokenizer = artifact_path(&roots, 'b');
        fs::create_dir_all(&model).unwrap();
        fs::create_dir_all(&tokenizer).unwrap();

        let invalid_single_mounts = [
            (runtime_models(&roots), "/models", true),
            (runtime_models(&roots).join("sha256"), "/models", true),
            (runtime_models(&roots).join("a".repeat(64)), "/models", true),
            (
                roots
                    .agent_data
                    .join("installations")
                    .join("installation-1")
                    .join("models")
                    .join("sha256")
                    .join("..")
                    .join("sha256")
                    .join("a".repeat(64)),
                "/models",
                true,
            ),
            (
                runtime_models(&roots)
                    .join("Primary")
                    .join("artifact-a.bin"),
                "/models",
                true,
            ),
            (model.clone(), "/models", false),
            (model.clone(), "/model", true),
            (model.clone(), "/models/..", true),
            (model.clone(), "/models/model/", true),
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
                let source = runtime_models(&roots)
                    .join("primary")
                    .join(format!("artifact-{index}.bin"));
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
    fn runtime_accepts_installation_model_root_and_rejects_legacy_model_root() {
        let (_temp, roots) = runtime_fixture_with_separate_agent_data();
        let current_model = artifact_path(&roots, 'a');
        fs::create_dir_all(&current_model).unwrap();
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(current_model, "/models", true)]),
                &roots,
                None,
            )
            .is_ok()
        );

        let legacy_model = roots
            .data
            .join("models")
            .join("sha256")
            .join("b".repeat(64));
        fs::create_dir_all(&legacy_model).unwrap();
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(legacy_model, "/models", true)]),
                &roots,
                None,
            )
            .is_err()
        );
    }

    #[test]
    fn runtime_rejects_legacy_sha256_model_layout() {
        let (_temp, roots) = runtime_fixture();
        let legacy_model = runtime_models(&roots).join("sha256").join("a".repeat(64));
        fs::create_dir_all(&legacy_model).unwrap();
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(legacy_model, "/models", true)]),
                &roots,
                None,
            )
            .is_err()
        );
    }

    #[test]
    fn runtime_cache_rejects_other_installations_and_symlinked_ancestors() {
        for case in [
            "other-installation",
            "installation-alias",
            "outside-installation",
        ] {
            let (temp, roots) = runtime_fixture_with_separate_agent_data();
            let model = artifact_path(&roots, 'a');
            fs::create_dir_all(&model).unwrap();
            let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
            validate_docker_run(&arguments, &roots, None).unwrap();

            let installation = runtime_models(&roots).parent().unwrap().to_path_buf();
            let other = roots
                .agent_data
                .join("installations")
                .join("installation-2");
            match case {
                "other-installation" => fs::create_dir_all(other.join("runtime-cache")).unwrap(),
                "installation-alias" => symlink(&installation, &other).unwrap(),
                "outside-installation" => {
                    let outside = temp.path().join("outside-installation");
                    fs::create_dir_all(outside.join("runtime-cache")).unwrap();
                    symlink(&outside, &other).unwrap();
                }
                _ => unreachable!(),
            }
            *arguments
                .iter_mut()
                .find(|value| value.ends_with("dst=/outputs/cache"))
                .unwrap() = format!(
                "type=bind,src={},dst=/outputs/cache",
                other.join("runtime-cache").display()
            );
            assert!(
                matches!(
                    validate_docker_run(&arguments, &roots, None),
                    Err(OperationError::UnsafePath)
                ),
                "cache boundary accepted {case}"
            );
        }
    }

    #[test]
    fn runtime_rejects_symlinked_model_ancestors_and_canonical_escapes() {
        {
            let (temp, roots) = runtime_fixture();
            let models = runtime_models(&roots);
            fs::remove_dir_all(&models).unwrap();
            let outside_models = temp.path().join("outside-models");
            let outside_model = outside_models.join("primary").join("artifact-a.bin");
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
            let model_root = runtime_models(&roots).join("primary");
            fs::remove_dir(&model_root).unwrap();
            let outside_model_root = temp.path().join("outside-primary");
            fs::create_dir_all(outside_model_root.join("artifact-a.bin")).unwrap();
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
            let (_temp, roots) = runtime_fixture_with_separate_agent_data();
            let installation = roots
                .agent_data
                .join("installations")
                .join("installation-1");
            let sibling = roots
                .agent_data
                .join("installations")
                .join("installation-2");
            fs::create_dir_all(sibling.join("models").join("sha256")).unwrap();
            fs::remove_dir_all(&installation).unwrap();
            symlink(&sibling, &installation).unwrap();
            let model = installation
                .join("models")
                .join("primary")
                .join("artifact-a.bin");
            fs::create_dir_all(&model).unwrap();
            assert!(
                validate_docker_run(
                    &runtime_arguments(&roots, &[(model, "/models", true)]),
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
            let model = outside
                .join("installations")
                .join("installation-1")
                .join("models")
                .join("sha256")
                .join("a".repeat(64));
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
    fn runtime_accepts_canonical_nested_model_path_at_512_characters() {
        let (_temp, roots) = runtime_fixture_with_separate_agent_data();
        let segment = format!("模_{}", "a".repeat(61));
        let final_segment = format!("模_{}", "a".repeat(62));
        let mut segments = vec![segment; 7];
        segments.push(final_segment);
        let relative = segments.join("/");
        assert_eq!(relative.chars().count(), MAX_COMPILED_MODEL_PATH_CHARS);
        let source = runtime_models(&roots).join("primary").join(&relative);
        fs::create_dir_all(source.parent().unwrap()).unwrap();
        fs::write(&source, b"model fixture").unwrap();
        let target = "/models/primary";
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(source, target, true)]),
                &roots,
                None,
            )
            .is_ok()
        );
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
            roots.agent_data.join("installations"),
            runtime_models(&roots),
            runtime_models(&roots).join("primary"),
            model,
        ] {
            fs::set_permissions(&path, fs::Permissions::from_mode(0o770)).unwrap();
            assert!(validate_docker_run(&arguments, &roots, Some(owner)).is_err());
            fs::set_permissions(&path, fs::Permissions::from_mode(0o750)).unwrap();
        }
    }

    #[test]
    fn runtime_tmp_reset_rejects_fifo_without_waiting_for_a_writer() {
        use std::os::unix::fs::FileTypeExt;
        use wait_timeout::ChildExt;

        const CHILD_ROOT: &str = "VONK_TMP_RESET_FIFO_TEST_ROOT";
        if let Some(root) = std::env::var_os(CHILD_ROOT) {
            let roots = ManagedRoots::under(Path::new(&root));
            let executor =
                OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
            assert!(matches!(
                executor.reset_runtime_tmp_if_requested(RUN_ID),
                Err(OperationError::UnsafePath)
            ));
            return;
        }

        let (_temp, roots) = runtime_fixture();
        let marker = roots
            .agent_data
            .join("run-metadata")
            .join(RUN_ID)
            .join("tmp-reset-required");
        rustix::fs::mknodat(
            rustix::fs::CWD,
            &marker,
            rustix::fs::FileType::Fifo,
            rustix::fs::Mode::from_raw_mode(0o600),
            0,
        )
        .unwrap();
        let temporary = roots
            .agent_data
            .join("runs")
            .join(RUN_ID)
            .join("outputs/tmp");
        fs::create_dir(&temporary).unwrap();
        fs::write(temporary.join("sentinel"), b"keep").unwrap();
        // Run the real open/fstat boundary in another process so the wrong
        // blocking open fails this test within a deadline rather than hanging
        // the suite indefinitely on a FIFO with no writer.
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "operations::tests::runtime_tmp_reset_rejects_fifo_without_waiting_for_a_writer",
            ])
            .env(CHILD_ROOT, &roots.agent_data)
            .spawn()
            .unwrap();
        let status = child.wait_timeout(Duration::from_secs(5)).unwrap();
        if status.is_none() {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("runtime tmp cleanup blocked on a FIFO marker with no writer");
        }
        assert!(status.unwrap().success());
        assert!(fs::symlink_metadata(marker).unwrap().file_type().is_fifo());
        assert_eq!(fs::read(temporary.join("sentinel")).unwrap(), b"keep");
    }

    #[test]
    fn installation_cleanup_removes_only_private_runtime_cache_and_is_retryable() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(&temp.path().join("agent-data"));
        let installation_id = "10000000-0000-4000-8000-000000000001";
        let installation = roots.agent_data.join("installations").join(installation_id);
        let cache = installation.join("runtime-cache");
        let private = cache.join("home/private/nested");
        let models = installation.join("models/primary");
        let outside = temp.path().join("outside");
        fs::create_dir_all(&private).unwrap();
        fs::create_dir_all(&models).unwrap();
        fs::create_dir_all(&outside).unwrap();
        fs::write(private.join("engine-owned.bin"), b"private").unwrap();
        fs::write(models.join("model.bin"), b"model").unwrap();
        fs::write(outside.join("sentinel"), b"outside").unwrap();
        std::os::unix::fs::symlink(&outside, cache.join("outside-link")).unwrap();
        fs::set_permissions(cache.join("home"), fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(
            cache.join("home/private"),
            fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        fs::set_permissions(&private, fs::Permissions::from_mode(0o700)).unwrap();
        if rustix::process::geteuid().is_root() {
            for path in [cache.join("home"), cache.join("home/private"), private] {
                rustix::fs::chown(
                    path,
                    Some(rustix::process::Uid::from_raw(10001)),
                    Some(rustix::process::Gid::from_raw(10001)),
                )
                .unwrap();
            }
        }
        let executor =
            OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();

        executor
            .runtime_installation_cleanup(installation_id)
            .unwrap();
        executor
            .runtime_installation_cleanup(installation_id)
            .unwrap();

        assert!(!cache.exists());
        assert_eq!(fs::read(models.join("model.bin")).unwrap(), b"model");
        assert_eq!(fs::read(outside.join("sentinel")).unwrap(), b"outside");
    }

    #[test]
    fn installation_cleanup_rejects_symlinked_managed_roots() {
        let temp = tempfile::tempdir().unwrap();
        let outside = temp.path().join("outside");
        let linked = temp.path().join("agent-data");
        let installation_id = "10000000-0000-4000-8000-000000000001";
        let cache = outside
            .join("installations")
            .join(installation_id)
            .join("runtime-cache");
        fs::create_dir_all(&cache).unwrap();
        fs::write(cache.join("sentinel"), b"outside").unwrap();
        std::os::unix::fs::symlink(&outside, &linked).unwrap();
        let roots = ManagedRoots::under(&linked);
        let executor =
            OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();

        assert!(matches!(
            executor.runtime_installation_cleanup(installation_id),
            Err(OperationError::Io(_))
        ));
        assert_eq!(fs::read(cache.join("sentinel")).unwrap(), b"outside");
    }

    #[test]
    fn a_request_document_between_the_retired_private_cap_and_the_exchange_ceiling_is_read() {
        // Wrong implementation: the helper read the request file under a
        // private `MAX_RUNTIME_REQUEST_BYTES = 64 * 1024` round number, so a
        // legitimate many-mount command line the plan admits -- and the agent
        // admitted under the same byte budget -- was refused as
        // `helper.unsafe_path` after a successful install, blaming the path
        // rather than the bound.
        let temp = tempfile::tempdir().unwrap();
        let requests = temp.path().join("runtime-requests");
        fs::create_dir_all(&requests).unwrap();
        let request = HostRuntimeRequest {
            schema_version: 1,
            action: HostRuntimeAction::ImageImport,
            job_id: uuid::Uuid::new_v4(),
            operation_id: uuid::Uuid::new_v4(),
            attempt: 1,
            fence: uuid::Uuid::new_v4(),
            arguments: (0..3000)
                .map(|index| format!("--mount=type=bind,src=/run/vonk/models/{index:05}"))
                .collect(),
            observation: None,
            installation_id: None,
            reconciliation_identity: None,
            job_plan: None,
            run_generation: None,
            start_plan: None,
            stop_plan: None,
        };
        let body = vonk_agent_protocol::canonical_json(&request).unwrap();
        assert!(
            body.len() > 64 * 1024,
            "this document must exceed the retired private cap, got {} bytes",
            body.len()
        );
        assert!(body.len() <= vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES);
        let digest = hex_sha256(&body);
        let path = requests.join(format!("{digest}.json"));
        fs::write(&path, &body).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();

        let executor = OperationExecutor::new(
            ManagedRoots::under(temp.path()).with_runtime_requests(&requests),
            &[0; 32],
            MissingContainerRunner,
            None,
        )
        .unwrap();
        let read = executor
            .read_runtime_request(&digest)
            .expect("a request inside the exchange ceiling must be read");
        assert_eq!(read.arguments.len(), 3000);
    }

    #[test]
    fn installation_cleanup_treats_each_missing_private_cache_level_as_complete() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(&temp.path().join("agent-data"));
        let installation_id = "10000000-0000-4000-8000-000000000001";
        let executor = |roots: &ManagedRoots| {
            OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap()
        };

        fs::create_dir_all(&roots.agent_data).unwrap();
        executor(&roots)
            .runtime_installation_cleanup(installation_id)
            .unwrap();
        fs::create_dir(roots.agent_data.join("installations")).unwrap();
        executor(&roots)
            .runtime_installation_cleanup(installation_id)
            .unwrap();
        fs::create_dir(roots.agent_data.join("installations").join(installation_id)).unwrap();
        executor(&roots)
            .runtime_installation_cleanup(installation_id)
            .unwrap();
    }

    #[test]
    fn reconciliation_clears_only_private_cache_and_replays_after_agent_removal() {
        let (_temp, roots, identity, runtime_cache, shared_cache) = helper_reconciliation_fixture();
        let runner = ReconciliationListingRunner::new(CommandOutput {
            success: true,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: Some(0),
        });
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();

        executor.runtime_reconcile_installation(&identity).unwrap();
        assert!(!runtime_cache.exists());
        assert_eq!(fs::read(&shared_cache).unwrap(), b"shared model cache");
        let receipt_path = roots
            .data
            .join(INSTALLATION_RECONCILIATION_DIRECTORY)
            .join(format!("{}.json", identity.installation_id));
        assert!(receipt_path.is_file());

        let calls = runner.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        let arguments = &calls[0];
        assert!(!arguments.iter().any(|argument| argument == "--filter"));
        let template = arguments.last().unwrap();
        assert!(template.contains(".Label"));
        assert!(template.as_bytes().contains(&b'\t'));
        assert!(!template.contains(r"\t"));
        drop(calls);

        // The agent may finish deleting its exact installation after the
        // helper's acknowledgement is lost. The helper tombstone must retain
        // enough identity to replay that same authorization without the
        // deleted marker or malformed plan.
        let installation = roots
            .agent_data
            .join("installations")
            .join(identity.installation_id.to_string());
        fs::remove_dir_all(&installation).unwrap();
        executor.runtime_reconcile_installation(&identity).unwrap();
        assert!(receipt_path.is_file());
        assert_eq!(fs::read(&shared_cache).unwrap(), b"shared model cache");
    }

    #[test]
    fn reconciliation_refuses_a_replacement_directory_even_with_matching_contract_bytes() {
        let (_temp, roots, identity, _runtime_cache, _shared_cache) =
            helper_reconciliation_fixture();
        let executor = OperationExecutor::new(
            roots.clone(),
            &[0; 32],
            ReconciliationListingRunner::new(CommandOutput {
                success: true,
                stdout: Vec::new(),
                stderr: Vec::new(),
                exit_code: Some(0),
            }),
            None,
        )
        .unwrap();
        executor.runtime_reconcile_installation(&identity).unwrap();

        let installation = roots
            .agent_data
            .join("installations")
            .join(identity.installation_id.to_string());
        let original = roots
            .agent_data
            .join("installations")
            .join(format!("{}.original", identity.installation_id));
        let spec = fs::read(installation.join("spec.json")).unwrap();
        let recipe = fs::read(installation.join("recipe-content.sha256")).unwrap();
        fs::rename(&installation, &original).unwrap();
        fs::create_dir(&installation).unwrap();
        fs::set_permissions(&installation, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(installation.join("spec.json"), spec).unwrap();
        fs::set_permissions(
            installation.join("spec.json"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        fs::write(installation.join("recipe-content.sha256"), recipe).unwrap();
        fs::set_permissions(
            installation.join("recipe-content.sha256"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();

        assert!(matches!(
            executor.runtime_reconcile_installation(&identity),
            Err(OperationError::InvalidArtifact)
        ));
        assert!(installation.is_dir());
        assert!(original.is_dir());
    }

    #[test]
    fn reconciliation_does_not_treat_a_missing_installation_as_prior_cleanup_proof() {
        let (_temp, roots, identity, _runtime_cache, _shared_cache) =
            helper_reconciliation_fixture();
        let installation = roots
            .agent_data
            .join("installations")
            .join(identity.installation_id.to_string());
        fs::remove_dir_all(&installation).unwrap();
        let receipt_path = roots
            .data
            .join(INSTALLATION_RECONCILIATION_DIRECTORY)
            .join(format!("{}.json", identity.installation_id));
        let executor = OperationExecutor::new(
            roots,
            &[0; 32],
            ReconciliationListingRunner::new(CommandOutput {
                success: true,
                stdout: Vec::new(),
                stderr: Vec::new(),
                exit_code: Some(0),
            }),
            None,
        )
        .unwrap();

        assert!(matches!(
            executor.runtime_reconcile_installation(&identity),
            Err(OperationError::UnsafePath)
        ));
        assert!(!receipt_path.exists());
    }

    #[test]
    fn reconciliation_refuses_active_unknown_failed_or_truncated_runtime_inventory() {
        for case in 0..5 {
            let (_temp, roots, identity, runtime_cache, _shared_cache) =
                helper_reconciliation_fixture();
            let stdout = match case {
                0 => format!(
                    "{}\trunning\tvonk-{}\ttrue\t{}\n",
                    "1".repeat(64),
                    RUN_ID,
                    identity.installation_id
                )
                .into_bytes(),
                1 => format!("{}\texited\tvonk-old-{}\ttrue\t\n", "2".repeat(64), RUN_ID)
                    .into_bytes(),
                2 => {
                    format!("{}\texited\tvonk-legacy-{}\t\t\n", "3".repeat(64), RUN_ID).into_bytes()
                }
                3 => Vec::new(),
                _ => vec![b'x'; MAX_COMMAND_OUTPUT_BYTES as usize + 1],
            };
            let response = CommandOutput {
                success: case != 3,
                stdout,
                stderr: if case == 3 {
                    b"docker inventory unavailable".to_vec()
                } else {
                    Vec::new()
                },
                exit_code: Some(if case == 3 { 1 } else { 0 }),
            };
            let receipt_path = roots
                .data
                .join(INSTALLATION_RECONCILIATION_DIRECTORY)
                .join(format!("{}.json", identity.installation_id));
            let executor = OperationExecutor::new(
                roots,
                &[0; 32],
                ReconciliationListingRunner::new(response),
                None,
            )
            .unwrap();

            assert!(executor.runtime_reconcile_installation(&identity).is_err());
            assert!(runtime_cache.exists());
            assert!(!receipt_path.exists());
        }
    }

    #[test]
    fn reconciliation_retires_stopped_unbound_vonk_run_before_publishing_receipt() {
        let (_temp, roots, identity, runtime_cache, _shared_cache) =
            helper_reconciliation_fixture();
        let run_id = "40000000-0000-4000-8000-000000000004";
        let container_id = "a".repeat(64);
        let runner = ReconciliationCleanupRunner::new(CommandOutput {
            success: true,
            stdout: format!("{container_id}\texited\tvonk-{run_id}\ttrue\t\n").into_bytes(),
            stderr: Vec::new(),
            exit_code: Some(0),
        });
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();

        executor.runtime_reconcile_installation(&identity).unwrap();

        assert!(!runtime_cache.exists());
        let calls = runner.calls.lock().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].first().map(String::as_str), Some("container"));
        assert_eq!(calls[0].get(1).map(String::as_str), Some("ls"));
        assert_eq!(
            calls[1],
            vec!["container".to_owned(), "rm".to_owned(), container_id]
        );
    }

    #[test]
    fn reconciliation_lock_blocks_start_owner_and_published_tombstone_blocks_stale_start() {
        let (_temp, roots, identity, runtime_cache, _shared_cache) =
            helper_reconciliation_fixture();
        let empty_listing = || {
            ReconciliationListingRunner::new(CommandOutput {
                success: true,
                stdout: Vec::new(),
                stderr: Vec::new(),
                exit_code: Some(0),
            })
        };
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], empty_listing(), None).unwrap();
        let installation_id = identity.installation_id.to_string();

        // START holds this exact per-installation runtime fence through its
        // Docker call. A cleanup request arriving during that critical section
        // must be deferred without removing the cache or recording a receipt.
        let start_guard = executor
            .lock_installation_runtime(&installation_id)
            .unwrap();
        assert!(matches!(
            executor.runtime_reconcile_installation(&identity),
            Err(OperationError::InstallationReconciliationBusy)
        ));
        assert!(runtime_cache.exists());
        drop(start_guard);

        executor.runtime_reconcile_installation(&identity).unwrap();
        assert!(!runtime_cache.exists());

        // A stale signed START may still arrive after installation cleanup.
        // The exact per-installation tombstone must refuse it before the
        // helper validates paths or attempts to create a runtime.
        let start_identity = RuntimeEffectIdentity {
            runtime_id: uuid::Uuid::parse_str(RUN_ID).unwrap(),
            installation_id: identity.installation_id,
            run_generation: 1,
        };
        assert!(matches!(
            executor.runtime_start_authorized(
                &[],
                start_identity,
                start_identity.runtime_id,
                &"d".repeat(64),
            ),
            Err(OperationError::InvalidArtifact)
        ));
    }

    #[test]
    fn installation_cleanup_rejects_symlinked_installation_path_components() {
        let installation_id = "10000000-0000-4000-8000-000000000001";
        for linked_component in ["installations", "installation", "runtime-cache"] {
            let temp = tempfile::tempdir().unwrap();
            let roots = ManagedRoots::under(&temp.path().join("agent-data"));
            let outside = temp.path().join("outside");
            fs::create_dir_all(&outside).unwrap();
            fs::write(outside.join("sentinel"), b"outside").unwrap();
            fs::create_dir_all(&roots.agent_data).unwrap();

            let installations = roots.agent_data.join("installations");
            if linked_component == "installations" {
                std::os::unix::fs::symlink(&outside, &installations).unwrap();
            } else {
                fs::create_dir(&installations).unwrap();
                let installation = installations.join(installation_id);
                if linked_component == "installation" {
                    std::os::unix::fs::symlink(&outside, &installation).unwrap();
                } else {
                    fs::create_dir(&installation).unwrap();
                    std::os::unix::fs::symlink(&outside, installation.join("runtime-cache"))
                        .unwrap();
                }
            }

            let executor =
                OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
            assert!(matches!(
                executor.runtime_installation_cleanup(installation_id),
                Err(OperationError::Io(_))
            ));
            assert_eq!(fs::read(outside.join("sentinel")).unwrap(), b"outside");
        }
    }
}
