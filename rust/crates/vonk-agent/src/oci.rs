use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    net::IpAddr,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::{
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES, RecipeReconciliationIdentity,
    RecipeRunInspectionBinding, canonical_json as canonical_protocol_json,
    hex_sha256 as protocol_sha256,
};

use crate::{
    compiled_oci::{CompiledOciPaths, project},
    health::readiness_endpoint,
    inventory::{available_disk_bytes, available_memory_bytes},
    process::{ProcessError, ProcessRunner, Program},
    workloads::{
        CompiledExecutionPlan, CompiledRuntimePlacement, WorkloadError, managed_path,
        same_installed_workload, same_job_workload,
    },
};

#[derive(Debug, Error)]
pub enum OciError {
    #[error("OCI subprocess failed")]
    Process(#[from] ProcessError),
    #[error("workload policy rejected the request")]
    Workload(#[from] WorkloadError),
    #[error("container runtime rejected the request")]
    Runtime,
    #[error("post-stop hook effect may already have been applied")]
    PostStopHooksStarted,
    #[error("container image digest did not match")]
    ImageDigest,
    #[error("managed artifact content is corrupt")]
    Artifact,
    #[error("managed workload storage failed")]
    Io(#[from] std::io::Error),
    #[error("managed workload metadata is invalid")]
    Json(#[from] serde_json::Error),
    #[error("local disk or memory capacity changed after admission")]
    Capacity,
    #[error("installation reconciliation is waiting for its current local owner")]
    ReconciliationBusy,
    #[error("install {stage} failed: {source}")]
    Install {
        stage: &'static str,
        #[source]
        source: Box<OciError>,
    },
    #[error("start {stage} failed: {source}")]
    Start {
        stage: &'static str,
        #[source]
        source: Box<OciError>,
    },
}

impl OciError {
    pub fn safe_start_context(&self) -> (&'static str, &'static str) {
        let (stage, source) = match self {
            Self::Start { stage, source } => (*stage, source.as_ref()),
            error => ("unknown", error),
        };
        let category = match source {
            Self::Io(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                "storage-permission-denied"
            }
            Self::Io(error) if error.kind() == std::io::ErrorKind::NotFound => "storage-not-found",
            error => error.safe_category(),
        };
        (stage, category)
    }

    pub fn safe_install_context(&self) -> (&'static str, &'static str) {
        match self {
            Self::Install { stage, source } => (*stage, source.safe_category()),
            error => ("unknown", error.safe_category()),
        }
    }

    pub(crate) fn safe_category(&self) -> &'static str {
        match self {
            Self::Process(_) => "process",
            Self::Workload(_) => "workload",
            Self::Runtime => "runtime",
            Self::PostStopHooksStarted => "runtime",
            Self::ImageDigest => "image-digest",
            Self::Artifact => "artifact",
            Self::Io(_) => "storage",
            Self::Json(_) => "metadata",
            Self::Capacity => "capacity",
            Self::ReconciliationBusy => "reconciliation-busy",
            Self::Install { source, .. } | Self::Start { source, .. } => source.safe_category(),
        }
    }
}

pub struct OciRuntime<'a, R> {
    pub runner: &'a R,
    pub data_root: &'a Path,
    pub huggingface_curl_config: Option<&'a Path>,
}

pub const MAX_MANAGED_RECIPE_RUNS: usize = 64;
const MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES: usize = MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES;
const MAX_RUN_DIRECTORY_ENTRIES: usize = 4096;

#[derive(Debug, Clone)]
pub struct RecipeRunInspectionPlan {
    pub binding: RecipeRunInspectionBinding,
    pub arguments: Vec<String>,
    pub endpoint_address: Option<IpAddr>,
    pub endpoint_port: u16,
    pub health_path: String,
}

type LoadedRunLifecycle = (
    CompiledExecutionPlan,
    String,
    CompiledRuntimePlacement,
    Option<RecipeRunInspectionBinding>,
);

#[derive(Debug, Clone)]
pub struct RecipeRunStartIdentity {
    pub mapping_generation: u64,
    pub mapping_id: uuid::Uuid,
    pub recipe_content_sha256: String,
    pub recipe_revision_id: uuid::Uuid,
    pub run_generation: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum InstallationReconciliationState {
    Prepared,
    Removing,
    Complete,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct InstallationReconciliationCheckpoint {
    schema_version: u8,
    state: InstallationReconciliationState,
    identity: RecipeReconciliationIdentity,
    installation_device: u64,
    installation_inode: u64,
    removed_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstallationReconciliationProgress {
    pub complete: bool,
    pub removed_bytes: u64,
    pub cleanup_receipt_sha256: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct JobOutputState {
    pub output_path: &'static str,
    pub file_count: usize,
    pub total_bytes: u64,
    pub manifest_sha256: String,
}

const INSTALLATION_METADATA_SCHEMA_VERSION: u8 = 2;
const INSTALLATION_METADATA_FILE: &str = "model-metadata.json";
const INSTALLATION_RECONCILIATION_ROOT: &str = "installation-reconciliation";
const INSTALLATION_RECONCILIATION_SCHEMA_VERSION: u8 = 2;
const MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES: u64 = 64 * 1024;
const MAX_COMPILED_DOCUMENT_BYTES: u64 = MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES as u64;
const TRUSTED_RUNTIME_UID: u32 = 10_001;

type PhysicalArtifactIdentity = (
    String,
    String,
    u64,
    String,
    String,
    String,
    String,
    String,
    u64,
    String,
);
type PhysicalMaterialization = (PathBuf, PhysicalArtifactIdentity);

fn install_error(stage: &'static str, source: OciError) -> OciError {
    OciError::Install {
        stage,
        source: Box::new(source),
    }
}

fn start_stage<T>(
    stage: &'static str,
    work: impl FnOnce() -> Result<T, OciError>,
) -> Result<T, OciError> {
    work().map_err(|source| OciError::Start {
        stage,
        source: Box::new(source),
    })
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct InstallationMetadataReceipt {
    schema_version: u8,
    entries: Vec<InstallationMetadataEntry>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(deny_unknown_fields)]
struct InstallationMetadataEntry {
    selection_id: String,
    path: String,
    sha256: String,
    size_bytes: u64,
    dev: u64,
    ino: u64,
    mtime_ns: i128,
    ctime_ns: i128,
}

/// Open descriptors bind the agent's verified model custody across one
/// authorized helper Start call. This token is process-local and never grants
/// access to a different installation or helper operation.
pub struct InstallationAclTransition {
    installation_id: String,
    receipt: InstallationMetadataReceipt,
    files: Vec<(PathBuf, File, fs::Metadata)>,
}

#[derive(Debug, Deserialize)]
struct RuntimePolicy {
    runtime_interface: String,
    architecture: String,
    required_image_label: RuntimePolicyLabel,
}

#[derive(Debug, Deserialize)]
struct RuntimePolicyLabel {
    name: String,
    value: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RunLifecycle {
    installation_id: String,
    placement: CompiledRuntimePlacement,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    observation: Option<RecipeRunInspectionBinding>,
}

pub struct RuntimeStartPlan {
    pub image_digest: String,
    pub registry_index_digest: String,
    pub platform_manifest_digest: String,
    pub archive_sha256: String,
    pub image_reference: String,
    pub pre_start: Vec<Vec<String>>,
    pub main: Vec<String>,
}

pub struct RuntimeStopPlan {
    pub remove: Vec<String>,
    pub image_digest: Option<String>,
    pub registry_index_digest: Option<String>,
    pub platform_manifest_digest: Option<String>,
    pub archive_sha256: Option<String>,
    pub image_reference: Option<String>,
    pub post_stop: Vec<Vec<String>>,
}

/// Project one validated workload into the exact Podman argument vector used
/// by `start_arguments`.  This remains pure so protocol probes can exercise
/// the same argument construction without touching the host runtime.
pub fn start_arguments_for_paths(
    spec: &CompiledExecutionPlan,
    paths: &CompiledOciPaths,
    run_id: &str,
) -> Result<Vec<String>, OciError> {
    let invocation = project(spec, paths).map_err(|_| OciError::Runtime)?;
    let mut arguments = invocation.podman_arguments();
    arguments.splice(
        1..1,
        [
            "--name".to_owned(),
            format!("vonk-{run_id}"),
            "--restart".to_owned(),
            "no".to_owned(),
        ],
    );
    Ok(arguments)
}

fn runtime_policy() -> Result<RuntimePolicy, OciError> {
    serde_json::from_str(include_str!(
        "../../../../schemas/global/container-runtime-policy-v1.json"
    ))
    .map_err(OciError::Json)
}

impl<R: ProcessRunner> OciRuntime<'_, R> {
    pub fn job_input_destination(&self, run_id: &str, name: &str) -> Result<PathBuf, OciError> {
        if name.is_empty()
            || name == "manifest.json"
            || name.len() > 128
            || !name.as_bytes()[0].is_ascii_alphanumeric()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        {
            return Err(OciError::Artifact);
        }
        let state = managed_path(self.data_root, "runs", run_id)?;
        fs::create_dir_all(&state)?;
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
        let inputs = state.join("inputs");
        fs::create_dir_all(&inputs)?;
        fs::set_permissions(&inputs, fs::Permissions::from_mode(0o700))?;
        let destination = inputs.join(name);
        if fs::symlink_metadata(&destination).is_ok() {
            return Err(OciError::Artifact);
        }
        Ok(destination)
    }

    pub fn write_job_input_manifest(
        &self,
        run_id: &str,
        names: &[String],
        bytes: &[u8],
        expected_sha256: &str,
    ) -> Result<(), OciError> {
        self.verify_job_inputs(run_id, names)?;
        if bytes.len() > 64 * 1024
            || expected_sha256.len() != 64
            || hex::encode(Sha256::digest(bytes)) != expected_sha256
        {
            return Err(OciError::Artifact);
        }
        let inputs = managed_path(self.data_root, "runs", run_id)?.join("inputs");
        let manifest = inputs.join("manifest.json");
        let mut output = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o400)
            .open(&manifest)?;
        output.write_all(bytes)?;
        output.sync_all()?;
        fs::set_permissions(&manifest, fs::Permissions::from_mode(0o400))?;
        File::open(inputs)?.sync_all()?;
        Ok(())
    }

    pub fn verify_job_inputs(&self, run_id: &str, names: &[String]) -> Result<(), OciError> {
        let state = managed_path(self.data_root, "runs", run_id)?;
        fs::create_dir_all(&state)?;
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
        let inputs = state.join("inputs");
        fs::create_dir_all(&inputs)?;
        fs::set_permissions(&inputs, fs::Permissions::from_mode(0o700))?;
        let metadata = fs::symlink_metadata(&inputs)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let mut observed = fs::read_dir(inputs)?
            .map(|entry| {
                let entry = entry?;
                let file_type = entry.file_type()?;
                if !file_type.is_file() || file_type.is_symlink() {
                    return Err(OciError::Artifact);
                }
                entry
                    .file_name()
                    .into_string()
                    .map_err(|_| OciError::Artifact)
            })
            .collect::<Result<Vec<_>, _>>()?;
        observed.sort();
        if observed != names {
            return Err(OciError::Artifact);
        }
        Ok(())
    }

    pub fn job_output_root(&self, run_id: &str) -> Result<PathBuf, OciError> {
        let outputs = managed_path(self.data_root, "runs", run_id)?.join("outputs");
        let metadata = fs::symlink_metadata(&outputs)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        Ok(outputs)
    }

    pub fn cleanup_job_scope(&self, job_id: &str) -> Result<(), OciError> {
        if !canonical_uuid(job_id) {
            return Err(OciError::Artifact);
        }
        for path in [
            self.data_root.join("runs").join(job_id),
            self.data_root.join("run-metadata").join(job_id),
        ] {
            match fs::symlink_metadata(&path) {
                Ok(metadata) => {
                    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                        return Err(OciError::Artifact);
                    }
                    fs::remove_dir_all(&path)?;
                    if let Some(parent) = path.parent() {
                        File::open(parent)?.sync_all()?;
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
            }
        }
        Ok(())
    }

    pub fn ensure_disk_available(&self, required_bytes: u64) -> Result<(), OciError> {
        let required = required_bytes
            .checked_add(10_000_000_000)
            .ok_or(OciError::Capacity)?;
        if available_disk_bytes(self.data_root).map_err(|_| OciError::Capacity)? < required {
            return Err(OciError::Capacity);
        }
        Ok(())
    }

    pub fn ensure_memory_available(
        &self,
        required_bytes: u64,
        memory_floor_bytes: u64,
        memory_kind: &str,
        meminfo_path: &Path,
    ) -> Result<(), OciError> {
        let required = required_bytes
            .checked_add(memory_floor_bytes)
            .ok_or(OciError::Capacity)?;
        if available_memory_bytes(self.runner, meminfo_path, memory_kind)
            .map_err(|_| OciError::Capacity)?
            < required
        {
            return Err(OciError::Capacity);
        }
        Ok(())
    }

    pub fn install(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
    ) -> Result<(), OciError> {
        let _lock = self.lock_installation_reconciliation(installation_id)?;
        self.refuse_reconciled_installation(installation_id)?;
        self.install_unlocked(spec, installation_id, recipe_content_sha256)
    }

    fn install_unlocked(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
    ) -> Result<(), OciError> {
        if recipe_content_sha256.len() != 64
            || !recipe_content_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(OciError::Artifact);
        }
        self.verify_image(spec)
            .map_err(|error| install_error("image-verification", error))?;
        let installation = managed_path(self.data_root, "installations", installation_id)
            .map_err(|error| install_error("installation-path", OciError::Workload(error)))?;
        fs::create_dir_all(&installation)
            .map_err(OciError::Io)
            .map_err(|error| install_error("installation-directory", error))?;
        fs::set_permissions(&installation, fs::Permissions::from_mode(0o700))
            .map_err(OciError::Io)
            .map_err(|error| install_error("installation-directory", error))?;
        self.ensure_runtime_cache(installation_id)
            .map_err(|error| install_error("runtime-cache", error))?;
        self.materialize_compiled_models(spec, installation_id)
            .map_err(|error| install_error("model-materialization", error))?;
        self.verify_compiled_image_archive(spec)
            .map_err(|error| install_error("image-archive", error))?;
        let encoded_spec = serde_json::to_vec(spec)
            .map_err(OciError::Json)
            .map_err(|error| install_error("installation-metadata", error))?;
        if encoded_spec.len() > MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES {
            return Err(install_error("installation-metadata", OciError::Artifact));
        }
        write_installation_metadata(&installation, spec)
            .map_err(|error| install_error("installation-metadata", error))?;
        atomic_write(&installation, "spec.json", &encoded_spec)
            .map_err(|error| install_error("installation-metadata", error))?;
        atomic_write(
            &installation,
            "recipe-content.sha256",
            recipe_content_sha256.as_bytes(),
        )
        .map_err(|error| install_error("installation-metadata", error))?;
        File::open(&installation)
            .map_err(OciError::Io)
            .and_then(|file| file.sync_all().map_err(OciError::Io))
            .map_err(|error| install_error("installation-metadata", error))?;
        Ok(())
    }

    /// A lost install acknowledgement may be retried after the model has
    /// already consumed the admitted space. Only the exact completed,
    /// receipt-backed installation can bypass another full-copy reservation.
    pub fn install_with_space_check(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
        expected_bytes: u64,
    ) -> Result<(), OciError> {
        let _lock = self.lock_installation_reconciliation(installation_id)?;
        self.refuse_reconciled_installation(installation_id)?;
        if self.reuse_completed_install(spec, installation_id, recipe_content_sha256)? {
            return Ok(());
        }
        self.ensure_disk_available(expected_bytes)?;
        self.install_unlocked(spec, installation_id, recipe_content_sha256)
    }

    /// Write or resume a durable cleanup checkpoint for the exact installed
    /// contract. A receipt is written before the installation directory is
    /// moved or removed, and the checkpoint lives outside that directory.
    pub fn prepare_reconciliation(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<InstallationReconciliationProgress, OciError> {
        identity.validate().map_err(|_| OciError::Artifact)?;
        let installation_id = identity.installation_id.to_string();
        let _lock = self.lock_installation_reconciliation(&installation_id)?;
        let root = self.ensure_installation_reconciliation_root()?;
        let checkpoint_path = reconciliation_checkpoint_path(&root, &installation_id)?;
        let quarantine = reconciliation_quarantine_path(&root, &installation_id)?;
        let installation = managed_path(self.data_root, "installations", &installation_id)?;

        if let Some(checkpoint) = read_reconciliation_checkpoint(&checkpoint_path)? {
            if checkpoint.schema_version != INSTALLATION_RECONCILIATION_SCHEMA_VERSION
                || checkpoint.identity != *identity
            {
                return Err(OciError::Artifact);
            }
            match checkpoint.state {
                InstallationReconciliationState::Complete => {
                    if path_exists_without_following(&installation)?
                        || path_exists_without_following(&quarantine)?
                    {
                        return Err(OciError::Artifact);
                    }
                    return Ok(InstallationReconciliationProgress {
                        complete: true,
                        removed_bytes: checkpoint.removed_bytes,
                        cleanup_receipt_sha256: Some(reconciliation_receipt_sha256(&checkpoint)?),
                    });
                }
                InstallationReconciliationState::Prepared => {
                    let location = match (
                        read_reconciliation_directory_identity(&installation)?,
                        read_reconciliation_directory_identity(&quarantine)?,
                    ) {
                        (Some(identity), None) | (None, Some(identity)) => identity,
                        _ => return Err(OciError::Artifact),
                    };
                    if location
                        != (
                            checkpoint.installation_device,
                            checkpoint.installation_inode,
                        )
                    {
                        return Err(OciError::Artifact);
                    }
                    return Ok(InstallationReconciliationProgress {
                        complete: false,
                        removed_bytes: checkpoint.removed_bytes,
                        cleanup_receipt_sha256: None,
                    });
                }
                InstallationReconciliationState::Removing => {
                    match (
                        read_reconciliation_directory_identity(&installation)?,
                        read_reconciliation_directory_identity(&quarantine)?,
                    ) {
                        (None, None) => {}
                        (None, Some(found))
                            if found
                                == (
                                    checkpoint.installation_device,
                                    checkpoint.installation_inode,
                                ) => {}
                        _ => return Err(OciError::Artifact),
                    }
                    return Ok(InstallationReconciliationProgress {
                        complete: false,
                        removed_bytes: checkpoint.removed_bytes,
                        cleanup_receipt_sha256: None,
                    });
                }
            }
        }

        if path_exists_without_following(&quarantine)? {
            return Err(OciError::Artifact);
        }
        let directory_metadata = fs::symlink_metadata(&installation)?;
        if !trusted_installation_directory(&directory_metadata) {
            return Err(OciError::Artifact);
        }
        let spec_path = installation.join("spec.json");
        let spec_metadata = fs::symlink_metadata(&spec_path)?;
        if !trusted_receipt_metadata(&spec_metadata)
            || spec_metadata.len() > MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES as u64
        {
            return Err(OciError::Artifact);
        }
        let spec_bytes =
            read_regular_file(&spec_path, MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES as u64)?;
        let spec_value: serde_json::Value = serde_json::from_slice(&spec_bytes)?;
        let canonical_spec =
            canonical_protocol_json(&spec_value).map_err(|_| OciError::Artifact)?;
        if protocol_sha256(&canonical_spec) != identity.compiled_spec_canonical_sha256 {
            return Err(OciError::Artifact);
        }
        let recipe_path = installation.join("recipe-content.sha256");
        let recipe_metadata = fs::symlink_metadata(&recipe_path)?;
        if !trusted_receipt_metadata(&recipe_metadata) {
            return Err(OciError::Artifact);
        }
        let recipe_digest = String::from_utf8(read_regular_file(&recipe_path, 64)?)
            .map_err(|_| OciError::Artifact)?;
        let embedded_recipe_digest = spec_value
            .get("identity")
            .and_then(|value| value.get("recipe_revision_sha256"))
            .and_then(serde_json::Value::as_str);
        if recipe_digest != identity.recipe_content_sha256
            || embedded_recipe_digest != Some(identity.recipe_content_sha256.as_str())
        {
            return Err(OciError::Artifact);
        }
        let removed_bytes = reconciliation_directory_bytes(&installation)?;
        let checkpoint = InstallationReconciliationCheckpoint {
            schema_version: INSTALLATION_RECONCILIATION_SCHEMA_VERSION,
            state: InstallationReconciliationState::Prepared,
            identity: identity.clone(),
            installation_device: directory_metadata.dev(),
            installation_inode: directory_metadata.ino(),
            removed_bytes,
        };
        write_reconciliation_checkpoint(&root, &checkpoint_path, &checkpoint)?;
        Ok(InstallationReconciliationProgress {
            complete: false,
            removed_bytes,
            cleanup_receipt_sha256: None,
        })
    }

    /// Finish the exact identity-bound removal. Rename first moves the
    /// installation into a private quarantine path, so a crash during
    /// recursive removal can resume without reopening deleted plan metadata.
    pub fn finalize_reconciliation(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<InstallationReconciliationProgress, OciError> {
        identity.validate().map_err(|_| OciError::Artifact)?;
        let installation_id = identity.installation_id.to_string();
        let _lock = self.lock_installation_reconciliation(&installation_id)?;
        let root = self.ensure_installation_reconciliation_root()?;
        let checkpoint_path = reconciliation_checkpoint_path(&root, &installation_id)?;
        let quarantine = reconciliation_quarantine_path(&root, &installation_id)?;
        let installation = managed_path(self.data_root, "installations", &installation_id)?;
        let mut checkpoint =
            read_reconciliation_checkpoint(&checkpoint_path)?.ok_or(OciError::Artifact)?;
        if checkpoint.schema_version != INSTALLATION_RECONCILIATION_SCHEMA_VERSION
            || checkpoint.identity != *identity
        {
            return Err(OciError::Artifact);
        }
        if checkpoint.state == InstallationReconciliationState::Complete {
            if path_exists_without_following(&installation)?
                || path_exists_without_following(&quarantine)?
            {
                return Err(OciError::Artifact);
            }
            return Ok(InstallationReconciliationProgress {
                complete: true,
                removed_bytes: checkpoint.removed_bytes,
                cleanup_receipt_sha256: Some(reconciliation_receipt_sha256(&checkpoint)?),
            });
        }

        match checkpoint.state {
            InstallationReconciliationState::Prepared => {
                match (
                    read_reconciliation_directory_identity(&installation)?,
                    read_reconciliation_directory_identity(&quarantine)?,
                ) {
                    (Some(found), None) => {
                        if found
                            != (
                                checkpoint.installation_device,
                                checkpoint.installation_inode,
                            )
                        {
                            return Err(OciError::Artifact);
                        }
                        fs::rename(&installation, &quarantine)?;
                        File::open(self.data_root.join("installations"))?.sync_all()?;
                        File::open(&root)?.sync_all()?;
                    }
                    (None, Some(found)) => {
                        if found
                            != (
                                checkpoint.installation_device,
                                checkpoint.installation_inode,
                            )
                        {
                            return Err(OciError::Artifact);
                        }
                    }
                    _ => return Err(OciError::Artifact),
                }
                checkpoint.state = InstallationReconciliationState::Removing;
                write_reconciliation_checkpoint(&root, &checkpoint_path, &checkpoint)?;
            }
            InstallationReconciliationState::Removing => match (
                read_reconciliation_directory_identity(&installation)?,
                read_reconciliation_directory_identity(&quarantine)?,
            ) {
                (None, None) => {}
                (None, Some(found))
                    if found
                        == (
                            checkpoint.installation_device,
                            checkpoint.installation_inode,
                        ) => {}
                _ => return Err(OciError::Artifact),
            },
            InstallationReconciliationState::Complete => unreachable!("handled above"),
        }

        if path_exists_without_following(&quarantine)? {
            fs::remove_dir_all(&quarantine)?;
            File::open(&root)?.sync_all()?;
        }
        if path_exists_without_following(&installation)?
            || path_exists_without_following(&quarantine)?
        {
            return Err(OciError::Artifact);
        }
        checkpoint.state = InstallationReconciliationState::Complete;
        write_reconciliation_checkpoint(&root, &checkpoint_path, &checkpoint)?;
        Ok(InstallationReconciliationProgress {
            complete: true,
            removed_bytes: checkpoint.removed_bytes,
            cleanup_receipt_sha256: Some(reconciliation_receipt_sha256(&checkpoint)?),
        })
    }

    fn ensure_installation_reconciliation_root(&self) -> Result<PathBuf, OciError> {
        let root = self.data_root.join(INSTALLATION_RECONCILIATION_ROOT);
        match fs::symlink_metadata(&root) {
            Ok(metadata) if trusted_private_directory(&metadata) => {}
            Ok(_) => return Err(OciError::Artifact),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                fs::create_dir(&root)?;
                fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
                File::open(self.data_root)?.sync_all()?;
            }
            Err(error) => return Err(error.into()),
        }
        let metadata = fs::symlink_metadata(&root)?;
        if !trusted_private_directory(&metadata) {
            return Err(OciError::Artifact);
        }
        Ok(root)
    }

    fn lock_installation_reconciliation(&self, installation_id: &str) -> Result<File, OciError> {
        if !canonical_uuid(installation_id) {
            return Err(OciError::Artifact);
        }
        let root = self.ensure_installation_reconciliation_root()?;
        let path = root.join(format!("{installation_id}.lock"));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .mode(0o600)
            .open(path)?;
        if !trusted_receipt_metadata(&file.metadata()?) {
            return Err(OciError::Artifact);
        }
        match rustix::fs::flock(&file, rustix::fs::FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => Ok(file),
            Err(error)
                if error == rustix::io::Errno::AGAIN || error == rustix::io::Errno::WOULDBLOCK =>
            {
                Err(OciError::ReconciliationBusy)
            }
            Err(error) => Err(OciError::Io(error.into())),
        }
    }

    fn refuse_reconciled_installation(&self, installation_id: &str) -> Result<(), OciError> {
        let root = self.ensure_installation_reconciliation_root()?;
        let path = reconciliation_checkpoint_path(&root, installation_id)?;
        match fs::symlink_metadata(path) {
            Ok(_) => Err(OciError::Artifact),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }

    fn reuse_completed_install(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
    ) -> Result<bool, OciError> {
        self.verify_image(spec)?;
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let metadata = match fs::symlink_metadata(&installation) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_dir()
            || metadata.file_type().is_symlink()
            || metadata.uid() != rustix::process::geteuid().as_raw()
            || metadata.mode() & 0o777 != 0o700
        {
            return Err(OciError::Artifact);
        }
        let mut incomplete = false;
        for name in [
            "spec.json",
            "recipe-content.sha256",
            INSTALLATION_METADATA_FILE,
        ] {
            match fs::symlink_metadata(installation.join(name)) {
                Ok(metadata) if trusted_receipt_metadata(&metadata) => match name {
                    "spec.json" if self.load_spec(installation_id)? == *spec => {}
                    "recipe-content.sha256"
                        if self.recipe_digest(installation_id)? == recipe_content_sha256 => {}
                    INSTALLATION_METADATA_FILE
                        if read_installation_metadata(&installation)?
                            .is_some_and(|receipt| receipt_matches_plan(&receipt, spec)) => {}
                    _ => return Err(OciError::Artifact),
                },
                Ok(_) => return Err(OciError::Artifact),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => incomplete = true,
                Err(error) => return Err(error.into()),
            }
        }
        if incomplete {
            return Ok(false);
        }
        let cache = installation.join("runtime-cache");
        let cache_metadata = fs::symlink_metadata(&cache)?;
        if !cache_metadata.file_type().is_dir()
            || cache_metadata.file_type().is_symlink()
            || cache_metadata.uid() != rustix::process::geteuid().as_raw()
            || cache_metadata.mode() & 0o777 != 0o700
        {
            return Err(OciError::Artifact);
        }
        self.verify_installation(installation_id)?;
        self.verify_compiled_image_archive(spec)?;
        Ok(true)
    }

    /// Materialize only the model files authorized by a compiled Controller
    /// plan. Distribution objects live under the plan-independent,
    /// content-addressed model object root; artifact-set membership remains in
    /// the typed plan and each selected path is an explicit projection.
    pub fn materialize_compiled_models(
        &self,
        plan: &CompiledExecutionPlan,
        installation_id: &str,
    ) -> Result<Vec<PathBuf>, OciError> {
        materialize_compiled_models(self.data_root, plan, installation_id)
    }

    pub fn verify_image(&self, spec: &CompiledExecutionPlan) -> Result<(), OciError> {
        spec.validate()?;
        let policy = runtime_policy()?;
        if spec.runtime_image.runtime_interface != policy.runtime_interface
            // Compiled plans use the Agent architecture identifier, while the
            // image policy uses the OCI platform identifier. Match their one
            // supported pair explicitly; neither contract accepts aliases.
            || !matches!(
                (spec.runtime_image.architecture.as_str(), policy.architecture.as_str()),
                ("linux-arm64", "linux/arm64")
            )
            || policy.required_image_label.name != "ai.vonkforge.runtime-interface"
            || spec.runtime_image.runtime_interface_label != policy.required_image_label.value
            || spec.runtime.image_digest != spec.runtime_image.image_digest
        {
            return Err(OciError::ImageDigest);
        }
        Ok(())
    }

    fn verify_compiled_image_archive(
        &self,
        plan: &CompiledExecutionPlan,
    ) -> Result<PathBuf, OciError> {
        let archive = self
            .data_root
            .join("oci-archives")
            .join(&plan.runtime_image.oci_layout_sha256);
        let metadata = fs::symlink_metadata(&archive)?;
        if metadata.file_type().is_symlink()
            || !trusted_model_metadata(&metadata, plan.runtime_image.image_bytes)
        {
            return Err(OciError::ImageDigest);
        }
        Ok(archive)
    }

    pub fn start_arguments(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
    ) -> Result<Vec<String>, OciError> {
        spec.validate()?;
        placement.validate_bound()?;
        if placement != &spec.runtime.placement {
            return Err(OciError::Runtime);
        }
        managed_path(self.data_root, "installations", installation_id)?;
        let run_root = managed_path(self.data_root, "runs", run_id)?;
        let outputs = run_root.join("outputs");
        let metadata = self.run_metadata_path(run_id)?;
        let runtime_cache =
            managed_path(self.data_root, "installations", installation_id)?.join("runtime-cache");
        start_arguments_for_paths(
            spec,
            &CompiledOciPaths {
                image_archive: self
                    .data_root
                    .join("oci-archives")
                    .join(&spec.runtime_image.oci_layout_sha256),
                model_root: self
                    .data_root
                    .join("installations")
                    .join(installation_id)
                    .join("models"),
                input_root: spec.job.as_ref().map(|_| run_root.join("inputs")),
                output_root: outputs,
                cache_root: runtime_cache,
                runtime_spec: metadata.join("runtime.json"),
            },
            run_id,
        )
    }

    fn ensure_runtime_cache(&self, installation_id: &str) -> Result<PathBuf, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let cache = installation.join("runtime-cache");
        fs::create_dir_all(&cache)?;
        fs::set_permissions(&cache, fs::Permissions::from_mode(0o700))?;
        let metadata = fs::symlink_metadata(&cache)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        Ok(cache)
    }

    pub fn prepare_start(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.prepare_start_internal(spec, installation_id, run_id, placement, None)
    }

    pub fn prepare_start_with_inspection_identity(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
        identity: &RecipeRunStartIdentity,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.prepare_start_internal(spec, installation_id, run_id, placement, Some(identity))
    }

    fn prepare_start_internal(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
        identity: Option<&RecipeRunStartIdentity>,
    ) -> Result<RuntimeStartPlan, OciError> {
        start_stage("image-verification", || self.verify_image(spec))?;
        let state = start_stage("run-storage", || {
            let state = managed_path(self.data_root, "runs", run_id)?;
            fs::create_dir_all(&state)?;
            fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
            Ok(state)
        })?;
        start_stage("output-storage", || {
            let outputs = state.join("outputs");
            fs::create_dir_all(&outputs)?;
            fs::set_permissions(&outputs, fs::Permissions::from_mode(0o700))?;
            ensure_runtime_tmp(&outputs)
        })?;
        start_stage("runtime-cache", || {
            self.ensure_runtime_cache(installation_id)
        })?;
        start_stage("job-inputs", || {
            if spec.job.is_some() {
                let inputs = state.join("inputs");
                let metadata = fs::symlink_metadata(&inputs)?;
                if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                    return Err(OciError::Artifact);
                }
            }
            Ok(())
        })?;
        let metadata = start_stage("runtime-metadata", || {
            let metadata = self.ensure_run_metadata(run_id)?;
            self.write_runtime_contract(spec, run_id)?;
            // The first authorized helper invocation resets private runtime
            // tmp. Keep the marker outside writable mounts so later hooks and
            // the main process preserve temporary work from earlier hooks.
            atomic_write(&metadata, "tmp-reset-required", b"")?;
            File::open(&metadata)?.sync_all()?;
            Ok(metadata)
        })?;
        let main = start_stage("runtime-projection", || {
            self.start_arguments(spec, installation_id, run_id, placement)
        })?;
        let runtime_image_digest = spec.runtime_image.image_digest.clone();
        let runtime_image_reference = spec.runtime_image.local_image_reference();
        let pre_start = start_stage("start-hooks", || {
            spec.lifecycle
                .pre_start
                .iter()
                .map(|hook| hook_arguments(&main, &runtime_image_reference, hook))
                .collect::<Result<Vec<_>, _>>()
        })?;
        let observation = identity
            .map(|identity| {
                let (local_address, master_address, master_port) = if placement.world_size == 1 {
                    (None, None, None)
                } else {
                    (
                        Some(placement.local_address.ok_or(OciError::Artifact)?),
                        Some(placement.master_address.ok_or(OciError::Artifact)?),
                        Some(placement.master_port.ok_or(OciError::Artifact)?),
                    )
                };
                if identity.mapping_generation == 0
                    || identity.run_generation == 0
                    || identity.recipe_content_sha256 != self.recipe_digest(installation_id)?
                {
                    return Err(OciError::Artifact);
                }
                let registry_index_digest = spec
                    .runtime_image
                    .registry_manifest_digest
                    .clone()
                    .unwrap_or_else(|| spec.runtime_image.platform_manifest_digest.clone());
                let platform_manifest_digest = spec.runtime_image.platform_manifest_digest.clone();
                let mut arguments = vec![
                    spec.runtime_image.oci_layout_sha256.clone(),
                    registry_index_digest,
                    platform_manifest_digest,
                    runtime_image_reference.clone(),
                ];
                arguments.extend(main.clone());
                let binding = RecipeRunInspectionBinding {
                    artifact_set_digest: self.artifact_set_digest(installation_id)?,
                    image_digest: runtime_image_digest[7..].to_owned(),
                    installation_id: uuid::Uuid::parse_str(installation_id)
                        .map_err(|_| OciError::Artifact)?,
                    local_address,
                    master_address,
                    master_port,
                    mapping_generation: identity.mapping_generation,
                    mapping_id: identity.mapping_id,
                    model_identity: spec
                        .artifacts
                        .first()
                        .map(|artifact| {
                            format!(
                                "{}/{}@{}",
                                artifact.model.publisher,
                                artifact.model.slug,
                                artifact.model.content_sha256
                            )
                        })
                        .ok_or(OciError::Artifact)?,
                    port: placement.port.ok_or(OciError::Artifact)?,
                    rank: u32::try_from(placement.rank).map_err(|_| OciError::Artifact)?,
                    recipe_content_sha256: identity.recipe_content_sha256.clone(),
                    recipe_revision_id: identity.recipe_revision_id,
                    role: placement.role.clone(),
                    run_id: uuid::Uuid::parse_str(run_id).map_err(|_| OciError::Artifact)?,
                    run_generation: u32::try_from(identity.run_generation)
                        .map_err(|_| OciError::Artifact)?,
                    runtime_arguments_sha256: protocol_sha256(
                        &canonical_protocol_json(&arguments).map_err(|_| OciError::Artifact)?,
                    ),
                    world_size: u32::try_from(placement.world_size)
                        .map_err(|_| OciError::Artifact)?,
                };
                binding.validate().map_err(|_| OciError::Artifact)?;
                Ok(binding)
            })
            .transpose()
            .map_err(|source| OciError::Start {
                stage: "observation-identity",
                source: Box::new(source),
            })?;
        start_stage("lifecycle-metadata", || {
            atomic_write(
                &metadata,
                "lifecycle.json",
                &serde_json::to_vec(&RunLifecycle {
                    installation_id: installation_id.to_owned(),
                    placement: placement.clone(),
                    observation,
                })?,
            )
        })?;
        Ok(RuntimeStartPlan {
            image_digest: runtime_image_digest,
            registry_index_digest: spec
                .runtime_image
                .registry_manifest_digest
                .clone()
                .unwrap_or_else(|| spec.runtime_image.platform_manifest_digest.clone()),
            platform_manifest_digest: spec.runtime_image.platform_manifest_digest.clone(),
            archive_sha256: spec.runtime_image.oci_layout_sha256.clone(),
            image_reference: spec.runtime_image.local_image_reference(),
            pre_start,
            main,
        })
    }

    /// Reconstruct the exact runtime plan for a previously launched service.
    ///
    /// Collective readiness is deliberately a separate, non-starting phase for
    /// distributed workloads.  It may inspect only the run identity retained
    /// by `prepare_start`; a changed request, installation, specification, or
    /// placement fails closed instead of being allowed to inspect a different
    /// container under the same run id.
    pub fn prepare_retained_start(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.verify_image(spec)?;
        let Some((retained_spec, retained_installation_id, retained_placement, _)) =
            self.load_run_lifecycle(run_id)?
        else {
            return Err(OciError::Runtime);
        };
        if retained_spec != *spec
            || retained_installation_id != installation_id
            || retained_placement != *placement
        {
            return Err(OciError::Runtime);
        }
        // Retained reconstruction is inspection/collective-readiness only.
        // Only the authorized helper may reset runtime-owned temporary files.
        Ok(RuntimeStartPlan {
            image_digest: spec.runtime_image.image_digest.clone(),
            registry_index_digest: spec
                .runtime_image
                .registry_manifest_digest
                .clone()
                .unwrap_or_else(|| spec.runtime_image.platform_manifest_digest.clone()),
            platform_manifest_digest: spec.runtime_image.platform_manifest_digest.clone(),
            archive_sha256: spec.runtime_image.oci_layout_sha256.clone(),
            image_reference: spec.runtime_image.local_image_reference(),
            pre_start: Vec::new(),
            main: self.start_arguments(spec, installation_id, run_id, placement)?,
        })
    }

    /// A fresh claim may observe an earlier exact Start without rewriting its
    /// runtime contract, clearing tmp, or issuing lifecycle hooks again.
    pub fn prepare_retained_start_if_present(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
        identity: Option<&RecipeRunStartIdentity>,
    ) -> Result<Option<RuntimeStartPlan>, OciError> {
        if self.load_run_lifecycle(run_id)?.is_none() {
            return Ok(None);
        }
        let plan = match identity {
            Some(identity) => self.prepare_retained_start_with_inspection_identity(
                spec,
                installation_id,
                run_id,
                placement,
                identity,
            )?,
            None => self.prepare_retained_start(spec, installation_id, run_id, placement)?,
        };
        Ok(Some(plan))
    }

    pub fn prepare_retained_start_with_inspection_identity(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
        identity: &RecipeRunStartIdentity,
    ) -> Result<RuntimeStartPlan, OciError> {
        let plan = self.prepare_retained_start(spec, installation_id, run_id, placement)?;
        let Some((_, _, _, Some(binding))) = self.load_run_lifecycle(run_id)? else {
            return Err(OciError::Runtime);
        };
        if binding.mapping_id != identity.mapping_id
            || binding.mapping_generation != identity.mapping_generation
            || binding.recipe_revision_id != identity.recipe_revision_id
            || binding.recipe_content_sha256 != identity.recipe_content_sha256
            || u64::from(binding.run_generation) != identity.run_generation
        {
            return Err(OciError::Runtime);
        }
        Ok(plan)
    }

    pub fn prepare_job_start(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
        invocation: &CompiledExecutionPlan,
    ) -> Result<RuntimeStartPlan, OciError> {
        invocation.validate()?;
        if !same_job_workload(spec, invocation) {
            return Err(OciError::Runtime);
        }
        self.prepare_start(invocation, installation_id, run_id, placement)
    }

    pub fn prepare_stop(&self, run_id: &str) -> Result<RuntimeStopPlan, OciError> {
        let lifecycle = self.load_run_lifecycle(run_id)?;
        let stop_timeout = lifecycle
            .as_ref()
            .map(|(spec, _, _, _)| spec.lifecycle.stop_timeout_seconds)
            .unwrap_or(30);
        let (
            image_digest,
            registry_index_digest,
            platform_manifest_digest,
            archive_sha256,
            image_reference,
            post_stop,
        ) = match lifecycle {
            Some((spec, installation_id, placement, _)) => {
                let main = self.start_arguments(&spec, &installation_id, run_id, &placement)?;
                (
                    Some(spec.runtime_image.image_digest.clone()),
                    Some(
                        spec.runtime_image
                            .registry_manifest_digest
                            .clone()
                            .unwrap_or_else(|| spec.runtime_image.platform_manifest_digest.clone()),
                    ),
                    Some(spec.runtime_image.platform_manifest_digest.clone()),
                    Some(spec.runtime_image.oci_layout_sha256.clone()),
                    Some(spec.runtime_image.local_image_reference()),
                    spec.lifecycle
                        .post_stop
                        .iter()
                        .map(|hook| {
                            hook_arguments(&main, &spec.runtime_image.local_image_reference(), hook)
                        })
                        .collect::<Result<Vec<_>, _>>()?,
                )
            }
            None => (None, None, None, None, None, Vec::new()),
        };
        Ok(RuntimeStopPlan {
            remove: vec![run_id.to_owned(), stop_timeout.to_string()],
            image_digest,
            registry_index_digest,
            platform_manifest_digest,
            archive_sha256,
            image_reference,
            post_stop,
        })
    }

    pub fn complete_stop(&self, run_id: &str) -> Result<(), OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        let directory = match fs::symlink_metadata(&metadata) {
            Ok(directory) => directory,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error.into()),
        };
        if !directory.file_type().is_dir()
            || directory.file_type().is_symlink()
            || directory.uid() != rustix::process::geteuid().as_raw()
            || directory.mode() & 0o077 != 0
        {
            return Err(OciError::Artifact);
        }
        let marker = metadata.join("post-stop-hooks.started");
        match fs::symlink_metadata(&marker) {
            Ok(entry)
                if !entry.file_type().is_file()
                    || entry.file_type().is_symlink()
                    || entry.uid() != directory.uid()
                    || entry.mode() & 0o777 != 0o600 =>
            {
                return Err(OciError::Artifact);
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        match fs::remove_file(metadata.join("lifecycle.json")) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        match fs::remove_file(marker) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        match File::open(metadata) {
            Ok(directory) => directory.sync_all().map_err(OciError::Io),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }

    /// Persist before the first post-stop hook. A restart must never replay
    /// a hook whose effect may have completed before acknowledgement.
    pub fn begin_post_stop_hooks(&self, run_id: &str) -> Result<(), OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        let directory = fs::symlink_metadata(&metadata)?;
        if !directory.file_type().is_dir()
            || directory.file_type().is_symlink()
            || directory.uid() != rustix::process::geteuid().as_raw()
            || directory.mode() & 0o077 != 0
        {
            return Err(OciError::Artifact);
        }
        let marker = metadata.join("post-stop-hooks.started");
        let mut file = match OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(marker)
        {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                return Err(OciError::PostStopHooksStarted);
            }
            Err(error) => return Err(error.into()),
        };
        file.write_all(b"started")?;
        file.sync_all()?;
        File::open(metadata)?.sync_all()?;
        Ok(())
    }

    pub fn recipe_run_inspection_plans(&self) -> Result<Vec<RecipeRunInspectionPlan>, OciError> {
        let runs = self.data_root.join("runs");
        let metadata = match fs::symlink_metadata(&runs) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let mut run_ids = Vec::new();
        for entry in fs::read_dir(&runs)? {
            if run_ids.len() == MAX_RUN_DIRECTORY_ENTRIES {
                return Err(OciError::Artifact);
            }
            let entry = entry?;
            let file_type = entry.file_type()?;
            let run_id = entry
                .file_name()
                .into_string()
                .map_err(|_| OciError::Artifact)?;
            if !canonical_uuid(&run_id) || !file_type.is_dir() || file_type.is_symlink() {
                return Err(OciError::Artifact);
            }
            run_ids.push(run_id);
        }
        run_ids.sort_unstable();

        let mut plans = Vec::new();
        for run_id in run_ids {
            // Run directories intentionally outlive their lifecycle after a
            // successful stop and older agents retained the same residue. A
            // missing lifecycle is therefore historical, while a present but
            // malformed lifecycle remains an active-assignment integrity error.
            let Some((spec, installation_id, placement, observation)) =
                self.load_run_lifecycle(&run_id)?
            else {
                continue;
            };
            let Some(binding) = observation else {
                continue;
            };
            binding.validate().map_err(|_| OciError::Artifact)?;
            if binding.run_id.to_string() != run_id
                || binding.installation_id.to_string() != installation_id
                || u64::from(binding.rank) != placement.rank
                || binding.role != placement.role
                || u64::from(binding.world_size) != placement.world_size
                || binding.local_address
                    != if placement.world_size == 1 {
                        None
                    } else {
                        placement.local_address
                    }
                || binding.master_address
                    != if placement.world_size == 1 {
                        None
                    } else {
                        placement.master_address
                    }
                || binding.master_port
                    != if placement.world_size == 1 {
                        None
                    } else {
                        placement.master_port
                    }
                || Some(binding.port) != placement.port
                || binding.recipe_content_sha256 != self.recipe_digest(&installation_id)?
                || binding.artifact_set_digest != self.artifact_set_digest(&installation_id)?
                || binding.image_digest != spec.runtime_image.image_digest[7..]
                || binding.model_identity
                    != spec
                        .artifacts
                        .first()
                        .map(|artifact| {
                            format!(
                                "{}/{}@{}",
                                artifact.model.publisher,
                                artifact.model.slug,
                                artifact.model.content_sha256
                            )
                        })
                        .ok_or(OciError::Artifact)?
            {
                return Err(OciError::Artifact);
            }
            let retained =
                self.prepare_retained_start(&spec, &installation_id, &run_id, &placement)?;
            let mut arguments = vec![
                retained.archive_sha256.clone(),
                retained.registry_index_digest.clone(),
                retained.platform_manifest_digest.clone(),
                retained.image_reference.clone(),
            ];
            arguments.extend(retained.main);
            if binding.runtime_arguments_sha256
                != protocol_sha256(
                    &canonical_protocol_json(&arguments).map_err(|_| OciError::Artifact)?,
                )
            {
                return Err(OciError::Artifact);
            }
            if plans.len() == MAX_MANAGED_RECIPE_RUNS {
                return Err(OciError::Artifact);
            }
            let endpoint_owner = binding.local_address == binding.master_address;
            let health_path = spec
                .endpoint
                .as_ref()
                .ok_or(OciError::Artifact)?
                .health_path
                .clone();
            plans.push(RecipeRunInspectionPlan {
                binding,
                arguments,
                endpoint_address: if endpoint_owner {
                    Some(placement.endpoint_address.ok_or(OciError::Artifact)?)
                } else {
                    None
                },
                endpoint_port: placement.port.ok_or(OciError::Artifact)?,
                health_path,
            });
        }
        Ok(plans)
    }

    pub(crate) fn readiness_request(&self, address: IpAddr, port: u16, health_path: &str) -> bool {
        let endpoint = readiness_endpoint(address, port, health_path);
        let output = self.runner.run(
            Program::Curl,
            &[
                "--silent".to_owned(),
                "--show-error".to_owned(),
                "--connect-timeout".to_owned(),
                "2".to_owned(),
                "--max-time".to_owned(),
                "3".to_owned(),
                "--max-filesize".to_owned(),
                (64 * 1024).to_string(),
                "--noproxy".to_owned(),
                "*".to_owned(),
                "--proto".to_owned(),
                "=http".to_owned(),
                "--output".to_owned(),
                "/dev/null".to_owned(),
                "--write-out".to_owned(),
                "%{http_code}".to_owned(),
                endpoint,
            ],
            Duration::from_secs(5),
        );
        let Ok(output) = output else {
            return false;
        };
        output.success
            && std::str::from_utf8(&output.stdout)
                .ok()
                .and_then(|status| status.parse::<u16>().ok())
                .is_some_and(|status| (200..300).contains(&status))
    }

    pub(crate) fn retained_telemetry_plan(
        &self,
        run_id: &str,
    ) -> Result<Option<CompiledExecutionPlan>, OciError> {
        Ok(self.load_run_lifecycle(run_id)?.map(|(plan, _, _, _)| plan))
    }

    fn load_run_lifecycle(&self, run_id: &str) -> Result<Option<LoadedRunLifecycle>, OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        let path = metadata.join("lifecycle.json");
        let Some(record) = self.read_run_lifecycle(&path)? else {
            return Ok(None);
        };
        record.placement.validate_bound()?;
        if !canonical_uuid(&record.installation_id) {
            return Err(OciError::Artifact);
        }
        managed_path(self.data_root, "installations", &record.installation_id)?;
        let spec: CompiledExecutionPlan = serde_json::from_slice(&read_regular_file(
            &metadata.join("runtime.json"),
            MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES as u64,
        )?)?;
        spec.validate()?;
        let installed = self.load_spec(&record.installation_id)?;
        let matches = if spec.job.is_some() {
            same_job_workload(&installed, &spec)
        } else {
            same_installed_workload(&installed, &spec)
        };
        if !matches || spec.runtime.placement != record.placement {
            return Err(OciError::Artifact);
        }
        Ok(Some((
            spec,
            record.installation_id,
            record.placement,
            record.observation,
        )))
    }

    fn read_run_lifecycle(&self, path: &Path) -> Result<Option<RunLifecycle>, OciError> {
        let metadata = match fs::symlink_metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() > 16 * 1024
        {
            return Err(OciError::Artifact);
        }
        let record: RunLifecycle = serde_json::from_slice(&read_regular_file(path, 16 * 1024)?)?;
        Ok(Some(record))
    }

    fn run_metadata_path(&self, run_id: &str) -> Result<PathBuf, OciError> {
        if !canonical_uuid(run_id) {
            return Err(OciError::Artifact);
        }
        Ok(self.data_root.join("run-metadata").join(run_id))
    }

    fn ensure_run_metadata(&self, run_id: &str) -> Result<PathBuf, OciError> {
        let root = self.data_root.join("run-metadata");
        fs::create_dir_all(&root)?;
        let root_metadata = fs::symlink_metadata(&root)?;
        if !root_metadata.file_type().is_dir() || root_metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let metadata = self.run_metadata_path(run_id)?;
        match fs::create_dir(&metadata) {
            Ok(()) => fs::set_permissions(&metadata, fs::Permissions::from_mode(0o700))?,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                let metadata_type = fs::symlink_metadata(&metadata)?.file_type();
                if !metadata_type.is_dir() || metadata_type.is_symlink() {
                    return Err(OciError::Artifact);
                }
            }
            Err(error) => return Err(error.into()),
        }
        Ok(metadata)
    }

    pub fn uninstall(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
    ) -> Result<(), OciError> {
        self.finalize_uninstall(installation_id, expected_recipe_digest)
    }

    pub fn validate_uninstall(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
    ) -> Result<(), OciError> {
        self.load_uninstall_spec(installation_id, expected_recipe_digest)
            .map(|_| ())
    }

    fn load_uninstall_spec(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
    ) -> Result<CompiledExecutionPlan, OciError> {
        let plan = self.read_persisted_spec(installation_id)?;
        plan.validate_storage()?;
        if self.recipe_digest(installation_id)? != expected_recipe_digest
            || plan.identity.recipe_revision_sha256 != expected_recipe_digest
        {
            return Err(OciError::Artifact);
        }
        Ok(plan)
    }

    pub fn runtime_cache_present(&self, installation_id: &str) -> Result<bool, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let cache = installation.join("runtime-cache");
        match fs::symlink_metadata(cache) {
            Ok(metadata) if metadata.file_type().is_dir() && !metadata.file_type().is_symlink() => {
                Ok(true)
            }
            Ok(_) => Err(OciError::Artifact),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error.into()),
        }
    }

    pub fn finalize_uninstall(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
    ) -> Result<(), OciError> {
        self.validate_uninstall(installation_id, expected_recipe_digest)?;
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        fs::remove_dir_all(installation)?;
        File::open(self.data_root.join("installations"))?.sync_all()?;
        Ok(())
    }

    /// Remove only this installation's materialized model files. Other
    /// installations and the shared distribution cache retain their own files
    /// (including hard links to the same content) for future use.
    pub fn uninstall_with_model_cleanup(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
        model_content_sha256: &str,
    ) -> Result<u64, OciError> {
        let removed_model_bytes = self.validate_uninstall_with_model_cleanup(
            installation_id,
            expected_recipe_digest,
            model_content_sha256,
        )?;
        self.uninstall(installation_id, expected_recipe_digest)?;
        Ok(removed_model_bytes)
    }

    pub fn validate_uninstall_with_model_cleanup(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
        model_content_sha256: &str,
    ) -> Result<u64, OciError> {
        if !lower_hex(model_content_sha256, 64) {
            return Err(OciError::Artifact);
        }
        let persisted = self.load_uninstall_spec(installation_id, expected_recipe_digest)?;
        if !spec_references_model(&persisted, model_content_sha256) {
            return Err(OciError::Artifact);
        }
        let removed_model_bytes =
            materialized_model_bytes(self.data_root, installation_id, &persisted)?;
        Ok(removed_model_bytes)
    }

    pub fn load_spec(&self, installation_id: &str) -> Result<CompiledExecutionPlan, OciError> {
        self.load_persisted_spec(installation_id)
            .map(|(spec, _)| spec)
    }

    fn load_persisted_spec(
        &self,
        installation_id: &str,
    ) -> Result<(CompiledExecutionPlan, CompiledExecutionPlan), OciError> {
        let persisted = self.read_persisted_spec(installation_id)?;
        persisted.validate()?;
        Ok((persisted.clone(), persisted))
    }

    fn read_persisted_spec(
        &self,
        installation_id: &str,
    ) -> Result<CompiledExecutionPlan, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let directory_metadata = fs::symlink_metadata(&installation)?;
        if !directory_metadata.file_type().is_dir() || directory_metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let path = installation.join("spec.json");
        let metadata = fs::symlink_metadata(&path)?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() > MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES as u64
        {
            return Err(OciError::Artifact);
        }
        serde_json::from_slice(&read_regular_file(
            &path,
            MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES as u64,
        )?)
        .map_err(OciError::Json)
    }

