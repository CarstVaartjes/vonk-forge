//! Managed OCI installation and workload lifecycle.

mod custody;
mod errors;
mod installation;
mod jobs;
mod lifecycle;
mod materialization;
mod metadata;
mod observation;
mod reconciliation;
mod storage;

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::IpAddr,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::generated::FailureStage;
use vonk_agent_protocol::generated::{
    InstallationMetadataEntry, InstallationMetadataReceipt, InstallationReconciliationCheckpoint,
    InstallationReconciliationCheckpointState as InstallationReconciliationState,
    RecipeRunObservationCursorWitness as ObservationCursorWitness,
    RecipeRunObservationDirectoryStamp as ObservationDirectoryStamp,
    RunLifecycleRecord as RunLifecycle,
};
use vonk_agent_protocol::{
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES, RecipeReconciliationIdentity,
    canonical_json as canonical_protocol_json,
    compiled_oci::{
        CompiledOciPaths, start_arguments_for_paths as projected_start_arguments_for_paths,
    },
};

pub use vonk_agent_protocol::generated::RecipeRunObservationCheckpoint;

use crate::{
    inventory::available_disk_bytes,
    process::{ProcessError, ProcessRunner},
    workloads::{
        CompiledExecutionPlan, CompiledRuntimePlacement, WorkloadError, managed_path,
        same_installed_workload, same_job_workload,
    },
};

pub struct OciRuntime<'a, R> {
    pub runner: &'a R,
    pub data_root: &'a Path,
}

/// A throughput bound of the current observation wire envelope, not a limit on
/// retained or running workloads. Larger inventories use successive pages.
pub const MAX_RECIPE_RUN_OBSERVATIONS_PER_BATCH: usize = 64;
// One wave of the executor's eight simultaneous physical inspections. A
// sick first wave cannot permanently hide later runs inside a larger page.
const MAX_RUN_INSPECTIONS_PER_PAGE: usize =
    crate::host_runtime::BACKGROUND_RUN_INSPECTION_CONCURRENCY;
// Bound traversal work by a 64 KiB worst-case directory-name wave (Unix
// NAME_MAX plus a terminator per entry), in addition to elapsed scan time.
const RUN_DIRECTORY_NAME_WAVE_BYTES: usize = 64 * 1024;
const MAX_RUN_DIRECTORY_ENTRIES_PER_PAGE: usize = RUN_DIRECTORY_NAME_WAVE_BYTES / 256;
const MAX_RUN_INSPECTION_PAGE_BYTES: usize = 256 * 1024;
const RUN_INSPECTION_PAGE_BUDGET: Duration = Duration::from_millis(250);
pub(crate) const MAX_EMPTY_SCAN_AGE: chrono::TimeDelta = chrono::TimeDelta::minutes(5);

fn stamp_of_metadata(metadata: &fs::Metadata) -> ObservationDirectoryStamp {
    ObservationDirectoryStamp {
        device: format!("{:x}", metadata.dev()),
        inode: format!("{:x}", metadata.ino()),
        modified_seconds: metadata.mtime(),
        modified_nanoseconds: metadata.mtime_nsec(),
        changed_seconds: metadata.ctime(),
        changed_nanoseconds: metadata.ctime_nsec(),
    }
}

fn same_observation_directory(
    first: &ObservationDirectoryStamp,
    second: &ObservationDirectoryStamp,
) -> bool {
    first.device == second.device && first.inode == second.inode
}

pub struct RecipeRunInspectionFailure {
    pub run_id: Option<String>,
    pub error: OciError,
}

pub struct RecipeRunInspectionPage {
    pub plans: Vec<RecipeRunInspectionPlan>,
    pub failures: Vec<RecipeRunInspectionFailure>,
    pub checkpoint: Option<RecipeRunObservationCheckpoint>,
    pub observed_at: chrono::DateTime<chrono::Utc>,
    pub complete: bool,
    pub empty_snapshot_safe: bool,
    /// Local enumeration restarted instead of resuming its retained witness.
    /// This is page-local evidence, not a stored checkpoint or wire field.
    pub scan_restarted: bool,
}

fn observation_directory_stamp(path: &Path) -> Result<Option<ObservationDirectoryStamp>, OciError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {
            Ok(Some(stamp_of_metadata(&metadata)))
        }
        Ok(_) => Err(OciError::Artifact),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

