//! Operations operation boundaries and shared context.

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

use sha2::{Digest, Sha256};

use thiserror::Error;

use vonk_agent_protocol::generated::{
    CompiledExecutionPlan, ConfirmPackageActivationOperation,
    ExecuteContainerRuntimeRequestOperation, HostHelperProcessLogs, HostHelperResponseStatus,
    InstallVonkDebOperation, InstallationIntentFence, RecipeJobRunRequest, RecipeStartPayload,
    RecipeStopPayload,
};

use vonk_agent_protocol::{
    HostRuntimeAction, HostRuntimeRequest, PackageRollbackAuthority, RecipeReconciliationIdentity,
    canonical_json,
    compiled_oci::{
        CompiledOciError, CompiledOciPaths, ExecInvocationLimits, measure_exec_invocation,
        start_arguments_for_paths,
    },
    hex_sha256,
    integer::Integer,
    parse_strict,
};

use wait_timeout::ChildExt;

use crate::protocol::{ContainerRuntimeAction, HostOperation, artifact_signing_bytes};

const MAX_ARTIFACT_BYTES: u64 = 1024 * 1024 * 1024;

/// A first pull of a large runtime image over the Spark's link.
const RUNTIME_IMAGE_PULL_TIMEOUT: Duration = Duration::from_secs(3 * 60 * 60);

const MAX_COMMAND_OUTPUT_BYTES: u64 = 4096;

// Matches the canonical CompiledEnvironmentEntry UTF-8 byte bound.
const MAX_ENVIRONMENT_VALUE_BYTES: usize = 65536;

/// The byte ceiling on one canonical runtime-request document. Owned by the
/// wire contract, not by this helper: the agent enforces the same ceiling
/// before it writes the request file, so the helper's read cannot be stricter
/// than the request the agent was willing to send. A private `64 * 1024`
/// round number here once refused a legitimate many-mount command line with
/// the opaque `helper.unsafe_path`, after a successful install and after the
/// agent had admitted the same bytes.
const MAX_RUNTIME_REQUEST_BYTES: u64 = vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES as u64;

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

/// The firewall's exit status for a port its policy positively refuses.
const FIREWALL_PORT_REFUSED: i32 = 3;

const DOCKER_FIREWALL_CONFIG: &str = "/etc/vonk-forge-agent/docker-firewall.conf";

const NATIVE_FABRIC_ROOT: &str = "/sys/class/infiniband";

const RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION: u8 = 3;

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
    #[error("current installation intent observation is required")]
    InstallationIntentObservationRequired { nonce: String },
    #[error("package preparation observation is unavailable")]
    PackagePreparationUnavailable,
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
    #[error("runtime job wait failed")]
    RuntimeJobWaitFailed { evidence: Option<Box<JobEvidence>> },
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
        /// How the runtime says it ended (exit code, OOM flag, cause token).
        /// Never empty: see `runtime_logs::exit_summary`.
        exit_summary: String,
    },
    #[error("exact runtime container is absent")]
    RuntimeRunMissing,
    #[error("native fabric is unavailable or ambiguous")]
    RuntimeFabricUnavailable,
    #[error("native fabric firewall rejected the placement")]
    RuntimeFabricFirewallRejected {
        /// Which request argument the firewall refused and why, or why the
        /// check could not run. Names only values from the signed plan.
        reason: String,
    },
    #[error("endpoint firewall rejected the published port")]
    RuntimeEndpointFirewallRejected {
        /// The published host port the firewall refused and the set it
        /// authorises, or why the check could not run.
        reason: String,
    },
    #[error("one-shot runtime could not be stopped safely")]
    StopUncertain,
    #[error("installation runtime reconciliation is busy")]
    InstallationReconciliationBusy,
    #[error("installation runtime storage is temporarily unavailable")]
    InstallationReconciliationStorageUnavailable,
    #[error("host mutation failed")]
    Io(#[from] std::io::Error),
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

pub use vonk_agent_protocol::generated::HostOperationOutcome as OperationOutcome;

use vonk_agent_protocol::generated::{
    HostRuntimeImageReceipt as RuntimeImageReceipt, InstallationReconciliationReceipt,
    RuntimeGenerationFence,
};

struct RuntimeRequestOutcome {
    exit_code: Option<i32>,
    /// A one-shot job's own output and exit account, captured before the
    /// container was removed; present for a job that did not exit cleanly.
    evidence: Option<Box<JobEvidence>>,
}

/// How a one-shot job container ended, with its retained output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobEvidence {
    pub logs: Option<Box<HostHelperProcessLogs>>,
    pub summary: String,
}

/// What one read-only inspection of a managed run found.
#[derive(Debug)]
pub struct RunInspection {
    pub running: bool,
    /// The running container's bounded tail, when the caller asked for it.
    pub logs: Option<Box<HostHelperProcessLogs>>,
    /// Why the requested tail could not be read; never both `logs` and this.
    pub log_error: Option<String>,
}

#[derive(Clone, Copy)]
struct RuntimeRequestGrantBinding<'a> {
    fence: &'a uuid::Uuid,
    installation_intent_nonce: Option<&'a str>,
    installation_intent_ordinal: Option<u64>,
    installation_id: Option<&'a uuid::Uuid>,
    reconciliation_identity: Option<&'a RecipeReconciliationIdentity>,
    start_plan_sha256: Option<&'a str>,
    stop_plan_sha256: Option<&'a str>,
    run_generation: Option<u64>,
    runtime_run_id: Option<&'a uuid::Uuid>,
    runtime_target_id: Option<&'a uuid::Uuid>,
    runtime_installation_id: Option<&'a uuid::Uuid>,
}

enum AuthorizedRuntimeEffect {
    Start {
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: String,
        image_config_id: String,
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

const INSTALLATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION: u8 = 3;

const RUNTIME_GENERATION_FENCE_SCHEMA_VERSION: u8 = 2;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct RuntimeEffectIdentity {
    runtime_id: uuid::Uuid,
    installation_id: uuid::Uuid,
    run_generation: u64,
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
    /// The host port Docker publishes the endpoint on (bridge runs only).
    published_endpoint_port: Option<u16>,
    native_fabric: Option<NativeFabric>,
    /// The `NCCL_IB_HCA` argument as launched, replacing the single-device one
    /// that the container identity is computed over.
    launch_hca: Option<(String, String)>,
    job_timeout_seconds: Option<u16>,
}

struct NativeFabric {
    local: Ipv4Addr,
    master: Ipv4Addr,
    port: u16,
}

type RuntimeImageInspection = (String, String, String, String, String);

mod arguments;
use arguments::*;
mod commands;
mod inspection;
pub(crate) use inspection::process_exited;
use inspection::*;
mod access;
mod authority;
mod dispatch;
mod fabric;
mod generation;
mod images;
mod packages;
mod reconciliation;
mod start;
use access::*;
mod stop;
mod validation;
use validation::*;
mod model_paths;
use model_paths::*;
mod storage;
use storage::*;

#[cfg(test)]
mod test_support;

mod errors;

mod installation_intent;