    pub fn verify_installation(&self, installation_id: &str) -> Result<(), OciError> {
        let (_, plan) = self.load_persisted_spec(installation_id)?;
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let models = installation.join("models");
        let receipt = read_installation_metadata(&installation)?
            .filter(|receipt| receipt_matches_plan(receipt, &plan));
        let receipt_index = receipt.as_ref().map(|receipt| {
            receipt
                .entries
                .iter()
                .map(|entry| ((entry.selection_id.as_str(), entry.path.as_str()), entry))
                .collect::<BTreeMap<_, _>>()
        });
        if receipt.is_some() {
            let mut fast_path = true;
            for artifact in unique_plan_artifacts(&plan) {
                let destination = models.join(&artifact.selection_id).join(&artifact.path);
                let Some(entry) = receipt_index.as_ref().and_then(|index| {
                    index.get(&(artifact.selection_id.as_str(), artifact.path.as_str()))
                }) else {
                    fast_path = false;
                    break;
                };
                let (_, metadata) = open_trusted_model_file(&destination, artifact.size_bytes)?;
                if !metadata_matches_receipt(&metadata, entry) {
                    fast_path = false;
                    break;
                }
            }
            if fast_path {
                return Ok(());
            }
        }

        let unique_artifacts = unique_plan_artifacts(&plan);
        let mut refreshed = Vec::with_capacity(unique_artifacts.len());
        for artifact in unique_artifacts {
            let destination = models.join(&artifact.selection_id).join(&artifact.path);
            let (mut file, metadata) = open_trusted_model_file(&destination, artifact.size_bytes)?;
            if let Some(entry) = receipt_index.as_ref().and_then(|index| {
                index
                    .get(&(artifact.selection_id.as_str(), artifact.path.as_str()))
                    .filter(|entry| metadata_matches_receipt(&metadata, entry))
            }) {
                refreshed.push((**entry).clone());
                continue;
            }
            if sha256_open_file(&mut file, &metadata)? != artifact.sha256 {
                return Err(OciError::Artifact);
            }
            refreshed.push(installation_metadata_entry(artifact, &metadata));
        }
        refreshed.sort();
        atomic_write(
            &installation,
            INSTALLATION_METADATA_FILE,
            &serde_json::to_vec(&InstallationMetadataReceipt {
                schema_version: INSTALLATION_METADATA_SCHEMA_VERSION,
                entries: refreshed,
            })?,
        )?;
        File::open(&installation)?.sync_all()?;
        Ok(())
    }