// Only a directory with the captured physical identity can continue this scan.
// Changes to that directory's timestamps invalidate coverage, not its identity.
fn open_observation_directory(
    path: &Path,
    captured: &ObservationDirectoryStamp,
) -> Result<File, OciError> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)?;
    let metadata = file.metadata()?;
    let opened = stamp_of_metadata(&metadata);
    if !metadata.is_dir() || !same_observation_directory(captured, &opened) {
        return Err(OciError::Artifact);
    }
    if opened != *captured {
        return Err(std::io::Error::new(
            std::io::ErrorKind::WouldBlock,
            "run inspection directory changed between capture and open",
        )
        .into());
    }
    Ok(file)
}

const MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES: usize = MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES;

#[derive(Debug, Clone)]
pub struct RecipeRunInspectionPlan {
    pub run_id: uuid::Uuid,
    pub run_generation: u64,
    pub arguments: Vec<String>,
    pub endpoint_address: Option<IpAddr>,
    pub endpoint_port: u16,
    pub health_path: String,
}

type LoadedRunLifecycle = (
    CompiledExecutionPlan,
    String,
    CompiledRuntimePlacement,
    Option<u64>,
);

/// The Controller run generation a service start is launched for; it is
/// persisted with the lifecycle so every later observation names it.
#[derive(Debug, Clone)]
pub struct RecipeRunStartIdentity {
    pub run_generation: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstallationReconciliationProgress {
    pub complete: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobOutputState {
    pub output_path: &'static str,
    pub file_count: usize,
    pub total_bytes: u64,
    pub manifest_sha256: String,
}

const INSTALLATION_METADATA_SCHEMA_VERSION: u8 = 2;
const INSTALLATION_METADATA_FILE: &str = "model-metadata.json";
const INSTALLATION_RECONCILIATION_ROOT: &str = "installation-reconciliation";
const INSTALLATION_RECONCILIATION_SCHEMA_VERSION: u8 = 3;
const RECONCILIATION_LOCK_GRACE: Duration = Duration::from_millis(500);
const MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES: u64 = 64 * 1024;
const MAX_COMPILED_DOCUMENT_BYTES: u64 = MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES as u64;
const TRUSTED_RUNTIME_UID: u32 = 10_001;

type PhysicalArtifactIdentity = (String, String, u64, String, String, String);
type PhysicalMaterialization = (PathBuf, PhysicalArtifactIdentity);

fn install_error(stage: FailureStage, source: OciError) -> OciError {
    OciError::Install {
        stage,
        source: Box::new(source),
    }
}

fn start_stage<T>(
    stage: FailureStage,
    work: impl FnOnce() -> Result<T, OciError>,
) -> Result<T, OciError> {
    work().map_err(|source| OciError::Start {
        stage,
        source: Box::new(source),
    })
}

/// Open descriptors bind the agent's verified model custody across one
/// authorized helper Start call. This token is process-local and never grants
/// access to a different installation or helper operation.
pub struct InstallationAclTransition {
    installation_id: String,
    receipt: InstallationMetadataReceipt,
    files: Vec<(PathBuf, File, fs::Metadata)>,
}

pub struct RuntimeStartPlan {
    pub image_digest: String,
    pub registry_index_digest: String,
    pub platform_manifest_digest: String,
    pub archive_sha256: String,
    pub image_reference: String,
    pub main: Vec<String>,
}

pub struct RuntimeStopPlan {
    pub remove: Vec<String>,
    pub image_digest: Option<String>,
    pub registry_index_digest: Option<String>,
    pub platform_manifest_digest: Option<String>,
    pub archive_sha256: Option<String>,
    pub image_reference: Option<String>,
}

/// Project one validated workload into the exact Podman argument vector used
/// by `start_arguments`.  This remains pure so protocol probes can exercise
/// the same argument construction without touching the host runtime.
pub fn start_arguments_for_paths(
    spec: &CompiledExecutionPlan,
    paths: &CompiledOciPaths,
    run_id: &str,
) -> Result<Vec<String>, OciError> {
    projected_start_arguments_for_paths(spec, paths, run_id).map_err(|_| OciError::Runtime)
}

use materialization::*;
pub(crate) use metadata::exact_runtime_file_acl;
use metadata::*;
use storage::*;

#[cfg(test)]
mod test_support;

pub use errors::OciError;