    pub fn begin_installation_acl_transition(
        &self,
        installation_id: &str,
    ) -> Result<InstallationAclTransition, OciError> {
        let (_, plan) = self.load_persisted_spec(installation_id)?;
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let receipt = read_installation_metadata(&installation)?
            .filter(|receipt| receipt_matches_plan(receipt, &plan))
            .ok_or(OciError::Artifact)?;
        let models = installation.join("models");
        let mut files = Vec::with_capacity(receipt.entries.len());
        for entry in &receipt.entries {
            let path = models.join(&entry.selection_id).join(&entry.path);
            let (file, metadata) = open_trusted_model_file(&path, entry.size_bytes)?;
            if !metadata_matches_receipt(&metadata, entry) {
                return Err(OciError::Artifact);
            }
            files.push((path, file, metadata));
        }
        Ok(InstallationAclTransition {
            installation_id: installation_id.to_owned(),
            receipt,
            files,
        })
    }

    pub fn finish_installation_acl_transition(
        &self,
        installation_id: &str,
        mut transition: InstallationAclTransition,
    ) -> Result<(), OciError> {
        if transition.installation_id != installation_id {
            return Err(OciError::Artifact);
        }
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        if read_installation_metadata(&installation)? != Some(transition.receipt.clone()) {
            return Err(OciError::Artifact);
        }
        let previous_receipt = transition.receipt.clone();
        for (entry, (path, file, before)) in transition
            .receipt
            .entries
            .iter_mut()
            .zip(transition.files.iter())
        {
            let after = file.metadata()?;
            let (reopened, path_after) = open_trusted_model_file(path, entry.size_bytes)?;
            if before.dev() != after.dev()
                || before.ino() != after.ino()
                || before.len() != after.len()
                || before.mtime() != after.mtime()
                || before.mtime_nsec() != after.mtime_nsec()
                || after.dev() != path_after.dev()
                || after.ino() != path_after.ino()
                || after.len() != path_after.len()
                || after.mtime() != path_after.mtime()
                || after.mtime_nsec() != path_after.mtime_nsec()
                || after.ctime() != path_after.ctime()
                || after.ctime_nsec() != path_after.ctime_nsec()
                || !trusted_model_file(file, &after, entry.size_bytes)
                || !trusted_model_file(&reopened, &path_after, entry.size_bytes)
            {
                return Err(OciError::Artifact);
            }
            entry.ctime_ns = timestamp_ns(after.ctime(), after.ctime_nsec());
        }
        if transition.receipt == previous_receipt {
            return Ok(());
        }
        atomic_write(
            &installation,
            INSTALLATION_METADATA_FILE,
            &serde_json::to_vec(&transition.receipt)?,
        )?;
        File::open(&installation)?.sync_all()?;
        Ok(())
    }

    pub fn recipe_digest(&self, installation_id: &str) -> Result<String, OciError> {
        let path = managed_path(self.data_root, "installations", installation_id)?
            .join("recipe-content.sha256");
        let value =
            String::from_utf8(read_regular_file(&path, 64)?).map_err(|_| OciError::Artifact)?;
        if value.len() != 64
            || !value
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(OciError::Artifact);
        }
        Ok(value)
    }

    pub fn recipe_digest_if_present(
        &self,
        installation_id: &str,
    ) -> Result<Option<String>, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let metadata = match fs::symlink_metadata(&installation) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        self.recipe_digest(installation_id).map(Some)
    }

    pub fn installed_bytes(&self, installation_id: &str) -> Result<u64, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let mut files = BTreeMap::new();
        let mut total = 0;
        visit_files(&installation, &installation, &mut files, &mut total)?;
        Ok(total)
    }

    pub fn artifact_set_digest(&self, installation_id: &str) -> Result<String, OciError> {
        let (_, persisted) = self.load_persisted_spec(installation_id)?;
        Ok(persisted.identity.model_artifact_set_sha256)
    }

    fn write_runtime_contract(
        &self,
        spec: &CompiledExecutionPlan,
        run_id: &str,
    ) -> Result<(), OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        atomic_write(&metadata, "runtime.json", &serde_json::to_vec(spec)?)?;
        Ok(())
    }

    pub fn job_output_state(&self, run_id: &str) -> Result<JobOutputState, OciError> {
        let outputs = managed_path(self.data_root, "runs", run_id)?.join("outputs");
        let metadata = fs::symlink_metadata(&outputs)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let mut files = BTreeMap::new();
        let mut total_bytes = 0;
        visit_files(&outputs, &outputs, &mut files, &mut total_bytes)?;
        let manifest_sha256 = hex::encode(Sha256::digest(serde_json::to_vec(&files)?));
        Ok(JobOutputState {
            output_path: "/outputs",
            file_count: files.len(),
            total_bytes,
            manifest_sha256,
        })
    }
}

fn sync_parent(parent: &Path) -> Result<(), OciError> {
    File::open(parent)?.sync_all()?;
    Ok(())
}

/// Release the kernel's page cache for a file this agent has finished with.
///
/// A materialized model is hundreds of gigabytes.  Where the device's memory is
/// the system's unified memory, the page cache holding those bytes is memory the
/// GPU cannot allocate from: after one 199 GB installation the workload's own
/// loader read about 950 MB of free device memory and refused the 1.27 GB
/// staging buffer its checkpoint needs, on a node that reported 126 GB
/// available.  The agent wrote those bytes, so releasing them is the agent's
/// job, and it is a hint: the file's content is unaffected and the next read
/// simply caches again.
fn release_page_cache(path: &Path) -> Result<(), OciError> {
    if !path.is_file() {
        return Err(OciError::Artifact);
    }
    let file = File::open(path)?;
    rustix::fs::fadvise(&file, 0, None, rustix::fs::Advice::DontNeed)
        .map_err(std::io::Error::from)?;
    Ok(())
}

struct TemporaryArtifact {
    path: PathBuf,
    retained: bool,
}

impl TemporaryArtifact {
    fn new(path: PathBuf) -> Self {
        Self {
            path,
            retained: false,
        }
    }

    fn retain(&mut self) {
        self.retained = true;
    }
}

impl Drop for TemporaryArtifact {
    fn drop(&mut self) {
        if !self.retained {
            let _ = fs::remove_file(&self.path);
        }
    }
}

fn sha256_open_file(file: &mut File, before: &fs::Metadata) -> Result<String, OciError> {
    #[cfg(test)]
    SHA256_OPEN_FILE_CALLS.with(|calls| calls.set(calls.get() + 1));
    file.seek(SeekFrom::Start(0))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    let after = file.metadata()?;
    if !trusted_model_file(file, &after, before.len()) || !metadata_stable(before, &after) {
        return Err(OciError::Artifact);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn metadata_stable(before: &fs::Metadata, after: &fs::Metadata) -> bool {
    before.dev() == after.dev()
        && before.ino() == after.ino()
        && before.len() == after.len()
        && timestamp_ns(before.mtime(), before.mtime_nsec())
            == timestamp_ns(after.mtime(), after.mtime_nsec())
        && timestamp_ns(before.ctime(), before.ctime_nsec())
            == timestamp_ns(after.ctime(), after.ctime_nsec())
}

#[cfg(test)]
thread_local! {
    static SHA256_OPEN_FILE_CALLS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

#[cfg(test)]
pub(crate) fn test_sha256_open_file_call_count() -> usize {
    SHA256_OPEN_FILE_CALLS.with(|calls| calls.get())
}

fn write_installation_metadata(
    installation: &Path,
    plan: &CompiledExecutionPlan,
) -> Result<(), OciError> {
    let models = installation.join("models");
    let unique_artifacts = unique_plan_artifacts(plan);
    let mut entries = Vec::with_capacity(unique_artifacts.len());
    for artifact in unique_artifacts {
        let path = models.join(&artifact.selection_id).join(&artifact.path);
        let metadata = fs::symlink_metadata(&path)?;
        if !trusted_model_metadata(&metadata, artifact.size_bytes) {
            return Err(OciError::Artifact);
        }
        entries.push(installation_metadata_entry(artifact, &metadata));
    }
    entries.sort();
    atomic_write(
        installation,
        INSTALLATION_METADATA_FILE,
        &serde_json::to_vec(&InstallationMetadataReceipt {
            schema_version: INSTALLATION_METADATA_SCHEMA_VERSION,
            entries,
        })?,
    )?;
    Ok(())
}

fn read_installation_metadata(
    installation: &Path,
) -> Result<Option<InstallationMetadataReceipt>, OciError> {
    let path = installation.join(INSTALLATION_METADATA_FILE);
    let metadata = match fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    if !trusted_receipt_metadata(&metadata) || metadata.len() > MAX_COMPILED_DOCUMENT_BYTES {
        return Ok(None);
    }
    let value = match read_regular_file(&path, MAX_COMPILED_DOCUMENT_BYTES) {
        Ok(value) => value,
        Err(OciError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(None);
        }
        Err(_) => return Ok(None),
    };
    let receipt = match serde_json::from_slice(&value) {
        Ok(receipt) => receipt,
        Err(_) => return Ok(None),
    };
    Ok(Some(receipt))
}

fn receipt_matches_plan(
    receipt: &InstallationMetadataReceipt,
    plan: &CompiledExecutionPlan,
) -> bool {
    let unique_artifacts = unique_plan_artifacts(plan);
    if receipt.schema_version != INSTALLATION_METADATA_SCHEMA_VERSION
        || receipt.entries.len() != unique_artifacts.len()
    {
        return false;
    }
    let mut observed = BTreeMap::new();
    for entry in &receipt.entries {
        if observed
            .insert(
                (entry.selection_id.as_str(), entry.path.as_str()),
                (&entry.sha256, entry.size_bytes),
            )
            .is_some()
        {
            return false;
        }
    }
    unique_artifacts.iter().all(|artifact| {
        observed.get(&(artifact.selection_id.as_str(), artifact.path.as_str()))
            == Some(&(&artifact.sha256, artifact.size_bytes))
    })
}

fn unique_plan_artifacts(
    plan: &CompiledExecutionPlan,
) -> Vec<&crate::workloads::CompiledModelArtifact> {
    let mut seen = BTreeSet::new();
    plan.artifacts
        .iter()
        .filter(|artifact| seen.insert((artifact.selection_id.as_str(), artifact.path.as_str())))
        .collect()
}

fn installation_metadata_entry(
    artifact: &crate::workloads::CompiledModelArtifact,
    metadata: &fs::Metadata,
) -> InstallationMetadataEntry {
    InstallationMetadataEntry {
        selection_id: artifact.selection_id.clone(),
        path: artifact.path.clone(),
        sha256: artifact.sha256.clone(),
        size_bytes: artifact.size_bytes,
        dev: metadata.dev(),
        ino: metadata.ino(),
        mtime_ns: timestamp_ns(metadata.mtime(), metadata.mtime_nsec()),
        ctime_ns: timestamp_ns(metadata.ctime(), metadata.ctime_nsec()),
    }
}

fn metadata_matches_receipt(metadata: &fs::Metadata, receipt: &InstallationMetadataEntry) -> bool {
    metadata.dev() == receipt.dev
        && metadata.ino() == receipt.ino
        && metadata.len() == receipt.size_bytes
        && timestamp_ns(metadata.mtime(), metadata.mtime_nsec()) == receipt.mtime_ns
        && timestamp_ns(metadata.ctime(), metadata.ctime_nsec()) == receipt.ctime_ns
}

fn trusted_model_metadata(metadata: &fs::Metadata, expected_bytes: u64) -> bool {
    trusted_model_shape(metadata, expected_bytes) && metadata.mode() & 0o777 == 0o600
}

fn trusted_model_shape(metadata: &fs::Metadata, expected_bytes: u64) -> bool {
    metadata.file_type().is_file()
        && !metadata.file_type().is_symlink()
        && metadata.nlink() == 1
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && metadata.len() == expected_bytes
}

fn trusted_model_file(file: &File, metadata: &fs::Metadata, expected_bytes: u64) -> bool {
    if !trusted_model_shape(metadata, expected_bytes) {
        return false;
    }
    match metadata.mode() & 0o777 {
        0o600 => true,
        0o640 => exact_runtime_file_acl(file),
        _ => false,
    }
}

fn exact_runtime_file_acl(file: &File) -> bool {
    const ACL_VERSION: u32 = 0x0002;
    const USER_OBJ: u16 = 0x0001;
    const USER: u16 = 0x0002;
    const GROUP_OBJ: u16 = 0x0004;
    const MASK: u16 = 0x0010;
    const OTHER: u16 = 0x0020;
    let mut value = [0_u8; 4 + 5 * 8];
    let Ok(length) = rustix::fs::fgetxattr(file, "system.posix_acl_access", &mut value) else {
        return false;
    };
    if length != value.len() || u32::from_le_bytes(value[..4].try_into().unwrap()) != ACL_VERSION {
        return false;
    }
    let mut user_object = false;
    let mut runtime_user = false;
    let mut group_object = false;
    let mut mask = false;
    let mut other = false;
    for entry in value[4..].as_chunks::<8>().0.iter() {
        let [tag, permissions, low, high] = entry.as_chunks::<2>().0 else {
            unreachable!("an eight-byte entry yields four two-byte fields")
        };
        let tag = u16::from_le_bytes(*tag);
        let permissions = u16::from_le_bytes(*permissions);
        let identifier = u32::from_le_bytes([low[0], low[1], high[0], high[1]]);
        match tag {
            USER_OBJ if identifier == u32::MAX => user_object = permissions == 0o6,
            USER if identifier == TRUSTED_RUNTIME_UID => runtime_user = permissions == 0o4,
            GROUP_OBJ if identifier == u32::MAX => group_object = permissions == 0,
            MASK if identifier == u32::MAX => mask = permissions == 0o4,
            OTHER if identifier == u32::MAX => other = permissions == 0,
            _ => return false,
        }
    }
    user_object && runtime_user && group_object && mask && other
}

fn open_trusted_model_file(
    path: &Path,
    expected_bytes: u64,
) -> Result<(File, fs::Metadata), OciError> {
    let path_metadata = fs::symlink_metadata(path)?;
    if !trusted_model_shape(&path_metadata, expected_bytes) {
        return Err(OciError::Artifact);
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(path)?;
    let opened_metadata = file.metadata()?;
    if !trusted_model_file(&file, &opened_metadata, expected_bytes)
        || opened_metadata.dev() != path_metadata.dev()
        || opened_metadata.ino() != path_metadata.ino()
    {
        return Err(OciError::Artifact);
    }
    Ok((file, opened_metadata))
}

fn trusted_receipt_metadata(metadata: &fs::Metadata) -> bool {
    metadata.file_type().is_file()
        && !metadata.file_type().is_symlink()
        && metadata.nlink() == 1
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && metadata.mode() & 0o777 == 0o600
}

fn timestamp_ns(seconds: i64, nanoseconds: i64) -> i128 {
    i128::from(seconds)
        .saturating_mul(1_000_000_000)
        .saturating_add(i128::from(nanoseconds))
}

fn hook_arguments(main: &[String], image: &str, hook: &[String]) -> Result<Vec<String>, OciError> {
    if hook.is_empty() || main.first().map(String::as_str) != Some("run") {
        return Err(OciError::Runtime);
    }
    let image_index = main
        .iter()
        .position(|value| value == image)
        .ok_or(OciError::Runtime)?;
    let mut arguments = vec!["run".to_owned(), "--rm".to_owned()];
    let mut index = 1;
    while index < image_index {
        match main[index].as_str() {
            "--detach" => index += 1,
            "--name" | "--restart" | "--publish" => index += 2,
            _ => {
                arguments.push(main[index].clone());
                index += 1;
            }
        }
    }
    arguments.push(image.to_owned());
    arguments.extend(hook.iter().cloned());
    Ok(arguments)
}

fn materialize_compiled_models(
    data_root: &Path,
    plan: &CompiledExecutionPlan,
    installation_id: &str,
) -> Result<Vec<PathBuf>, OciError> {
    if !data_root.is_absolute() {
        return Err(OciError::Artifact);
    }
    plan.validate()?;
    let installation = managed_path(data_root, "installations", installation_id)?;
    let destination_root = installation.join("models");
    fs::create_dir_all(&installation)?;
    fs::set_permissions(&installation, fs::Permissions::from_mode(0o700))?;
    fs::create_dir_all(&destination_root)?;
    fs::set_permissions(&destination_root, fs::Permissions::from_mode(0o700))?;

    let model_root = data_root.join("distribution").join("models");
    let model_metadata = fs::symlink_metadata(&model_root)?;
    if model_metadata.file_type().is_symlink() || !model_metadata.is_dir() {
        return Err(OciError::Artifact);
    }
    let receipt_index = read_installation_metadata(&installation)?
        .filter(|receipt| receipt_matches_plan(receipt, plan))
        .map(|receipt| {
            receipt
                .entries
                .into_iter()
                .map(|entry| ((entry.selection_id.clone(), entry.path.clone()), entry))
                .collect::<BTreeMap<_, _>>()
        });
    let mut materialized = Vec::with_capacity(plan.artifacts.len());
    let mut physical_by_path: BTreeMap<(String, String), PhysicalMaterialization> = BTreeMap::new();
    for artifact in &plan.artifacts {
        let physical_key = (artifact.selection_id.clone(), artifact.path.clone());
        let destination = destination_root
            .join(&artifact.selection_id)
            .join(&artifact.path);
        let physical = (
            artifact.file_id.clone(),
            artifact.sha256.clone(),
            artifact.size_bytes,
            artifact.model.publisher.clone(),
            artifact.model.slug.clone(),
            artifact.model.content_sha256.clone(),
            artifact.distribution_object.name.clone(),
            artifact.distribution_object.sha256.clone(),
            artifact.distribution_object.bytes,
            artifact.distribution_object.kind.as_str().to_owned(),
        );
        if let Some((_, previous)) = physical_by_path.get(&physical_key) {
            if previous != &physical {
                return Err(OciError::Workload(WorkloadError::Invalid(
                    "compiled model artifact physical identity",
                )));
            }
            // The workload validator proved this is the same receipt-bound
            // physical object. Its first projection performed the only source
            // and destination hash verification; this projection only adds a
            // second OCI mount intent.
            continue;
        }
        if !destination.starts_with(&destination_root) {
            return Err(OciError::Artifact);
        }
        let parent = destination.parent().ok_or(OciError::Artifact)?;
        fs::create_dir_all(parent)?;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
        if let Ok(metadata) = fs::symlink_metadata(&destination) {
            if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
                return Err(OciError::Artifact);
            }
            let reusable = receipt_index.as_ref().and_then(|index| {
                index
                    .get(&(artifact.selection_id.clone(), artifact.path.clone()))
                    .filter(|entry| metadata_matches_receipt(&metadata, entry))
            });
            if let Some(entry) = reusable {
                let (_, opened_metadata) =
                    open_trusted_model_file(&destination, artifact.size_bytes)?;
                if metadata_matches_receipt(&opened_metadata, entry) {
                    physical_by_path.insert(physical_key, (destination.clone(), physical));
                    materialized.push(destination);
                    continue;
                }
            }
        }
        let source = model_root.join(&artifact.sha256);
        if !source.starts_with(&model_root) {
            return Err(OciError::Artifact);
        }
        let (mut source_file, source_metadata) =
            open_trusted_model_file(&source, artifact.size_bytes)?;
        let temporary = destination.with_extension(format!(
            "{}.{}.{}.partial",
            std::process::id(),
            uuid::Uuid::new_v4(),
            artifact.file_id
        ));
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32,
            )
            .open(&temporary)?;
        let mut temporary_guard = TemporaryArtifact::new(temporary.clone());
        // The source is an immutable managed distribution object. Hash the
        // bytes in the same pass that writes the private installation; the
        // retained source handle and stable metadata bind that read to the
        // exact object opened above. A failed digest never publishes a model.
        let mut hasher = Sha256::new();
        let mut copied = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let read = source_file.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            output.write_all(&buffer[..read])?;
            hasher.update(&buffer[..read]);
            copied = copied.checked_add(read as u64).ok_or(OciError::Artifact)?;
            if copied > artifact.size_bytes {
                return Err(OciError::Artifact);
            }
        }
        output.sync_all()?;
        let source_after = source_file.metadata()?;
        let output_metadata = output.metadata()?;
        if copied != artifact.size_bytes
            || hex::encode(hasher.finalize()) != artifact.sha256
            || !trusted_model_file(&source_file, &source_after, artifact.size_bytes)
            || !metadata_stable(&source_metadata, &source_after)
            || !trusted_model_metadata(&output_metadata, artifact.size_bytes)
        {
            drop(output);
            let _ = fs::remove_file(&temporary);
            return Err(OciError::Artifact);
        }
        drop(output);
        fs::rename(&temporary, &destination)?;
        temporary_guard.retain();
        sync_parent(parent)?;
        // The copy just filled the page cache with the destination, and the
        // read that verified it filled the same cache with the source object.
        // Neither is needed once this artifact is complete, and the workload
        // this installation exists for needs the memory more.
        release_page_cache(&destination)?;
        release_page_cache(&source)?;
        physical_by_path.insert(physical_key, (destination.clone(), physical));
        materialized.push(destination);
    }
    File::open(&destination_root)?.sync_all()?;
    Ok(materialized)
}

fn spec_references_model(spec: &CompiledExecutionPlan, model_content_sha256: &str) -> bool {
    spec.artifacts
        .iter()
        .any(|artifact| artifact.model.content_sha256 == model_content_sha256)
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn trusted_private_directory(metadata: &fs::Metadata) -> bool {
    metadata.file_type().is_dir()
        && !metadata.file_type().is_symlink()
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && metadata.mode() & 0o777 == 0o700
}

fn trusted_installation_directory(metadata: &fs::Metadata) -> bool {
    trusted_private_directory(metadata)
}

fn reconciliation_checkpoint_path(root: &Path, installation_id: &str) -> Result<PathBuf, OciError> {
    if !canonical_uuid(installation_id) {
        return Err(OciError::Artifact);
    }
    Ok(root.join(format!("{installation_id}.json")))
}

fn reconciliation_quarantine_path(root: &Path, installation_id: &str) -> Result<PathBuf, OciError> {
    if !canonical_uuid(installation_id) {
        return Err(OciError::Artifact);
    }
    Ok(root.join(format!("{installation_id}.partial")))
}

fn path_exists_without_following(path: &Path) -> Result<bool, OciError> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
}

fn read_reconciliation_directory_identity(path: &Path) -> Result<Option<(u64, u64)>, OciError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if trusted_installation_directory(&metadata) => {
            Ok(Some((metadata.dev(), metadata.ino())))
        }
        Ok(_) => Err(OciError::Artifact),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

fn read_reconciliation_checkpoint(
    path: &Path,
) -> Result<Option<InstallationReconciliationCheckpoint>, OciError> {
    let file = match OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    let metadata = file.metadata()?;
    if !trusted_receipt_metadata(&metadata)
        || metadata.len() > MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES
    {
        return Err(OciError::Artifact);
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take(MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES {
        return Err(OciError::Artifact);
    }
    Ok(Some(serde_json::from_slice(&bytes)?))
}

fn write_reconciliation_checkpoint(
    root: &Path,
    destination: &Path,
    checkpoint: &InstallationReconciliationCheckpoint,
) -> Result<(), OciError> {
    let bytes = canonical_protocol_json(checkpoint).map_err(|_| OciError::Artifact)?;
    if bytes.len() as u64 > MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES {
        return Err(OciError::Artifact);
    }
    let temporary = root.join(format!(".checkpoint-{}.tmp", uuid::Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    fs::rename(&temporary, destination)?;
    File::open(root)?.sync_all()?;
    Ok(())
}

fn reconciliation_receipt_sha256(
    checkpoint: &InstallationReconciliationCheckpoint,
) -> Result<String, OciError> {
    if checkpoint.state != InstallationReconciliationState::Complete {
        return Err(OciError::Artifact);
    }
    let bytes = canonical_protocol_json(checkpoint).map_err(|_| OciError::Artifact)?;
    Ok(protocol_sha256(&bytes))
}

fn reconciliation_directory_bytes(path: &Path) -> Result<u64, OciError> {
    fn visit(path: &Path, root: &Path, total: &mut u64) -> Result<(), OciError> {
        let mut entries = fs::read_dir(path)?.collect::<Result<Vec<_>, _>>()?;
        entries.sort_by_key(fs::DirEntry::file_name);
        for entry in entries {
            let file_type = entry.file_type()?;
            if path == root && entry.file_name() == "runtime-cache" {
                // This exact top-level subtree is helper-owned and may be
                // root-only. Its removal is separately proven by the signed
                // helper tombstone; do not traverse it as the agent user.
                if !file_type.is_dir() || file_type.is_symlink() {
                    return Err(OciError::Artifact);
                }
                continue;
            }
            if file_type.is_symlink() {
                return Err(OciError::Artifact);
            }
            if file_type.is_dir() {
                visit(&entry.path(), root, total)?;
            } else if file_type.is_file() {
                let size = entry.metadata()?.len();
                *total = total.checked_add(size).ok_or(OciError::Artifact)?;
            } else {
                return Err(OciError::Artifact);
            }
        }
        Ok(())
    }
    let mut total = 0_u64;
    visit(path, path, &mut total)?;
    Ok(total)
}

fn materialized_model_bytes(
    data_root: &Path,
    installation_id: &str,
    spec: &CompiledExecutionPlan,
) -> Result<u64, OciError> {
    let models = managed_path(data_root, "installations", installation_id)?.join("models");
    let metadata = match fs::symlink_metadata(&models) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(error) => return Err(error.into()),
    };
    if metadata.file_type().is_symlink() || !metadata.file_type().is_dir() {
        return Err(OciError::Artifact);
    }
    unique_plan_artifacts(spec)
        .into_iter()
        .try_fold(0_u64, |total, artifact| {
            let path = models.join(&artifact.selection_id).join(&artifact.path);
            let metadata = fs::symlink_metadata(path)?;
            if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
                return Err(OciError::Artifact);
            }
            total.checked_add(metadata.len()).ok_or(OciError::Artifact)
        })
}

fn visit_files(
    root: &Path,
    directory: &Path,
    files: &mut BTreeMap<String, String>,
    total: &mut u64,
) -> Result<(), OciError> {
    let mut entries = fs::read_dir(directory)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);
    for entry in entries {
        let metadata = entry.file_type()?;
        let path = entry.path();
        if metadata.is_symlink() {
            return Err(OciError::Artifact);
        }
        if metadata.is_dir() {
            visit_files(root, &path, files, total)?;
        } else if metadata.is_file() {
            let relative = path.strip_prefix(root).map_err(|_| OciError::Artifact)?;
            let name = relative
                .to_str()
                .ok_or(OciError::Artifact)?
                .replace('\\', "/");
            if name.contains("..") {
                return Err(OciError::Artifact);
            }
            let mut file = File::open(&path)?;
            let mut hasher = Sha256::new();
            let mut buffer = [0_u8; 64 * 1024];
            loop {
                let read = file.read(&mut buffer)?;
                if read == 0 {
                    break;
                }
                hasher.update(&buffer[..read]);
                *total = total.checked_add(read as u64).ok_or(OciError::Artifact)?;
            }
            files.insert(name, hex::encode(hasher.finalize()));
        } else {
            return Err(OciError::Artifact);
        }
    }
    Ok(())
}

fn atomic_write(root: &Path, name: &str, value: &[u8]) -> Result<(), OciError> {
    let temporary: PathBuf = root.join(format!(".{name}.{}.tmp", std::process::id()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(value)?;
    file.sync_all()?;
    fs::rename(temporary, root.join(name))?;
    Ok(())
}

/// Prepare the agent-owned temporary mount boundary without traversing its
/// contents. The helper and workload create private directories below it;
/// only the authorized helper can remove them after proving the old container
/// absent. A retained start must never erase a live workload's temporary work.
fn ensure_runtime_tmp(outputs: &Path) -> Result<(), OciError> {
    let output_metadata = fs::symlink_metadata(outputs)?;
    if output_metadata.file_type().is_symlink() || !output_metadata.is_dir() {
        return Err(OciError::Artifact);
    }
    let temporary = outputs.join("tmp");
    match fs::symlink_metadata(&temporary) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(OciError::Artifact);
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir(&temporary)?;
        }
        Err(error) => return Err(error.into()),
    }
    fs::set_permissions(&temporary, fs::Permissions::from_mode(0o700))?;
    let metadata = fs::symlink_metadata(&temporary)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() || metadata.mode() & 0o077 != 0 {
        return Err(OciError::Artifact);
    }
    Ok(())
}

fn read_regular_file(path: &Path, maximum_bytes: u64) -> Result<Vec<u8>, OciError> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.file_type().is_file() || metadata.len() > maximum_bytes {
        return Err(OciError::Artifact);
    }
    let mut value = Vec::with_capacity(metadata.len() as usize);
    Read::by_ref(&mut file)
        .take(maximum_bytes.saturating_add(1))
        .read_to_end(&mut value)?;
    if value.len() as u64 > maximum_bytes {
        return Err(OciError::Artifact);
    }
    Ok(value)
}

fn canonical_uuid(value: &str) -> bool {
    uuid::Uuid::parse_str(value).is_ok_and(|parsed| parsed.to_string() == value)
}

#[cfg(test)]
mod tests {
    use super::{
        InstallationReconciliationState, OciError, OciRuntime, SHA256_OPEN_FILE_CALLS,
        canonical_protocol_json, ensure_runtime_tmp, materialize_compiled_models, protocol_sha256,
        read_installation_metadata, read_reconciliation_directory_identity,
        reconciliation_checkpoint_path, reconciliation_directory_bytes,
        reconciliation_quarantine_path, release_page_cache, unique_plan_artifacts,
        write_installation_metadata, write_reconciliation_checkpoint,
    };
    use crate::process::{ProcessError, ProcessOutput, ProcessRunner, Program};
    use serde_json::{Value, json};
    use sha2::Digest;
    use std::{
        fs,
        os::unix::fs::{MetadataExt, PermissionsExt, symlink},
        path::{Path, PathBuf},
        time::Duration,
    };
    use tempfile::tempdir;
    use uuid::Uuid;

    fn reconciliation_identity(
        installation_id: Uuid,
        spec_bytes: &[u8],
    ) -> vonk_agent_protocol::RecipeReconciliationIdentity {
        let value: Value = serde_json::from_slice(spec_bytes).unwrap();
        let canonical = canonical_protocol_json(&value).unwrap();
        vonk_agent_protocol::RecipeReconciliationIdentity {
            compiled_spec_canonical_sha256: protocol_sha256(&canonical),
            install_operation_id: Uuid::new_v4(),
            install_operation_payload_sha256: "a".repeat(64),
            installation_id,
            node_id: format!("spk_{}", "b".repeat(32)),
            plan_digest: "c".repeat(64),
            recipe_content_sha256: "d".repeat(64),
            recipe_revision_id: Uuid::new_v4(),
            schema_version: 1,
        }
    }

    fn reconciliation_installation(
        data_root: &Path,
        installation_id: Uuid,
    ) -> (
        PathBuf,
        vonk_agent_protocol::RecipeReconciliationIdentity,
        u64,
    ) {
        let installation = data_root
            .join("installations")
            .join(installation_id.to_string());
        fs::create_dir_all(&installation).unwrap();
        fs::set_permissions(&installation, fs::Permissions::from_mode(0o700)).unwrap();
        let spec = serde_json::to_vec(&json!({
            "identity": {"recipe_revision_sha256": "d".repeat(64)},
            "old invalid launch shape": [false, null, {"opaque": "preserved"}],
        }))
        .unwrap();
        fs::write(installation.join("spec.json"), &spec).unwrap();
        fs::set_permissions(
            installation.join("spec.json"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        let recipe_digest = "d".repeat(64);
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
        fs::write(installation.join("opaque-agent-file"), b"agent-owned").unwrap();
        let identity = reconciliation_identity(installation_id, &spec);
        let measured = spec.len() as u64 + recipe_digest.len() as u64 + b"agent-owned".len() as u64;
        (installation, identity, measured)
    }

    #[test]
    fn releasing_a_models_pages_keeps_its_bytes_and_refuses_a_directory() {
        // The hint must be exactly that: the artifact stays on disk, unchanged,
        // and only its resident pages are dropped. A directory is a caller
        // mistake rather than a silently ignored no-op.
        let directory = tempdir().unwrap();
        let path = directory.path().join("model.safetensors");
        let content = vec![7_u8; 4 * 1024 * 1024];
        fs::write(&path, &content).unwrap();
        // Read it once so the pages are resident before the release.
        let observed = fs::read(&path).unwrap();
        assert_eq!(observed.len(), content.len());
        release_page_cache(&path).unwrap();
        assert_eq!(fs::read(&path).unwrap(), content);
        assert!(matches!(
            release_page_cache(directory.path()),
            Err(OciError::Artifact)
        ));
    }

    struct NoProcess;

    impl ProcessRunner for NoProcess {
        fn run(
            &self,
            _: Program,
            _: &[String],
            _: Duration,
        ) -> Result<ProcessOutput, ProcessError> {
            panic!("OCI verification tests must not launch a process");
        }
    }

    #[test]
    fn reconciliation_measures_agent_tree_but_skips_only_helper_owned_runtime_cache() {
        let directory = tempdir().unwrap();
        let data_root = directory.path().join("data");
        fs::create_dir_all(&data_root).unwrap();
        let installation_id = Uuid::new_v4();
        let (installation, identity, expected_bytes) =
            reconciliation_installation(&data_root, installation_id);
        let runtime_cache = installation.join("runtime-cache");
        fs::create_dir(&runtime_cache).unwrap();
        fs::write(runtime_cache.join("private-cache.bin"), vec![3_u8; 8192]).unwrap();
        fs::set_permissions(&runtime_cache, fs::Permissions::from_mode(0))
            .expect("test owns the cache directory metadata");

        assert_eq!(
            reconciliation_directory_bytes(&installation).unwrap(),
            expected_bytes
        );
        let runtime = OciRuntime {
            runner: &NoProcess,
            data_root: &data_root,
            huggingface_curl_config: None,
        };
        let progress = runtime.prepare_reconciliation(&identity).unwrap();
        assert_eq!(progress.removed_bytes, expected_bytes);
        assert!(!progress.complete);
    }

    #[test]
    fn reconciliation_refuses_symlinked_agent_owned_paths_outside_the_cache() {
        let directory = tempdir().unwrap();
        let data_root = directory.path().join("data");
        fs::create_dir_all(&data_root).unwrap();
        let outside = directory.path().join("outside");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("sentinel"), b"preserve").unwrap();
        let installation_id = Uuid::new_v4();
        let (installation, identity, _) = reconciliation_installation(&data_root, installation_id);
        symlink(&outside, installation.join("opaque-link")).unwrap();

        let runtime = OciRuntime {
            runner: &NoProcess,
            data_root: &data_root,
            huggingface_curl_config: None,
        };
        assert!(matches!(
            runtime.prepare_reconciliation(&identity),
            Err(OciError::Artifact)
        ));
        assert_eq!(fs::read(outside.join("sentinel")).unwrap(), b"preserve");
        assert!(
            !data_root
                .join("installation-reconciliation")
                .join(format!("{installation_id}.json"))
                .exists()
        );
    }

    #[test]
    fn reconciliation_removing_checkpoint_recovers_after_partial_or_complete_quarantine_deletion() {
        for delete_quarantine_before_retry in [false, true] {
            let directory = tempdir().unwrap();
            let data_root = directory.path().join("data");
            fs::create_dir_all(&data_root).unwrap();
            let installation_id = Uuid::new_v4();
            let (installation, identity, expected_bytes) =
                reconciliation_installation(&data_root, installation_id);
            let runtime = OciRuntime {
                runner: &NoProcess,
                data_root: &data_root,
                huggingface_curl_config: None,
            };
            runtime.prepare_reconciliation(&identity).unwrap();

            let checkpoint_root = data_root.join("installation-reconciliation");
            let checkpoint_path =
                reconciliation_checkpoint_path(&checkpoint_root, &installation_id.to_string())
                    .unwrap();
            let quarantine =
                reconciliation_quarantine_path(&checkpoint_root, &installation_id.to_string())
                    .unwrap();
            let mut checkpoint = super::read_reconciliation_checkpoint(&checkpoint_path)
                .unwrap()
                .unwrap();
            let expected_directory_identity = read_reconciliation_directory_identity(&installation)
                .unwrap()
                .unwrap();
            assert_eq!(
                expected_directory_identity,
                (
                    checkpoint.installation_device,
                    checkpoint.installation_inode
                )
            );
            fs::rename(&installation, &quarantine).unwrap();
            checkpoint.state = InstallationReconciliationState::Removing;
            write_reconciliation_checkpoint(&checkpoint_root, &checkpoint_path, &checkpoint)
                .unwrap();
            if delete_quarantine_before_retry {
                fs::remove_dir_all(&quarantine).unwrap();
            } else {
                fs::remove_file(quarantine.join("opaque-agent-file")).unwrap();
            }

            let resumed = runtime.prepare_reconciliation(&identity).unwrap();
            assert!(!resumed.complete);
            assert_eq!(resumed.removed_bytes, expected_bytes);
            let completed = runtime.finalize_reconciliation(&identity).unwrap();
            assert!(completed.complete);
            assert_eq!(completed.removed_bytes, expected_bytes);
            assert!(completed.cleanup_receipt_sha256.is_some());
            assert!(!installation.exists());
            assert!(!quarantine.exists());
            // A current reviewed retry of the same exact installation identity
            // can replay its durable receipt after a lost response.
            let replay = runtime.prepare_reconciliation(&identity).unwrap();
            assert!(replay.complete);
            assert_eq!(replay, completed);
        }
    }

    #[test]
    fn reconciliation_missing_installation_without_checkpoint_is_not_cleanup_success() {
        let directory = tempdir().unwrap();
        let data_root = directory.path().join("data");
        fs::create_dir_all(data_root.join("installations")).unwrap();
        let missing_id = Uuid::new_v4();
        let spec = json!({
            "identity": {"recipe_revision_sha256": "d".repeat(64)},
            "corrupt": true,
        });
        let identity = reconciliation_identity(missing_id, &serde_json::to_vec(&spec).unwrap());
        let runtime = OciRuntime {
            runner: &NoProcess,
            data_root: &data_root,
            huggingface_curl_config: None,
        };
        assert!(runtime.prepare_reconciliation(&identity).is_err());
    }

    struct Gb10MemoryRunner;

    impl ProcessRunner for Gb10MemoryRunner {
        fn run(
            &self,
            program: Program,
            _: &[String],
            _: Duration,
        ) -> Result<ProcessOutput, ProcessError> {
            assert_eq!(program, Program::NvidiaSmi);
            Ok(ProcessOutput {
                success: true,
                stdout: b"NVIDIA GB10, [N/A], [N/A], 590.44\n".to_vec(),
                stderr: vec![],
            })
        }
    }

    struct SeparateMemoryRunner {
        total_mib: u64,
        free_mib: u64,
    }

    impl ProcessRunner for SeparateMemoryRunner {
        fn run(
            &self,
            program: Program,
            _: &[String],
            _: Duration,
        ) -> Result<ProcessOutput, ProcessError> {
            assert_eq!(program, Program::NvidiaSmi);
            Ok(ProcessOutput {
                success: true,
                stdout: format!(
                    "NVIDIA RTX, {}, {}, 590.44\n",
                    self.total_mib, self.free_mib
                )
                .into_bytes(),
                stderr: vec![],
            })
        }
    }

    #[test]
    fn declared_recipe_reserve_is_the_only_agent_memory_floor() {
        let directory = tempdir().unwrap();
        let meminfo = directory.path().join("meminfo");
        // This is 122,999,999,488 bytes: enough for the 120 GB GLM demand and
        // its declared 2 GB reserve, with almost 1 GB left over.
        fs::write(
            &meminfo,
            "MemTotal: 134217728 kB\nMemAvailable: 120117187 kB\n",
        )
        .unwrap();
        let runtime = OciRuntime {
            runner: &Gb10MemoryRunner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        };

        assert!(
            runtime
                .ensure_memory_available(120_000_000_000, 2_000_000_000, "unified", &meminfo)
                .is_ok()
        );

        // 119,140,625 KiB is exactly 122,000,000,000 bytes: demand plus the
        // declared reserve must fit at the inclusive boundary.
        fs::write(
            &meminfo,
            "MemTotal: 134217728 kB\nMemAvailable: 119140625 kB\n",
        )
        .unwrap();
        assert!(
            runtime
                .ensure_memory_available(120_000_000_000, 2_000_000_000, "unified", &meminfo)
                .is_ok()
        );

        fs::write(
            &meminfo,
            "MemTotal: 134217728 kB\nMemAvailable: 119140624 kB\n",
        )
        .unwrap();
        assert!(matches!(
            runtime.ensure_memory_available(120_000_000_000, 2_000_000_000, "unified", &meminfo),
            Err(OciError::Capacity)
        ));
    }

    #[test]
    fn host_only_demand_fits_without_counting_separate_vram() {
        let directory = tempdir().unwrap();
        let meminfo = directory.path().join("meminfo");
        fs::write(
            &meminfo,
            "MemTotal: 134217728 kB\nMemAvailable: 50331648 kB\n",
        )
        .unwrap();
        let runtime = OciRuntime {
            runner: &SeparateMemoryRunner {
                total_mib: 65_536,
                free_mib: 8_192,
            },
            data_root: directory.path(),
            huggingface_curl_config: None,
        };

        assert!(
            runtime
                .ensure_memory_available(17 * 1024_u64.pow(3), 0, "host", &meminfo)
                .is_ok()
        );
    }

    #[test]
    fn accelerator_only_demand_fits_without_counting_separate_host_ram() {
        let directory = tempdir().unwrap();
        let meminfo = directory.path().join("meminfo");
        fs::write(
            &meminfo,
            "MemTotal: 134217728 kB\nMemAvailable: 8388608 kB\n",
        )
        .unwrap();
        let runtime = OciRuntime {
            runner: &SeparateMemoryRunner {
                total_mib: 65_536,
                free_mib: 49_152,
            },
            data_root: directory.path(),
            huggingface_curl_config: None,
        };

        assert!(
            runtime
                .ensure_memory_available(17 * 1024_u64.pow(3), 0, "accelerator", &meminfo)
                .is_ok()
        );
    }

    #[test]
    fn unified_separate_demand_needs_both_pools_and_shared_uses_host_capacity() {
        let directory = tempdir().unwrap();
        let meminfo = directory.path().join("meminfo");
        fs::write(
            &meminfo,
            "MemTotal: 134217728 kB\nMemAvailable: 8388608 kB\n",
        )
        .unwrap();
        let separate_runner = SeparateMemoryRunner {
            total_mib: 65_536,
            free_mib: 49_152,
        };
        let separate = OciRuntime {
            runner: &separate_runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        };
        assert!(matches!(
            separate.ensure_memory_available(17 * 1024_u64.pow(3), 0, "unified", &meminfo),
            Err(OciError::Capacity)
        ));

        fs::write(
            &meminfo,
            "MemTotal: 134217728 kB\nMemAvailable: 50331648 kB\n",
        )
        .unwrap();
        let shared = OciRuntime {
            runner: &Gb10MemoryRunner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        };
        for memory_kind in ["host", "accelerator", "unified"] {
            assert!(
                shared
                    .ensure_memory_available(17 * 1024_u64.pow(3), 0, memory_kind, &meminfo)
                    .is_ok()
            );
        }
    }

    fn digest(value: &[u8]) -> String {
        hex::encode(sha2::Sha256::digest(value))
    }

    fn compiled_plan() -> Value {
        let canonical: Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
        ))
        .unwrap();
        let primary = digest(b"primary");
        let secondary = digest(b"secondary");
        json!({
            "schema_version": 2,
            "identity": {
                "recipe_revision_sha256": "a".repeat(64),
                "execution_sha256": "b".repeat(64),
                "harness_sha256": "c".repeat(64),
                "build_input_sha256": null,
                "model_artifact_set_sha256": "d".repeat(64),
                "model_artifact_bytes": 16
            },
            "runtime": {
                "telemetry": canonical["runtime"]["telemetry"],
                "executable": "/opt/vonk/bin/vllm",
                "argv": ["serve", "/models"],
                "env": [],
                "image_digest": format!("sha256:{}", "1".repeat(64)),
                "placement": {
                    "endpoint_address": null,
                    "rank": 0,
                    "role": "entrypoint",
                    "world_size": 1,
                    "local_address": null,
                    "master_address": null,
                    "master_port": null,
                    "port": 8000,
                    "reserved_memory_bytes": 4096,
                    "memory_floor_bytes": 0,
                    "memory_kind": "unified"
                }
            },
            "artifacts": [
                {
                    "selection_id": "primary",
                    "file_id": "config-primary",
                    "path": "config.json",
                    "sha256": primary,
                    "size_bytes": 7,
                    "roles": ["entrypoint"],
                    "mount": {"target": "/models", "read_only": true},
                    "model": {"publisher": "vonk-forge", "slug": "primary-model", "content_sha256": "e".repeat(64)},
                    "distribution_object": {"name": "config.json", "sha256": primary, "bytes": 7, "kind": "model"}
                },
                {
                    "selection_id": "secondary",
                    "file_id": "config-secondary",
                    "path": "config.json",
                    "sha256": secondary,
                    "size_bytes": 9,
                    "roles": ["entrypoint"],
                    "mount": {"target": "/models/secondary", "read_only": true},
                    "model": {"publisher": "vonk-forge", "slug": "secondary-model", "content_sha256": "f".repeat(64)},
                    "distribution_object": {"name": "config.json", "sha256": secondary, "bytes": 9, "kind": "model"}
                }
            ],
            "runtime_image": {
                "image_digest": format!("sha256:{}", "1".repeat(64)),
                "registry_manifest_digest": format!("sha256:{}", "3".repeat(64)),
                "platform_manifest_digest": format!("sha256:{}", "1".repeat(64)),
                "local_image_config_id": format!("sha256:{}", "4".repeat(64)),
                "local_image_reference": format!("localhost/vonk/compiled-runtime-{}@sha256:{}", "2".repeat(64), "1".repeat(64)),
                "runtime_interface_label": "v1",
                "oci_layout_sha256": "2".repeat(64),
                "image_bytes": 4096,
                "architecture": "linux-arm64",
                "runtime_interface": "vonk.runtime.v1",
                "source": "published",
                "build_id": null,
                "distribution_object": {"name": "image.oci.tar", "sha256": "2".repeat(64), "bytes": 4096, "kind": "oci-archive"}
            },
            "security": {
                "devices": [], "capabilities": [], "host_network": false,
                "network_mode": "none",
                "privileged": false, "user": "10001:10001",
                "mounts": [
                    {"source": "model", "target": "/models", "read_only": true},
                    {"source": "outputs", "target": "/outputs", "read_only": false}
                ],
                "read_only_root": true, "no_new_privileges": true
            },
            "topology": {
                "name": "solo", "mode": "single", "backend": "local",
                "node_count": 1, "world_size": 1, "rank": 0, "role": "entrypoint"
            },
            "lifecycle": {"pre_start": [], "post_stop": [], "stop_timeout_seconds": 30},
            "endpoint": {
                "protocol": "openai", "port": 8000,
                "model_aliases": ["primary"], "health_path": "/v1/models"
            },
            "job": null
        })
    }

    fn large_plan() -> crate::workloads::CompiledExecutionPlan {
        let mut value = compiled_plan();
        let artifacts = value["artifacts"].as_array_mut().unwrap();
        let template = artifacts[0].clone();
        for index in 2..751 {
            let mut artifact = template.clone();
            artifact["selection_id"] = json!(format!("model-{index:04}"));
            artifact["file_id"] = json!(format!("config-{index:04}"));
            artifact["model"]["slug"] = json!(format!("primary-model-{index:04}"));
            artifact["mount"]["target"] = json!(format!("/models/model-{index:04}"));
            artifacts.push(artifact);
        }
        serde_json::from_value(value).unwrap()
    }

    fn persisted_installation(
        data: &Path,
    ) -> (String, PathBuf, crate::workloads::CompiledExecutionPlan) {
        let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f".to_owned();
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        persisted_plan_installation(data, installation_id, plan)
    }

    fn persisted_plan_installation(
        data: &Path,
        installation_id: String,
        plan: crate::workloads::CompiledExecutionPlan,
    ) -> (String, PathBuf, crate::workloads::CompiledExecutionPlan) {
        let installation = data.join("installations").join(&installation_id);
        for artifact in unique_plan_artifacts(&plan) {
            let path = installation
                .join("models")
                .join(&artifact.selection_id)
                .join(&artifact.path);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(
                &path,
                if artifact.size_bytes == 7 {
                    b"primary".as_slice()
                } else {
                    b"secondary".as_slice()
                },
            )
            .unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        }
        fs::write(
            installation.join("spec.json"),
            serde_json::to_vec(&plan).unwrap(),
        )
        .unwrap();
        write_installation_metadata(&installation, &plan).unwrap();
        (installation_id, installation, plan)
    }

    fn runtime<'a>(data: &'a Path, runner: &'a NoProcess) -> OciRuntime<'a, NoProcess> {
        OciRuntime {
            runner,
            data_root: data,
            huggingface_curl_config: None,
        }
    }

    fn authorize_installation(installation: &Path, recipe_digest: &str) {
        fs::write(installation.join("recipe-content.sha256"), recipe_digest).unwrap();
    }

    #[test]
    fn singleton_start_persists_authoritative_observation_binding_without_rendezvous_defaults() {
        let data = tempdir().unwrap();
        let (installation_id, installation, plan) = persisted_installation(data.path());
        let recipe_digest = "9".repeat(64);
        authorize_installation(&installation, &recipe_digest);
        let run_id = Uuid::new_v4().to_string();
        let placement = plan.runtime.placement.clone();
        let identity = super::RecipeRunStartIdentity {
            mapping_generation: 12,
            mapping_id: Uuid::new_v4(),
            recipe_content_sha256: recipe_digest,
            recipe_revision_id: Uuid::new_v4(),
            run_generation: 7,
        };
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);

        runtime
            .prepare_start_with_inspection_identity(
                &plan,
                &installation_id,
                &run_id,
                &placement,
                &identity,
            )
            .unwrap();

        let lifecycle: Value = serde_json::from_slice(
            &fs::read(
                data.path()
                    .join("run-metadata")
                    .join(&run_id)
                    .join("lifecycle.json"),
            )
            .unwrap(),
        )
        .unwrap();
        let observation = lifecycle["observation"].clone();
        assert!(observation["local_address"].is_null());
        assert!(observation["master_address"].is_null());
        assert!(observation["master_port"].is_null());
        assert_eq!(observation["run_generation"], 7);
        assert_eq!(observation["mapping_generation"], 12);
        let binding: vonk_agent_protocol::RecipeRunInspectionBinding =
            serde_json::from_value(observation).unwrap();
        binding.validate().unwrap();
    }

    #[test]
    fn interrupted_post_stop_hook_cannot_be_issued_twice() {
        let data = tempdir().unwrap();
        let run_id = Uuid::new_v4().to_string();
        let metadata = data.path().join("run-metadata").join(&run_id);
        fs::create_dir_all(&metadata).unwrap();
        fs::set_permissions(&metadata, fs::Permissions::from_mode(0o700)).unwrap();
        let runner = NoProcess;
        let first_agent = runtime(data.path(), &runner);
        first_agent.begin_post_stop_hooks(&run_id).unwrap();

        let restarted_agent = runtime(data.path(), &runner);
        assert!(matches!(
            restarted_agent.begin_post_stop_hooks(&run_id),
            Err(OciError::PostStopHooksStarted)
        ));
        restarted_agent.complete_stop(&run_id).unwrap();
        assert!(!metadata.join("post-stop-hooks.started").exists());
    }

    #[test]
    fn fresh_claim_retains_exact_started_plan_without_resetting_writable_state() {
        let data = tempdir().unwrap();
        let (installation_id, installation, plan) = persisted_installation(data.path());
        authorize_installation(&installation, &plan.identity.recipe_revision_sha256);
        let run_id = Uuid::new_v4().to_string();
        let runner = NoProcess;
        let first_agent = runtime(data.path(), &runner);
        first_agent
            .prepare_start(&plan, &installation_id, &run_id, &plan.runtime.placement)
            .unwrap();
        let reset = data
            .path()
            .join("run-metadata")
            .join(&run_id)
            .join("tmp-reset-required");
        // Stand in for the helper's completed cleanup. Retained recovery must
        // not request a second cleanup after hooks or a workload have run.
        fs::remove_file(&reset).unwrap();
        let marker = data
            .path()
            .join("runs")
            .join(&run_id)
            .join("outputs/tmp")
            .join(&run_id)
            .join("in-flight-output");
        fs::create_dir_all(marker.parent().unwrap()).unwrap();
        fs::write(&marker, b"keep").unwrap();

        let restarted_agent = runtime(data.path(), &runner);
        let retained = restarted_agent
            .prepare_retained_start_if_present(
                &plan,
                &installation_id,
                &run_id,
                &plan.runtime.placement,
                None,
            )
            .unwrap()
            .unwrap();
        assert!(retained.pre_start.is_empty());
        assert!(!reset.exists());
        assert_eq!(fs::read(marker).unwrap(), b"keep");
        let mut other_placement = plan.runtime.placement.clone();
        other_placement.reserved_memory_bytes += 1;
        assert!(
            restarted_agent
                .prepare_retained_start_if_present(
                    &plan,
                    &installation_id,
                    &run_id,
                    &other_placement,
                    None,
                )
                .is_err()
        );
    }

    #[test]
    fn routine_recipe_uninstall_retains_shared_model_cache() {
        let data = tempdir().unwrap();
        let (installation_id, installation, plan) = persisted_installation(data.path());
        let recipe_digest = plan.identity.recipe_revision_sha256.clone();
        authorize_installation(&installation, &recipe_digest);
        let cached = data
            .path()
            .join("distribution")
            .join("models")
            .join(&plan.artifacts[0].sha256);
        fs::create_dir_all(cached.parent().unwrap()).unwrap();
        fs::write(&cached, b"primary").unwrap();

        let runner = NoProcess;
        runtime(data.path(), &runner)
            .uninstall(&installation_id, &recipe_digest)
            .unwrap();

        assert!(!installation.exists());
        assert_eq!(fs::read(cached).unwrap(), b"primary");
    }

    #[cfg(target_os = "linux")]
    fn apply_acl(path: &Path, entries: &[(u16, u16, u32)]) {
        let mut value = Vec::with_capacity(4 + entries.len() * 8);
        value.extend_from_slice(&0x0002_u32.to_le_bytes());
        for &(tag, permissions, identifier) in entries {
            value.extend_from_slice(&tag.to_le_bytes());
            value.extend_from_slice(&permissions.to_le_bytes());
            value.extend_from_slice(&identifier.to_le_bytes());
        }
        let file = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .unwrap();
        rustix::fs::fsetxattr(
            &file,
            "system.posix_acl_access",
            &value,
            rustix::fs::XattrFlags::empty(),
        )
        .unwrap();
    }

    #[test]
    fn trusted_installation_verification_reuses_unchanged_metadata_receipt() {
        let data = tempdir().unwrap();
        let (installation_id, _, _) = persisted_installation(data.path());
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        let before = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());

        runtime.verify_installation(&installation_id).unwrap();

        let after = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        assert_eq!(after, before);
    }

    #[test]
    fn completed_install_retry_reuses_exact_receipt_without_another_space_reservation_or_copy() {
        let data = tempdir().unwrap();
        let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f".to_owned();
        let installation = data.path().join("installations").join(&installation_id);
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        let recipe_digest = plan.identity.recipe_revision_sha256.clone();
        let model_root = data.path().join("distribution/models");
        fs::create_dir_all(&model_root).unwrap();
        for (bytes, digest) in [
            (b"primary".as_slice(), &plan.artifacts[0].sha256),
            (b"secondary".as_slice(), &plan.artifacts[1].sha256),
        ] {
            let path = model_root.join(digest);
            fs::write(&path, bytes).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
        }
        let archive = data
            .path()
            .join("oci-archives")
            .join(&plan.runtime_image.oci_layout_sha256);
        fs::create_dir_all(archive.parent().unwrap()).unwrap();
        fs::write(&archive, vec![0; plan.runtime_image.image_bytes as usize]).unwrap();
        fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();

        let runner = NoProcess;
        {
            let first_runtime = runtime(data.path(), &runner);
            // The first attempt really copies the verified distribution objects
            // and persists the installation receipt. Its acknowledgement is lost.
            first_runtime
                .install_with_space_check(&plan, &installation_id, &recipe_digest, 16)
                .unwrap();
            assert_eq!(
                fs::read(installation.join("models/primary/config.json")).unwrap(),
                b"primary"
            );
            assert!(
                installation
                    .join(super::INSTALLATION_METADATA_FILE)
                    .is_file()
            );
        }
        let runtime = runtime(data.path(), &runner);
        let unavailable_full_copy_bytes =
            crate::inventory::available_disk_bytes(data.path()).unwrap();
        assert!(matches!(
            runtime.ensure_disk_available(unavailable_full_copy_bytes),
            Err(OciError::Capacity)
        ));
        let model = installation.join("models/primary/config.json");
        let before = fs::metadata(&model).unwrap();
        let hashes_before = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());

        runtime
            .install_with_space_check(
                &plan,
                &installation_id,
                &recipe_digest,
                unavailable_full_copy_bytes,
            )
            .unwrap();

        let after = fs::metadata(&model).unwrap();
        assert_eq!(
            (after.dev(), after.ino(), after.ctime_nsec()),
            (before.dev(), before.ino(), before.ctime_nsec())
        );
        assert_eq!(
            SHA256_OPEN_FILE_CALLS.with(|calls| calls.get()),
            hashes_before
        );
        assert!(model_root.is_dir());

        fs::write(
            installation.join(super::INSTALLATION_METADATA_FILE),
            b"invalid receipt",
        )
        .unwrap();
        assert!(matches!(
            runtime.install_with_space_check(
                &plan,
                &installation_id,
                &recipe_digest,
                unavailable_full_copy_bytes,
            ),
            Err(OciError::Artifact)
        ));
    }

    #[test]
    fn installation_metadata_deduplicates_physical_projection_entries() {
        let data = tempdir().unwrap();
        let mut value = compiled_plan();
        value["identity"]["model_artifact_bytes"] = json!(7);
        let first = value["artifacts"][0].clone();
        let mut second = first.clone();
        second["mount"]["target"] = json!("/models/target");
        value["artifacts"] = json!([first, second]);
        let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let (installation_id, installation, plan) = persisted_plan_installation(
            data.path(),
            "cb555393-764b-4eb6-8f15-b416d2894291".to_owned(),
            plan,
        );
        assert_eq!(plan.artifacts.len(), 2);
        assert_eq!(
            read_installation_metadata(&installation)
                .unwrap()
                .unwrap()
                .entries
                .len(),
            1
        );

        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        let before = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        runtime.verify_installation(&installation_id).unwrap();
        assert_eq!(SHA256_OPEN_FILE_CALLS.with(|calls| calls.get()), before);

        std::thread::sleep(Duration::from_millis(2));
        fs::write(installation.join("models/primary/config.json"), b"primary").unwrap();
        runtime.verify_installation(&installation_id).unwrap();
        assert_eq!(SHA256_OPEN_FILE_CALLS.with(|calls| calls.get()), before + 1);
        runtime.verify_installation(&installation_id).unwrap();
        assert_eq!(SHA256_OPEN_FILE_CALLS.with(|calls| calls.get()), before + 1);
    }

    #[test]
    fn trusted_installation_verification_reuses_751_entry_receipt_without_hashing() {
        let data = tempdir().unwrap();
        let plan = large_plan();
        let (installation_id, _, _) = persisted_plan_installation(
            data.path(),
            "cb555393-764b-4eb6-8f15-b416d2894290".to_owned(),
            plan,
        );
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        let before = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());

        runtime.verify_installation(&installation_id).unwrap();

        let after = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        assert_eq!(after, before);
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn authorized_runtime_acl_transition_preserves_receipt_without_model_rehash() {
        let data = tempdir().unwrap();
        let (installation_id, installation, _) = persisted_installation(data.path());
        let primary = installation.join("models/primary/config.json");
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        let before = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        runtime.verify_installation(&installation_id).unwrap();
        assert_eq!(SHA256_OPEN_FILE_CALLS.with(|calls| calls.get()), before);

        let transition = runtime
            .begin_installation_acl_transition(&installation_id)
            .unwrap();
        apply_acl(
            &primary,
            &[
                (0x0001, 0o6, u32::MAX),
                (0x0002, 0o4, 10_001),
                (0x0004, 0, u32::MAX),
                (0x0010, 0o4, u32::MAX),
                (0x0020, 0, u32::MAX),
            ],
        );
        runtime
            .finish_installation_acl_transition(&installation_id, transition)
            .unwrap();
        runtime.verify_installation(&installation_id).unwrap();
        let after_acl = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        assert_eq!(after_acl, before);

        runtime.verify_installation(&installation_id).unwrap();
        assert_eq!(SHA256_OPEN_FILE_CALLS.with(|calls| calls.get()), after_acl);
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn runtime_acl_transition_refuses_same_size_content_change() {
        let data = tempdir().unwrap();
        let (installation_id, installation, _) = persisted_installation(data.path());
        let primary = installation.join("models/primary/config.json");
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        let transition = runtime
            .begin_installation_acl_transition(&installation_id)
            .unwrap();
        fs::write(&primary, b"changed").unwrap();
        assert!(
            runtime
                .finish_installation_acl_transition(&installation_id, transition)
                .is_err()
        );
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn trusted_installation_verification_rejects_unauthorized_runtime_acls() {
        for entries in [
            vec![
                (0x0001, 0o6, u32::MAX),
                (0x0002, 0o4, 10_001),
                (0x0004, 0o4, u32::MAX),
                (0x0010, 0o4, u32::MAX),
                (0x0020, 0, u32::MAX),
            ],
            vec![
                (0x0001, 0o6, u32::MAX),
                (0x0002, 0o4, 10_001),
                (0x0004, 0, u32::MAX),
                (0x0010, 0o4, u32::MAX),
                (0x0020, 0o4, u32::MAX),
            ],
            vec![
                (0x0001, 0o6, u32::MAX),
                (0x0002, 0o4, 10_001),
                (0x0002, 0o4, 10_002),
                (0x0004, 0, u32::MAX),
                (0x0010, 0o4, u32::MAX),
                (0x0020, 0, u32::MAX),
            ],
            vec![
                (0x0001, 0o6, u32::MAX),
                (0x0002, 0o6, 10_001),
                (0x0004, 0, u32::MAX),
                (0x0010, 0o4, u32::MAX),
                (0x0020, 0, u32::MAX),
            ],
        ] {
            let data = tempdir().unwrap();
            let (installation_id, installation, _) = persisted_installation(data.path());
            let primary = installation.join("models/primary/config.json");
            apply_acl(&primary, &entries);
            let runner = NoProcess;
            let runtime = runtime(data.path(), &runner);
            assert!(
                matches!(
                    runtime.verify_installation(&installation_id),
                    Err(OciError::Artifact)
                ),
                "entries {entries:?}"
            );
        }
    }

    #[test]
    fn trusted_installation_verification_hashes_and_rejects_same_size_mutation() {
        let data = tempdir().unwrap();
        let (installation_id, installation, _) = persisted_installation(data.path());
        let primary = installation.join("models/primary/config.json");
        fs::write(&primary, b"mutated").unwrap();
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        let before = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());

        assert!(matches!(
            runtime.verify_installation(&installation_id),
            Err(OciError::Artifact)
        ));

        let after = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        assert_eq!(after, before + 1);
    }

    #[test]
    fn trusted_installation_verification_refreshes_after_metadata_only_change() {
        let data = tempdir().unwrap();
        let (installation_id, installation, _) = persisted_installation(data.path());
        let primary = installation.join("models/primary/config.json");
        std::thread::sleep(Duration::from_millis(2));
        fs::write(&primary, b"primary").unwrap();
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        let before = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());

        runtime.verify_installation(&installation_id).unwrap();

        let after = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        assert_eq!(after, before + 1);
        runtime.verify_installation(&installation_id).unwrap();
        let final_count = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        assert_eq!(final_count, after);
    }

    #[test]
    fn trusted_installation_verification_rejects_invalid_file_metadata() {
        let cases = ["size", "symlink", "directory", "mode", "nlink", "owner"];
        for case in cases {
            let data = tempdir().unwrap();
            let (installation_id, installation, _) = persisted_installation(data.path());
            let primary = installation.join("models/primary/config.json");
            match case {
                "size" => fs::write(&primary, b"short").unwrap(),
                "symlink" => {
                    fs::remove_file(&primary).unwrap();
                    symlink(installation.join("models/secondary/config.json"), &primary).unwrap();
                }
                "directory" => {
                    fs::remove_file(&primary).unwrap();
                    fs::create_dir(&primary).unwrap();
                }
                "mode" => fs::set_permissions(&primary, fs::Permissions::from_mode(0o644)).unwrap(),
                "nlink" => fs::hard_link(
                    &primary,
                    installation.join("models/primary/config-link.json"),
                )
                .unwrap(),
                "owner" => {
                    if rustix::process::geteuid().as_raw() != 0 {
                        continue;
                    }
                    rustix::fs::chown(&primary, Some(rustix::process::Uid::from_raw(65_534)), None)
                        .unwrap();
                }
                _ => unreachable!(),
            }
            let runner = NoProcess;
            let runtime = runtime(data.path(), &runner);
            assert!(
                matches!(
                    runtime.verify_installation(&installation_id),
                    Err(OciError::Artifact)
                ),
                "case {case}"
            );
        }
    }

    #[test]
    fn restart_preparation_leaves_private_runtime_tmp_for_the_authorized_helper() {
        use std::os::unix::process::CommandExt;

        const CHILD_ROOT: &str = "VONK_RESTART_TMP_TEST_ROOT";
        if let Ok(root) = std::env::var(CHILD_ROOT) {
            let root = Path::new(&root);
            let installation_id = std::env::var("VONK_RESTART_TMP_INSTALLATION").unwrap();
            let run_id = std::env::var("VONK_RESTART_TMP_RUN").unwrap();
            let runner = NoProcess;
            let runtime = runtime(root, &runner);
            let plan = runtime.load_spec(&installation_id).unwrap();
            runtime
                .prepare_start(&plan, &installation_id, &run_id, &plan.runtime.placement)
                .unwrap();
            return;
        }

        let data = tempdir().unwrap();
        let (installation_id, _, plan) = persisted_installation(data.path());
        let run_id = Uuid::new_v4().to_string();
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        runtime
            .prepare_start(&plan, &installation_id, &run_id, &plan.runtime.placement)
            .unwrap();
        runtime.complete_stop(&run_id).unwrap();
        let private = data
            .path()
            .join("runs")
            .join(&run_id)
            .join("outputs/tmp")
            .join(&run_id);
        fs::create_dir(&private).unwrap();
        fs::write(private.join("engine-owned"), b"temporary").unwrap();
        fs::set_permissions(&private, fs::Permissions::from_mode(0o700)).unwrap();

        let mut child = std::process::Command::new(std::env::current_exe().unwrap());
        child.args(["--exact", "oci::tests::restart_preparation_leaves_private_runtime_tmp_for_the_authorized_helper", "--nocapture"])
            .env(CHILD_ROOT, data.path())
            .env("VONK_RESTART_TMP_INSTALLATION", &installation_id)
            .env("VONK_RESTART_TMP_RUN", &run_id);
        if rustix::process::geteuid().is_root() {
            fn agent_owns(path: &Path) {
                rustix::fs::chown(path, Some(rustix::process::Uid::from_raw(65534)), None).unwrap();
                if path.is_dir() {
                    for entry in fs::read_dir(path).unwrap() {
                        agent_owns(&entry.unwrap().path());
                    }
                }
            }
            agent_owns(data.path());
            // The helper creates this directory as root and grants only the
            // runtime UID access. The unprivileged agent cannot traverse it.
            rustix::fs::chown(&private, Some(rustix::process::Uid::ROOT), None).unwrap();
            child.uid(65534).gid(65534);
        } else {
            fs::set_permissions(&private, fs::Permissions::from_mode(0o000)).unwrap();
        }
        let result = child.output().unwrap();
        fs::set_permissions(&private, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(
            result.status.success(),
            "{}{}",
            String::from_utf8_lossy(&result.stdout),
            String::from_utf8_lossy(&result.stderr)
        );
        assert_eq!(
            fs::read(private.join("engine-owned")).unwrap(),
            b"temporary"
        );
    }

    #[test]
    fn runtime_tmp_boundary_is_kept_private_without_deleting_runtime_owned_content() {
        let data = tempdir().unwrap();
        let outputs = data.path().join("outputs");
        fs::create_dir_all(outputs.join("tmp")).unwrap();
        fs::write(outputs.join("tmp").join("stale.marker"), b"stale").unwrap();

        ensure_runtime_tmp(&outputs).unwrap();

        let temporary = outputs.join("tmp");
        assert!(temporary.join("stale.marker").exists());
        let metadata = fs::symlink_metadata(temporary).unwrap();
        assert!(metadata.is_dir());
        assert!(!metadata.file_type().is_symlink());
        assert_eq!(metadata.mode() & 0o777, 0o700);
    }

    #[test]
    fn runtime_tmp_refuses_symlink_replacement_targets() {
        let data = tempdir().unwrap();
        let outputs = data.path().join("outputs");
        fs::create_dir(&outputs).unwrap();
        let target = data.path().join("outside");
        fs::create_dir(&target).unwrap();
        symlink(&target, outputs.join("tmp")).unwrap();

        assert!(matches!(
            ensure_runtime_tmp(&outputs),
            Err(OciError::Artifact)
        ));
        assert!(target.is_dir());
    }

    #[test]
    fn model_materialization_temp_cleanup_is_task_owned() {
        let data = tempdir().unwrap();
        let temporary = data.path().join("model.partial");
        fs::write(&temporary, b"incomplete").unwrap();
        {
            let _guard = super::TemporaryArtifact::new(temporary.clone());
        }
        assert!(!temporary.exists());

        fs::write(&temporary, b"published").unwrap();
        {
            let mut guard = super::TemporaryArtifact::new(temporary.clone());
            guard.retain();
        }
        assert_eq!(fs::read(temporary).unwrap(), b"published");
    }

    #[test]
    fn compiled_models_materialize_selection_scoped_colliding_paths() {
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        plan.validate().unwrap();
        let data = tempdir().unwrap();
        let root = data.path().join("distribution").join("models");
        fs::create_dir_all(&root).unwrap();
        for (artifact, bytes) in [
            (&plan.artifacts[0], b"primary".as_slice()),
            (&plan.artifacts[1], b"secondary".as_slice()),
        ] {
            let path = root.join(&artifact.sha256);
            fs::write(&path, bytes).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        }

        let paths =
            materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
                .unwrap();
        assert_eq!(paths.len(), 2);
        assert_eq!(
            fs::read(data.path().join(
                "installations/cb555393-764b-4eb6-8f15-b416d289428f/models/primary/config.json"
            ))
            .unwrap(),
            b"primary"
        );
        assert_eq!(
            fs::read(data.path().join(
                "installations/cb555393-764b-4eb6-8f15-b416d289428f/models/secondary/config.json"
            ))
            .unwrap(),
            b"secondary"
        );
    }

    #[test]
    fn compiled_models_materialize_valid_empty_support_files() {
        let mut value = compiled_plan();
        value["identity"]["model_artifact_bytes"] = json!(0);
        let artifact = &mut value["artifacts"][0];
        artifact["selection_id"] = json!("primary");
        artifact["file_id"] = json!("tokenizer-config");
        artifact["path"] = json!("tokenizer_config.json");
        artifact["sha256"] = json!(crate::workloads::EMPTY_SHA256);
        artifact["size_bytes"] = json!(0);
        artifact["roles"] = json!(["tokenizer"]);
        artifact["distribution_object"] = json!({
            "name": "tokenizer_config.json",
            "sha256": crate::workloads::EMPTY_SHA256,
            "bytes": 0,
            "kind": "model"
        });
        value["artifacts"] = json!([artifact.clone()]);
        let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let data = tempdir().unwrap();
        let source = data.path().join("distribution").join("models");
        fs::create_dir_all(&source).unwrap();
        let source_file = source.join(&plan.artifacts[0].sha256);
        fs::write(&source_file, []).unwrap();
        fs::set_permissions(&source_file, fs::Permissions::from_mode(0o600)).unwrap();
        materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
            .unwrap();
        assert_eq!(
            fs::metadata(data.path().join("installations/cb555393-764b-4eb6-8f15-b416d289428f/models/primary/tokenizer_config.json")).unwrap().len(),
            0
        );
    }

    #[test]
    fn compiled_models_reject_duplicate_final_target() {
        let mut value = compiled_plan();
        let duplicate = value["artifacts"][0].clone();
        value["artifacts"] = json!([duplicate.clone(), duplicate]);
        let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let result = materialize_compiled_models(
            Path::new("/tmp/vonk-agent-test-data"),
            &plan,
            "cb555393-764b-4eb6-8f15-b416d289428f",
        );
        assert!(matches!(result, Err(OciError::Workload(_))));
    }

    #[test]
    fn compiled_models_materialize_one_source_for_two_mount_projections() {
        let mut value = compiled_plan();
        value["identity"]["model_artifact_bytes"] = json!(7);
        let mut projection = value["artifacts"][0].clone();
        projection["mount"]["target"] = json!("/models/target");
        value["artifacts"] = json!([value["artifacts"][0].clone(), projection]);
        let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let data = tempdir().unwrap();
        let source = data.path().join("distribution").join("models");
        fs::create_dir_all(&source).unwrap();
        let source_file = source.join(&plan.artifacts[0].sha256);
        fs::write(&source_file, b"primary").unwrap();
        fs::set_permissions(&source_file, fs::Permissions::from_mode(0o600)).unwrap();
        let paths =
            materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
                .unwrap();
        assert_eq!(paths.len(), 1);
        assert_eq!(
            paths[0],
            data.path().join(
                "installations/cb555393-764b-4eb6-8f15-b416d289428f/models/primary/config.json"
            )
        );
        let installation = data
            .path()
            .join("installations/cb555393-764b-4eb6-8f15-b416d289428f");
        write_installation_metadata(&installation, &plan).unwrap();
        let before = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        let repeated =
            materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
                .unwrap();
        assert_eq!(repeated.len(), 1);
        assert_eq!(SHA256_OPEN_FILE_CALLS.with(|calls| calls.get()), before);
        assert_eq!(plan.artifacts.len(), 2);
    }
}
