use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::IpAddr,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};

use serde::Deserialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::generated::FailureStage;
use vonk_agent_protocol::generated::{
    InstallationMetadataEntry, InstallationMetadataReceipt, InstallationReconciliationCheckpoint,
    InstallationReconciliationCheckpointState as InstallationReconciliationState,
    RunLifecycleRecord as RunLifecycle,
};
use vonk_agent_protocol::{
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES, RecipeReconciliationIdentity,
    canonical_json as canonical_protocol_json,
    compiled_oci::{
        CompiledOciPaths, start_arguments_for_paths as projected_start_arguments_for_paths,
    },
};

use crate::{
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
        stage: FailureStage,
        #[source]
        source: Box<OciError>,
    },
    #[error("start {stage} failed: {source}")]
    Start {
        stage: FailureStage,
        #[source]
        source: Box<OciError>,
    },
}

impl OciError {
    pub fn safe_start_context(&self) -> (FailureStage, &'static str) {
        let (stage, source) = match self {
            Self::Start { stage, source } => (*stage, source.as_ref()),
            error => (FailureStage::Unknown, error),
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

    pub fn safe_install_context(&self) -> (FailureStage, &'static str) {
        match self {
            Self::Install { stage, source } => (*stage, source.safe_category()),
            error => (FailureStage::Unknown, error.safe_category()),
        }
    }

    pub(crate) fn safe_category(&self) -> &'static str {
        match self {
            Self::Process(_) => "process",
            Self::Workload(_) => "workload",
            Self::Runtime => "runtime",
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
}

pub const MAX_MANAGED_RECIPE_RUNS: usize = 64;
const MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES: usize = MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES;
const MAX_RUN_DIRECTORY_ENTRIES: usize = 4096;

#[derive(Debug, Clone)]
pub struct RecipeRunInspectionPlan {
    pub run_id: uuid::Uuid,
    pub run_generation: u32,
    pub arguments: Vec<String>,
    pub endpoint_address: Option<IpAddr>,
    pub endpoint_port: u16,
    pub health_path: String,
}

type LoadedRunLifecycle = (
    CompiledExecutionPlan,
    String,
    CompiledRuntimePlacement,
    Option<u32>,
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

fn runtime_policy() -> Result<RuntimePolicy, OciError> {
    serde_json::from_str(include_str!(
        "../../../../schemas/global/container-runtime-policy-v1.json"
    ))
    .map_err(OciError::Json)
}

/// One run directory's inspection outcome; a failure names the run it belongs to.
type RunInspectionResult = Result<Option<RecipeRunInspectionPlan>, (String, OciError)>;

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
        self.install_unlocked(spec, installation_id, recipe_content_sha256, &mut |_, _| {})
    }

    fn install_unlocked(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
        progress: &mut dyn FnMut(u64, u64),
    ) -> Result<(), OciError> {
        if recipe_content_sha256.len() != 64
            || !recipe_content_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(OciError::Artifact);
        }
        self.verify_image(spec)
            .map_err(|error| install_error(FailureStage::ImageVerification, error))?;
        let installation =
            managed_path(self.data_root, "installations", installation_id).map_err(|error| {
                install_error(FailureStage::InstallationPath, OciError::Workload(error))
            })?;
        fs::create_dir_all(&installation)
            .map_err(OciError::Io)
            .map_err(|error| install_error(FailureStage::InstallationDirectory, error))?;
        fs::set_permissions(&installation, fs::Permissions::from_mode(0o700))
            .map_err(OciError::Io)
            .map_err(|error| install_error(FailureStage::InstallationDirectory, error))?;
        self.ensure_runtime_cache(installation_id)
            .map_err(|error| install_error(FailureStage::RuntimeCache, error))?;
        materialize_compiled_models_observed(self.data_root, spec, installation_id, progress)
            .map_err(|error| install_error(FailureStage::ModelMaterialization, error))?;
        let encoded_spec = serde_json::to_vec(spec)
            .map_err(OciError::Json)
            .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
        if encoded_spec.len() > MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES {
            return Err(install_error(
                FailureStage::InstallationMetadata,
                OciError::Artifact,
            ));
        }
        write_installation_metadata(self.data_root, &installation, spec)
            .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
        atomic_write(&installation, "spec.json", &encoded_spec)
            .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
        atomic_write(
            &installation,
            "recipe-content.sha256",
            recipe_content_sha256.as_bytes(),
        )
        .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
        File::open(&installation)
            .map_err(OciError::Io)
            .and_then(|file| file.sync_all().map_err(OciError::Io))
            .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
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
        self.install_with_space_check_observed(
            spec,
            installation_id,
            recipe_content_sha256,
            expected_bytes,
            &mut |_, _| {},
        )
    }

    /// As `install_with_space_check`, reporting `(copied, total)` model bytes
    /// while the installation's own model copies are written.
    pub fn install_with_space_check_observed(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
        expected_bytes: u64,
        progress: &mut dyn FnMut(u64, u64),
    ) -> Result<(), OciError> {
        let _lock = self.lock_installation_reconciliation(installation_id)?;
        self.refuse_reconciled_installation(installation_id)?;
        if self.reuse_completed_install(spec, installation_id, recipe_content_sha256)? {
            return Ok(());
        }
        // Model files already in the shared store are linked, not written, so
        // they need no free space; only what the install must still write does.
        self.ensure_disk_available(
            expected_bytes.saturating_sub(linkable_model_bytes(self.data_root, spec)),
        )?;
        self.install_unlocked(spec, installation_id, recipe_content_sha256, progress)
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
        let lock = self.lock_installation_reconciliation(&installation_id)?;
        let result = (|| {
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
                        return Ok(InstallationReconciliationProgress { complete: true });
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
                        return Ok(InstallationReconciliationProgress { complete: false });
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
                        return Ok(InstallationReconciliationProgress { complete: false });
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
            let checkpoint = InstallationReconciliationCheckpoint {
                schema_version: INSTALLATION_RECONCILIATION_SCHEMA_VERSION,
                state: InstallationReconciliationState::Prepared,
                identity: identity.clone(),
                installation_device: directory_metadata.dev(),
                installation_inode: directory_metadata.ino(),
            };
            write_reconciliation_checkpoint(&root, &checkpoint_path, &checkpoint)?;
            Ok(InstallationReconciliationProgress { complete: false })
        })();
        drop(lock);
        result
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
        let lock = self.lock_installation_reconciliation(&installation_id)?;
        let result = (|| {
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
                return Ok(InstallationReconciliationProgress { complete: true });
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
            Ok(InstallationReconciliationProgress { complete: true })
        })();
        drop(lock);
        result
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
        // flock(2) locks belong to the open file description, and a concurrent
        // fork (another thread spawning a process) briefly shares it until exec
        // closes it, so a just-released lock can still look held for a few
        // milliseconds. Wait a short bounded grace period before reporting
        // busy; a genuinely held lock still reports the retryable busy error.
        let deadline = std::time::Instant::now() + RECONCILIATION_LOCK_GRACE;
        loop {
            match rustix::fs::flock(&file, rustix::fs::FlockOperation::NonBlockingLockExclusive) {
                Ok(()) => return Ok(file),
                Err(error)
                    if error == rustix::io::Errno::AGAIN
                        || error == rustix::io::Errno::WOULDBLOCK =>
                {
                    if std::time::Instant::now() >= deadline {
                        return Err(OciError::ReconciliationBusy);
                    }
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(error) => return Err(OciError::Io(error.into())),
            }
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
        // Compiled images are always linux/arm64 and the current runtime
        // interface; the policy must agree.
        if policy.runtime_interface != "vonk.runtime.v1"
            || policy.architecture != "linux/arm64"
            || policy.required_image_label.name != "ai.vonkforge.runtime-interface"
            || spec.runtime_image.runtime_interface_label != policy.required_image_label.value
        {
            return Err(OciError::ImageDigest);
        }
        Ok(())
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
        start_stage(FailureStage::ImageVerification, || self.verify_image(spec))?;
        let state = start_stage(FailureStage::RunStorage, || {
            let state = managed_path(self.data_root, "runs", run_id)?;
            fs::create_dir_all(&state)?;
            fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
            Ok(state)
        })?;
        start_stage(FailureStage::OutputStorage, || {
            let outputs = state.join("outputs");
            fs::create_dir_all(&outputs)?;
            fs::set_permissions(&outputs, fs::Permissions::from_mode(0o700))?;
            ensure_runtime_tmp(&outputs)
        })?;
        start_stage(FailureStage::RuntimeCache, || {
            self.ensure_runtime_cache(installation_id)
        })?;
        start_stage(FailureStage::JobInputs, || {
            if spec.job.is_some() {
                let inputs = state.join("inputs");
                let metadata = fs::symlink_metadata(&inputs)?;
                if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                    return Err(OciError::Artifact);
                }
            }
            Ok(())
        })?;
        let metadata = start_stage(FailureStage::RuntimeMetadata, || {
            let metadata = self.ensure_run_metadata(run_id)?;
            self.write_runtime_contract(spec, run_id)?;
            // The first authorized helper invocation resets private runtime
            // tmp. Keep the marker outside writable mounts.
            atomic_write(&metadata, "tmp-reset-required", b"")?;
            File::open(&metadata)?.sync_all()?;
            Ok(metadata)
        })?;
        let main = start_stage(FailureStage::RuntimeProjection, || {
            self.start_arguments(spec, installation_id, run_id, placement)
        })?;
        let runtime_image_digest = spec.runtime_image.image_digest.clone();
        let run_generation = identity
            .map(|identity| {
                u32::try_from(identity.run_generation)
                    .ok()
                    .filter(|generation| *generation != 0)
                    .ok_or(OciError::Artifact)
            })
            .transpose()
            .map_err(|source| OciError::Start {
                stage: FailureStage::ObservationIdentity,
                source: Box::new(source),
            })?;
        start_stage(FailureStage::LifecycleMetadata, || {
            atomic_write(
                &metadata,
                "lifecycle.json",
                &serde_json::to_vec(&RunLifecycle {
                    installation_id: installation_id.to_owned(),
                    placement: placement.clone(),
                    run_generation,
                })?,
            )
        })?;
        Ok(RuntimeStartPlan {
            image_digest: runtime_image_digest,
            registry_index_digest: spec.runtime_image.image_digest.clone(),
            platform_manifest_digest: spec.runtime_image.image_digest.clone(),
            archive_sha256: spec.runtime_image.oci_layout_sha256.clone(),
            image_reference: spec.runtime_image.local_image_reference(),
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
            registry_index_digest: spec.runtime_image.image_digest.clone(),
            platform_manifest_digest: spec.runtime_image.image_digest.clone(),
            archive_sha256: spec.runtime_image.oci_layout_sha256.clone(),
            image_reference: spec.runtime_image.local_image_reference(),
            main: self.start_arguments(spec, installation_id, run_id, placement)?,
        })
    }

    /// A fresh claim may observe an earlier exact Start without rewriting its
    /// runtime contract or clearing tmp.
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
        let Some((_, _, _, Some(run_generation))) = self.load_run_lifecycle(run_id)? else {
            return Err(OciError::Runtime);
        };
        if u64::from(run_generation) != identity.run_generation {
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
        ) = match lifecycle {
            Some((spec, _, _, _)) => (
                Some(spec.runtime_image.image_digest.clone()),
                Some(spec.runtime_image.image_digest.clone()),
                Some(spec.runtime_image.image_digest.clone()),
                Some(spec.runtime_image.oci_layout_sha256.clone()),
                Some(spec.runtime_image.local_image_reference()),
            ),
            None => (None, None, None, None, None),
        };
        Ok(RuntimeStopPlan {
            remove: vec![run_id.to_owned(), stop_timeout.to_string()],
            image_digest,
            registry_index_digest,
            platform_manifest_digest,
            archive_sha256,
            image_reference,
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
        match fs::remove_file(metadata.join("lifecycle.json")) {
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

    pub fn recipe_run_inspection_plans(&self) -> Result<Vec<RecipeRunInspectionPlan>, OciError> {
        let mut plans = Vec::new();
        let mut failure = None;
        for result in self.recipe_run_inspection_results()? {
            match result {
                Ok(Some(plan)) => plans.push(plan),
                Ok(None) => {}
                Err((run_id, error)) => {
                    eprintln!(
                        "vonk-agent: skipping exact recipe run {run_id}: invalid managed metadata ({})",
                        error.safe_category()
                    );
                    if failure.is_none() {
                        failure = Some(error);
                    }
                }
            }
        }
        if plans.is_empty()
            && let Some(error) = failure
        {
            return Err(error);
        }
        Ok(plans)
    }

    pub(crate) fn recipe_run_inspection_results(
        &self,
    ) -> Result<Vec<RunInspectionResult>, OciError> {
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
        for (index, entry) in fs::read_dir(&runs)?.enumerate() {
            if index == MAX_RUN_DIRECTORY_ENTRIES {
                eprintln!(
                    "vonk-agent: run inspection skipped entries beyond the configured {} directory scan bound",
                    MAX_RUN_DIRECTORY_ENTRIES
                );
                break;
            }
            let entry = match entry {
                Ok(entry) => entry,
                Err(error) => {
                    eprintln!("vonk-agent: skipping unreadable run directory entry: {error}");
                    continue;
                }
            };
            let run_id = match entry.file_name().into_string() {
                Ok(run_id) => run_id,
                Err(_) => {
                    eprintln!("vonk-agent: skipping run directory entry with a non-UTF-8 name");
                    continue;
                }
            };
            let file_type = match entry.file_type() {
                Ok(file_type) => file_type,
                Err(error) => {
                    eprintln!(
                        "vonk-agent: skipping run entry {run_id}: cannot inspect type: {error}"
                    );
                    continue;
                }
            };
            if !canonical_uuid(&run_id) || !file_type.is_dir() || file_type.is_symlink() {
                eprintln!(
                    "vonk-agent: skipping malformed run entry {run_id}: expected a canonical UUID directory"
                );
                continue;
            }
            run_ids.push(run_id);
        }
        run_ids.sort_unstable();

        let mut plans = Vec::new();
        for run_id in run_ids {
            if plans
                .iter()
                .filter(|plan| matches!(plan, Ok(Some(_))))
                .count()
                == MAX_MANAGED_RECIPE_RUNS
            {
                plans.push(Err((run_id, OciError::Artifact)));
                continue;
            }
            plans.push(
                self.recipe_run_inspection_plan(&run_id)
                    .map_err(|error| (run_id, error)),
            );
        }
        Ok(plans)
    }

    fn recipe_run_inspection_plan(
        &self,
        run_id: &str,
    ) -> Result<Option<RecipeRunInspectionPlan>, OciError> {
        // Stopped run directories intentionally outlive their lifecycle. A
        // missing lifecycle or run generation is historical; malformed
        // metadata is returned to the caller as this run's isolated failure.
        let Some((spec, installation_id, placement, Some(run_generation))) =
            self.load_run_lifecycle(run_id)?
        else {
            return Ok(None);
        };
        let retained = self.prepare_retained_start(&spec, &installation_id, run_id, &placement)?;
        let mut arguments = vec![
            retained.archive_sha256.clone(),
            retained.registry_index_digest.clone(),
            retained.platform_manifest_digest.clone(),
            retained.image_reference.clone(),
        ];
        arguments.extend(retained.main);
        let endpoint_owner =
            placement.world_size == 1 || placement.local_address == placement.master_address;
        let health_path = spec
            .endpoint
            .as_ref()
            .ok_or(OciError::Artifact)?
            .health_path
            .clone();
        Ok(Some(RecipeRunInspectionPlan {
            run_id: uuid::Uuid::parse_str(run_id).map_err(|_| OciError::Artifact)?,
            run_generation,
            arguments,
            endpoint_address: if endpoint_owner {
                Some(placement.endpoint_address.ok_or(OciError::Artifact)?)
            } else {
                None
            },
            endpoint_port: placement.port.ok_or(OciError::Artifact)?,
            health_path,
        }))
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
            record.run_generation,
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

    /// Remove this installation's materialized model files, then the shared
    /// store's copy of each one that no installation links any more. Other
    /// installations keep their own files, and a store object one of them still
    /// links stays for them and for future installations.
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
        let store_objects = self.model_store_objects(
            installation_id,
            expected_recipe_digest,
            model_content_sha256,
        )?;
        self.uninstall(installation_id, expected_recipe_digest)?;
        self.reclaim_unshared_model_objects(&store_objects);
        Ok(removed_model_bytes)
    }

    /// The shared-store objects (by file digest) the installation's files of one
    /// model were made from.
    pub fn model_store_objects(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
        model_content_sha256: &str,
    ) -> Result<Vec<String>, OciError> {
        let persisted = self.load_uninstall_spec(installation_id, expected_recipe_digest)?;
        if !spec_references_model(&persisted, model_content_sha256) {
            return Err(OciError::Artifact);
        }
        Ok(unique_plan_artifacts(&persisted)
            .into_iter()
            .filter(|artifact| artifact.model.content_sha256 == model_content_sha256)
            .map(|artifact| artifact.sha256.clone())
            .filter(|sha256| lower_hex(sha256, 64))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect())
    }

    /// Remove each named store object that nothing links any more (its only
    /// link is the store's own name for it), and return the bytes that freed.
    ///
    /// Installations link store objects instead of copying them, so removing an
    /// installation alone frees none of its model bytes. An object that still
    /// has another link belongs to an installation that is still using it and
    /// is left alone. Removal is best effort: an object that cannot be removed
    /// stays for the next uninstall.
    pub fn reclaim_unshared_model_objects(&self, digests: &[String]) -> u64 {
        let owner = rustix::process::geteuid().as_raw();
        digests
            .iter()
            .filter(|digest| lower_hex(digest, 64))
            .filter_map(|digest| {
                let path = store_object_path(self.data_root, digest);
                let metadata = fs::symlink_metadata(&path).ok()?;
                (metadata.file_type().is_file()
                    && metadata.uid() == owner
                    && metadata.nlink() == 1
                    && fs::remove_file(&path).is_ok())
                .then_some(metadata.len())
            })
            .fold(0_u64, u64::saturating_add)
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
                let (_, metadata) = open_trusted_model_file(
                    &destination,
                    artifact.size_bytes,
                    shared_store_inode(self.data_root, &artifact.sha256),
                )?;
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
            let (_file, metadata) = open_trusted_model_file(
                &destination,
                artifact.size_bytes,
                shared_store_inode(self.data_root, &artifact.sha256),
            )?;
            if let Some(entry) = receipt_index.as_ref().and_then(|index| {
                index
                    .get(&(artifact.selection_id.as_str(), artifact.path.as_str()))
                    .filter(|entry| metadata_matches_receipt(&metadata, entry))
            }) {
                refreshed.push((**entry).clone());
                continue;
            }
            // Model files reached this installation from the Controller over
            // mTLS; size and trusted custody (checked above) are the check.
            refreshed.push(installation_metadata_entry(artifact, &metadata));
        }
        sort_metadata_entries(&mut refreshed);
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
            let (file, metadata) = open_trusted_model_file(
                &path,
                entry.size_bytes,
                shared_store_inode(self.data_root, &entry.sha256),
            )?;
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

    /// Record the ACL transition of an installation's model files.
    ///
    /// The step reads and verifies only: it works on a copy of the receipt and
    /// changes the stored one in a single atomic write at the end, so a failed
    /// attempt leaves the transition and the installation untouched and the
    /// same call can be repeated until it succeeds.
    pub fn finish_installation_acl_transition(
        &self,
        installation_id: &str,
        transition: &InstallationAclTransition,
    ) -> Result<(), OciError> {
        if transition.installation_id != installation_id {
            return Err(OciError::Artifact);
        }
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        if read_installation_metadata(&installation)? != Some(transition.receipt.clone()) {
            return Err(OciError::Artifact);
        }
        let previous_receipt = transition.receipt.clone();
        let mut receipt = transition.receipt.clone();
        for (entry, (path, file, before)) in receipt.entries.iter_mut().zip(transition.files.iter())
        {
            let after = file.metadata()?;
            let shared = shared_store_inode(self.data_root, &entry.sha256);
            let (reopened, path_after) = open_trusted_model_file(path, entry.size_bytes, shared)?;
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
                || !trusted_model_file(file, &after, entry.size_bytes, shared)
                || !trusted_model_file(&reopened, &path_after, entry.size_bytes, shared)
            {
                return Err(OciError::Artifact);
            }
            entry.ctime_ns = timestamp_ns(after.ctime(), after.ctime_nsec());
        }
        if receipt == previous_receipt {
            return Ok(());
        }
        atomic_write(
            &installation,
            INSTALLATION_METADATA_FILE,
            &serde_json::to_vec(&receipt)?,
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

    /// The logical size of the installation tree: every file counted at its
    /// full length, whether it is a private copy or a link to a shared model
    /// object. Only metadata is read; no content is.
    pub fn installed_bytes(&self, installation_id: &str) -> Result<u64, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        tree_bytes(&installation)
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

fn metadata_stable(before: &fs::Metadata, after: &fs::Metadata) -> bool {
    before.dev() == after.dev()
        && before.ino() == after.ino()
        && before.len() == after.len()
        && timestamp_ns(before.mtime(), before.mtime_nsec())
            == timestamp_ns(after.mtime(), after.mtime_nsec())
        && timestamp_ns(before.ctime(), before.ctime_nsec())
            == timestamp_ns(after.ctime(), after.ctime_nsec())
}

fn write_installation_metadata(
    data_root: &Path,
    installation: &Path,
    plan: &CompiledExecutionPlan,
) -> Result<(), OciError> {
    let models = installation.join("models");
    let unique_artifacts = unique_plan_artifacts(plan);
    let mut entries = Vec::with_capacity(unique_artifacts.len());
    for artifact in unique_artifacts {
        let path = models.join(&artifact.selection_id).join(&artifact.path);
        let (_, metadata) = open_trusted_model_file(
            &path,
            artifact.size_bytes,
            shared_store_inode(data_root, &artifact.sha256),
        )?;
        entries.push(installation_metadata_entry(artifact, &metadata));
    }
    sort_metadata_entries(&mut entries);
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
        // Every link another installation adds or drops moves the change time
        // of an inode they share, so it says nothing about a shared object. A
        // private inode keeps the exact change-time binding.
        && (metadata.nlink() > 1
            || timestamp_ns(metadata.ctime(), metadata.ctime_nsec()) == receipt.ctime_ns)
}

/// `(device, inode)` of the shared store object an installation's model file
/// may be a hard link of.
type SharedInode = (u64, u64);

fn store_object_path(data_root: &Path, sha256: &str) -> PathBuf {
    data_root.join("distribution").join("models").join(sha256)
}

/// The inode of the store object named `sha256`, when the agent owns a regular
/// file there. An installation's model file with more than one link is trusted
/// only when it is exactly this inode; a private copy keeps a single link.
fn shared_store_inode(data_root: &Path, sha256: &str) -> Option<SharedInode> {
    if !lower_hex(sha256, 64) {
        return None;
    }
    let metadata = fs::symlink_metadata(store_object_path(data_root, sha256)).ok()?;
    (metadata.file_type().is_file() && metadata.uid() == rustix::process::geteuid().as_raw())
        .then(|| (metadata.dev(), metadata.ino()))
}

fn trusted_model_shape(
    metadata: &fs::Metadata,
    expected_bytes: u64,
    shared: Option<SharedInode>,
) -> bool {
    metadata.file_type().is_file()
        && !metadata.file_type().is_symlink()
        && (metadata.nlink() == 1 || shared == Some((metadata.dev(), metadata.ino())))
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && metadata.len() == expected_bytes
}

fn trusted_model_file(
    file: &File,
    metadata: &fs::Metadata,
    expected_bytes: u64,
    shared: Option<SharedInode>,
) -> bool {
    if !trusted_model_shape(metadata, expected_bytes, shared) {
        return false;
    }
    match metadata.mode() & 0o777 {
        0o600 => true,
        0o640 => exact_runtime_file_acl(file),
        _ => false,
    }
}

pub(crate) fn exact_runtime_file_acl(file: &impl std::os::fd::AsFd) -> bool {
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
    shared: Option<SharedInode>,
) -> Result<(File, fs::Metadata), OciError> {
    let path_metadata = fs::symlink_metadata(path)?;
    if !trusted_model_shape(&path_metadata, expected_bytes, shared) {
        return Err(OciError::Artifact);
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(path)?;
    let opened_metadata = file.metadata()?;
    if !trusted_model_file(&file, &opened_metadata, expected_bytes, shared)
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

/// Nanoseconds since the epoch, saturating at the i64 range the receipt's
/// contract allows (year 2262).
fn timestamp_ns(seconds: i64, nanoseconds: i64) -> i64 {
    seconds
        .saturating_mul(1_000_000_000)
        .saturating_add(nanoseconds)
}

/// The order a receipt lists its entries in: by selection, then path, then the
/// remaining identity fields.
fn sort_metadata_entries(entries: &mut [InstallationMetadataEntry]) {
    entries.sort_by(|left, right| {
        let key = |entry: &InstallationMetadataEntry| {
            (
                entry.selection_id.clone(),
                entry.path.clone(),
                entry.sha256.clone(),
                entry.size_bytes,
                entry.dev,
                entry.ino,
                entry.mtime_ns,
                entry.ctime_ns,
            )
        };
        key(left).cmp(&key(right))
    });
}

fn materialize_compiled_models(
    data_root: &Path,
    plan: &CompiledExecutionPlan,
    installation_id: &str,
) -> Result<Vec<PathBuf>, OciError> {
    materialize_compiled_models_observed(data_root, plan, installation_id, &mut |_, _| {})
}

/// Bytes between two progress reports while one model file is copied.
const MATERIALIZE_PROGRESS_STEP: u64 = 64 * 1024 * 1024;

/// Put every planned model file in the installation, reporting `(done, total)`
/// bytes. A file already in place counts as done at once.
///
/// The shared distribution object is the one trusted copy of a model file, so
/// a new installation hard-links it: no bytes move and no extra disk is used,
/// however large the model or however many installations already hold it. An
/// installation that already holds a private copy, from before files were
/// shared, keeps it until the installation goes. Where a link cannot be made
/// (another filesystem, the link limit) the file is copied instead.
fn materialize_compiled_models_observed(
    data_root: &Path,
    plan: &CompiledExecutionPlan,
    installation_id: &str,
    progress: &mut dyn FnMut(u64, u64),
) -> Result<Vec<PathBuf>, OciError> {
    materialize_compiled_models_with(data_root, plan, installation_id, true, progress)
}

/// `link` is false only where a test needs the copy fallback on a filesystem
/// that would allow the link.
fn materialize_compiled_models_with(
    data_root: &Path,
    plan: &CompiledExecutionPlan,
    installation_id: &str,
    link: bool,
    progress: &mut dyn FnMut(u64, u64),
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
    let total_bytes: u64 = unique_plan_artifacts(plan)
        .iter()
        .map(|artifact| artifact.size_bytes)
        .sum();
    let mut done_bytes = 0_u64;
    progress(0, total_bytes);
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
        );
        if let Some((_, previous)) = physical_by_path.get(&physical_key) {
            if previous != &physical {
                return Err(OciError::Workload(WorkloadError::Invalid(
                    "compiled model artifact physical identity",
                )));
            }
            // The workload validator proved this is the same receipt-bound
            // physical object. Its first projection performed the only source
            // and destination verification; this projection only adds a
            // second OCI mount intent.
            continue;
        }
        if !destination.starts_with(&destination_root) {
            return Err(OciError::Artifact);
        }
        let parent = destination.parent().ok_or(OciError::Artifact)?;
        fs::create_dir_all(parent)?;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
        let source = model_root.join(&artifact.sha256);
        if !source.starts_with(&model_root) {
            return Err(OciError::Artifact);
        }
        let shared = shared_store_inode(data_root, &artifact.sha256);
        if let Ok(metadata) = fs::symlink_metadata(&destination) {
            if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
                return Err(OciError::Artifact);
            }
            let reusable = receipt_index.as_ref().and_then(|index| {
                index
                    .get(&(artifact.selection_id.clone(), artifact.path.clone()))
                    .filter(|entry| metadata_matches_receipt(&metadata, entry))
            });
            // A file that already is the shared object needs no receipt: it is
            // the one trusted inode, and nothing is left to place.
            let already_shared = shared == Some((metadata.dev(), metadata.ino()));
            if reusable.is_some() || already_shared {
                let (_, opened_metadata) =
                    open_trusted_model_file(&destination, artifact.size_bytes, shared)?;
                if already_shared
                    || reusable
                        .is_some_and(|entry| metadata_matches_receipt(&opened_metadata, entry))
                {
                    physical_by_path.insert(physical_key, (destination.clone(), physical));
                    materialized.push(destination);
                    done_bytes += artifact.size_bytes;
                    progress(done_bytes, total_bytes);
                    continue;
                }
            }
        }
        let (mut source_file, source_metadata) =
            open_trusted_model_file(&source, artifact.size_bytes, shared)?;
        let temporary = destination.with_extension(format!(
            "{}.{}.{}.partial",
            std::process::id(),
            uuid::Uuid::new_v4(),
            artifact.file_id
        ));
        let linked = if link {
            match fs::hard_link(&source, &temporary) {
                Ok(()) => true,
                Err(error) => {
                    eprintln!(
                        "vonk-agent: model.materialization_copy_fallback sha256={} bytes={} cause={error}",
                        artifact.sha256, artifact.size_bytes
                    );
                    false
                }
            }
        } else {
            false
        };
        if linked {
            let mut temporary_guard = TemporaryArtifact::new(temporary.clone());
            // The name just linked must be the object opened above, still a
            // trusted model file, before it replaces anything.
            let (_, linked) = open_trusted_model_file(&temporary, artifact.size_bytes, shared)?;
            if linked.dev() != source_metadata.dev() || linked.ino() != source_metadata.ino() {
                return Err(OciError::Artifact);
            }
            fs::rename(&temporary, &destination)?;
            temporary_guard.retain();
            sync_parent(parent)?;
            physical_by_path.insert(physical_key, (destination.clone(), physical));
            materialized.push(destination);
            done_bytes += artifact.size_bytes;
            progress(done_bytes, total_bytes);
            continue;
        }
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32,
            )
            .open(&temporary)?;
        let mut temporary_guard = TemporaryArtifact::new(temporary.clone());
        // The source is an immutable managed distribution object received
        // from the Controller over mTLS. Its bytes are not re-hashed; the
        // retained source handle, exact size and stable metadata bind this
        // copy to the object opened above.
        let mut copied = 0_u64;
        let mut reported = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let read = source_file.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            output.write_all(&buffer[..read])?;
            copied = copied.checked_add(read as u64).ok_or(OciError::Artifact)?;
            if copied > artifact.size_bytes {
                return Err(OciError::Artifact);
            }
            if copied - reported >= MATERIALIZE_PROGRESS_STEP {
                reported = copied;
                progress(done_bytes + copied, total_bytes);
            }
        }
        output.sync_all()?;
        let source_after = source_file.metadata()?;
        let output_metadata = output.metadata()?;
        if copied != artifact.size_bytes
            || !trusted_model_file(&source_file, &source_after, artifact.size_bytes, shared)
            || !metadata_stable(&source_metadata, &source_after)
            || !trusted_model_file(&output, &output_metadata, artifact.size_bytes, None)
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
        // read filled the same cache with the source object.
        // Neither is needed once this artifact is complete, and the workload
        // this installation exists for needs the memory more.
        release_page_cache(&destination)?;
        release_page_cache(&source)?;
        physical_by_path.insert(physical_key, (destination.clone(), physical));
        materialized.push(destination);
        done_bytes += artifact.size_bytes;
        progress(done_bytes, total_bytes);
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

/// Bytes of the plan's model files the shared store already holds in full, so
/// an installation links them instead of writing them. An estimate for the
/// capacity check only; the install itself verifies every object it uses.
fn linkable_model_bytes(data_root: &Path, plan: &CompiledExecutionPlan) -> u64 {
    unique_plan_artifacts(plan)
        .into_iter()
        .filter(|artifact| {
            lower_hex(&artifact.sha256, 64)
                && fs::symlink_metadata(store_object_path(data_root, &artifact.sha256)).is_ok_and(
                    |metadata| {
                        metadata.file_type().is_file() && metadata.len() == artifact.size_bytes
                    },
                )
        })
        .map(|artifact| artifact.size_bytes)
        .fold(0_u64, u64::saturating_add)
}

/// Sum of the lengths of the regular files below `directory`.
fn tree_bytes(directory: &Path) -> Result<u64, OciError> {
    let mut total = 0_u64;
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let size = if file_type.is_dir() {
            tree_bytes(&entry.path())?
        } else if file_type.is_file() {
            entry.metadata()?.len()
        } else {
            return Err(OciError::Artifact);
        };
        total = total.checked_add(size).ok_or(OciError::Artifact)?;
    }
    Ok(total)
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
        InstallationReconciliationState, OciError, OciRuntime, ensure_runtime_tmp,
        materialize_compiled_models, materialize_compiled_models_observed,
        materialize_compiled_models_with, read_installation_metadata,
        read_reconciliation_directory_identity, reconciliation_checkpoint_path,
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
    ) -> vonk_agent_protocol::RecipeReconciliationIdentity {
        vonk_agent_protocol::RecipeReconciliationIdentity {
            installation_id,
            plan_digest: "c".repeat(64),
        }
    }

    fn reconciliation_installation(
        data_root: &Path,
        installation_id: Uuid,
    ) -> (PathBuf, vonk_agent_protocol::RecipeReconciliationIdentity) {
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
        (installation, reconciliation_identity(installation_id))
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
    fn reconciliation_removing_checkpoint_recovers_after_partial_or_complete_quarantine_deletion() {
        for delete_quarantine_before_retry in [false, true] {
            let directory = tempdir().unwrap();
            let data_root = directory.path().join("data");
            fs::create_dir_all(&data_root).unwrap();
            let installation_id = Uuid::new_v4();
            let (installation, identity) = reconciliation_installation(&data_root, installation_id);
            let runtime = OciRuntime {
                runner: &NoProcess,
                data_root: &data_root,
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
            let completed = runtime.finalize_reconciliation(&identity).unwrap();
            assert!(completed.complete);
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
        let identity = reconciliation_identity(missing_id);
        let runtime = OciRuntime {
            runner: &NoProcess,
            data_root: &data_root,
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
        let primary = digest(b"primary");
        let secondary = digest(b"secondary");
        json!({
            "identity": {
                "recipe_revision_sha256": "a".repeat(64),
                "model_artifact_set_sha256": "d".repeat(64)
            },
            "runtime": {
                "executable": "/opt/vonk/bin/vllm",
                "argv": ["serve", "/models"],
                "env": [],
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
                    "memory_floor_bytes": 0
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
                    "mount": {"target": "/models"},
                    "model": {"publisher": "vonk-forge", "slug": "primary-model", "content_sha256": "e".repeat(64)}
                },
                {
                    "selection_id": "secondary",
                    "file_id": "config-secondary",
                    "path": "config.json",
                    "sha256": secondary,
                    "size_bytes": 9,
                    "roles": ["entrypoint"],
                    "mount": {"target": "/models/secondary"},
                    "model": {"publisher": "vonk-forge", "slug": "secondary-model", "content_sha256": "f".repeat(64)}
                }
            ],
            "runtime_image": {
                "image_digest": format!("sha256:{}", "1".repeat(64)),
                "local_image_config_id": format!("sha256:{}", "4".repeat(64)),
                "runtime_interface_label": "v1",
                "oci_layout_sha256": "2".repeat(64),
                "image_bytes": 4096,
                "build_id": "build-1"
            },
            "security": {
                "gpu": false,
                "network_mode": "none",
                "user": "10001:10001",
                "mounts": [
                    {"source": "model", "target": "/models"},
                    {"source": "outputs", "target": "/outputs"}
                ]
            },
            "topology": {"name": "solo", "node_count": 1},
            "lifecycle": {"stop_timeout_seconds": 30},
            "endpoint": {
                "port": 8000,
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
        write_installation_metadata(data, &installation, &plan).unwrap();
        (installation_id, installation, plan)
    }

    fn runtime<'a>(data: &'a Path, runner: &'a NoProcess) -> OciRuntime<'a, NoProcess> {
        OciRuntime {
            runner,
            data_root: data,
        }
    }

    fn authorize_installation(installation: &Path, recipe_digest: &str) {
        fs::write(installation.join("recipe-content.sha256"), recipe_digest).unwrap();
    }

    #[test]
    fn service_start_persists_its_run_generation_for_observation() {
        let data = tempdir().unwrap();
        let (installation_id, installation, plan) = persisted_installation(data.path());
        authorize_installation(&installation, &"9".repeat(64));
        let run_id = Uuid::new_v4().to_string();
        let placement = plan.runtime.placement.clone();
        let identity = super::RecipeRunStartIdentity { run_generation: 7 };
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
        assert_eq!(lifecycle["run_generation"], 7);
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
        // not request a second cleanup after a workload has run.
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
        restarted_agent
            .prepare_retained_start_if_present(
                &plan,
                &installation_id,
                &run_id,
                &plan.runtime.placement,
                None,
            )
            .unwrap()
            .unwrap();
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

        runtime.verify_installation(&installation_id).unwrap();
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

        let runner = NoProcess;
        {
            let first_runtime = runtime(data.path(), &runner);
            // The first attempt really copies the verified distribution objects
            // and persists the installation receipt. Its acknowledgement is lost.
            // The host's free space is irrelevant to this setup step.
            first_runtime
                .install_unlocked(&plan, &installation_id, &recipe_digest, &mut |_, _| {})
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
        runtime.verify_installation(&installation_id).unwrap();

        std::thread::sleep(Duration::from_millis(2));
        fs::write(installation.join("models/primary/config.json"), b"primary").unwrap();
        runtime.verify_installation(&installation_id).unwrap();
        runtime.verify_installation(&installation_id).unwrap();
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

        runtime.verify_installation(&installation_id).unwrap();
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn authorized_runtime_acl_transition_preserves_receipt_without_model_rehash() {
        let data = tempdir().unwrap();
        let (installation_id, installation, _) = persisted_installation(data.path());
        let primary = installation.join("models/primary/config.json");
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        runtime.verify_installation(&installation_id).unwrap();

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
            .finish_installation_acl_transition(&installation_id, &transition)
            .unwrap();
        runtime.verify_installation(&installation_id).unwrap();
        runtime.verify_installation(&installation_id).unwrap();
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
                .finish_installation_acl_transition(&installation_id, &transition)
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
    fn installation_verification_does_not_rehash_same_size_content() {
        let data = tempdir().unwrap();
        let (installation_id, installation, _) = persisted_installation(data.path());
        let primary = installation.join("models/primary/config.json");
        fs::write(&primary, b"mutated").unwrap();
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);

        // Bytes were placed by the agent from the Controller; size and
        // custody are the check, so a same-size edit is not re-hashed.
        runtime.verify_installation(&installation_id).unwrap();
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

        runtime.verify_installation(&installation_id).unwrap();
        runtime.verify_installation(&installation_id).unwrap();
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
    fn model_materialization_reports_bytes_while_a_large_file_is_copied() {
        // A first install copies hundreds of gigabytes. Its progress must move
        // inside a file, not only between files, or the operation shows no
        // bytes for minutes at a time.
        const LARGE: u64 = 70 * 1024 * 1024;
        let mut value = compiled_plan();
        value["artifacts"][0]["size_bytes"] = json!(LARGE);
        let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let data = tempdir().unwrap();
        let root = data.path().join("distribution").join("models");
        fs::create_dir_all(&root).unwrap();
        let large = root.join(&plan.artifacts[0].sha256);
        fs::File::create(&large).unwrap().set_len(LARGE).unwrap();
        fs::set_permissions(&large, fs::Permissions::from_mode(0o600)).unwrap();
        let small = root.join(&plan.artifacts[1].sha256);
        fs::write(&small, b"secondary").unwrap();
        fs::set_permissions(&small, fs::Permissions::from_mode(0o600)).unwrap();
        let total = LARGE + plan.artifacts[1].size_bytes;

        let mut reports = Vec::new();
        materialize_compiled_models_with(
            data.path(),
            &plan,
            "cb555393-764b-4eb6-8f15-b416d289428f",
            false,
            &mut |done, of| reports.push((done, of)),
        )
        .unwrap();

        assert!(reports.iter().all(|(_, of)| *of == total));
        assert!(reports.windows(2).all(|pair| pair[0].0 <= pair[1].0));
        assert_eq!(reports.first(), Some(&(0, total)));
        assert_eq!(reports.last(), Some(&(total, total)));
        assert!(
            reports.iter().any(|(done, _)| *done > 0 && *done < LARGE),
            "no progress inside the large file: {reports:?}"
        );
    }

    #[test]
    fn compiled_models_materialize_valid_empty_support_files() {
        let mut value = compiled_plan();
        let artifact = &mut value["artifacts"][0];
        artifact["selection_id"] = json!("primary");
        artifact["file_id"] = json!("tokenizer-config");
        artifact["path"] = json!("tokenizer_config.json");
        artifact["sha256"] = json!(crate::workloads::EMPTY_SHA256);
        artifact["size_bytes"] = json!(0);
        artifact["roles"] = json!(["tokenizer"]);
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
        write_installation_metadata(data.path(), &installation, &plan).unwrap();
        let repeated =
            materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
                .unwrap();
        assert_eq!(repeated.len(), 1);
        assert_eq!(plan.artifacts.len(), 2);
    }

    /// Put the plan's two model files in the shared store, as distribution does.
    fn stock_store(data: &Path, plan: &crate::workloads::CompiledExecutionPlan) -> Vec<PathBuf> {
        let root = data.join("distribution/models");
        fs::create_dir_all(&root).unwrap();
        [
            (b"primary".as_slice(), &plan.artifacts[0].sha256),
            (b"secondary".as_slice(), &plan.artifacts[1].sha256),
        ]
        .into_iter()
        .map(|(bytes, digest)| {
            let path = root.join(digest);
            fs::write(&path, bytes).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
            path
        })
        .collect()
    }

    const FIRST: &str = "cb555393-764b-4eb6-8f15-b416d289428f";
    const SECOND: &str = "cb555393-764b-4eb6-8f15-b416d2894290";

    fn linked_installation(
        data: &Path,
        plan: &crate::workloads::CompiledExecutionPlan,
        installation_id: &str,
    ) -> PathBuf {
        let installation = data.join("installations").join(installation_id);
        let mut reports = Vec::new();
        materialize_compiled_models_observed(data, plan, installation_id, &mut |done, of| {
            reports.push((done, of))
        })
        .unwrap();
        // Linking moves no bytes, but the progress still ends complete.
        assert_eq!(reports.last().map(|(done, of)| done == of), Some(true));
        write_installation_metadata(data, &installation, plan).unwrap();
        fs::write(
            installation.join("spec.json"),
            serde_json::to_vec(plan).unwrap(),
        )
        .unwrap();
        installation
    }

    #[test]
    fn a_new_installation_links_the_shared_model_files_instead_of_copying_them() {
        let data = tempdir().unwrap();
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        let store = stock_store(data.path(), &plan);

        let first = linked_installation(data.path(), &plan, FIRST);
        let second = linked_installation(data.path(), &plan, SECOND);

        for (artifact, object) in plan.artifacts.iter().zip(&store) {
            let object = fs::metadata(object).unwrap();
            for installation in [&first, &second] {
                let file = fs::metadata(
                    installation
                        .join("models")
                        .join(&artifact.selection_id)
                        .join(&artifact.path),
                )
                .unwrap();
                assert_eq!((file.dev(), file.ino()), (object.dev(), object.ino()));
            }
            // The store and both installations: one inode, three names, one
            // set of bytes on disk.
            assert_eq!(object.nlink(), 3);
        }
        assert_eq!(
            fs::read(first.join("models/primary/config.json")).unwrap(),
            b"primary"
        );

        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        runtime.verify_installation(FIRST).unwrap();
        runtime.verify_installation(SECOND).unwrap();
        // The tree still measures at its full logical size.
        assert!(runtime.installed_bytes(FIRST).unwrap() > 16);
    }

    #[test]
    fn a_sibling_installation_changing_the_link_count_does_not_stale_the_others_custody() {
        let data = tempdir().unwrap();
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        stock_store(data.path(), &plan);
        linked_installation(data.path(), &plan, FIRST);
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        runtime.verify_installation(FIRST).unwrap();

        // Linking a second installation and removing it again both change the
        // shared inode's change time.
        std::thread::sleep(Duration::from_millis(2));
        let second = linked_installation(data.path(), &plan, SECOND);
        fs::remove_dir_all(second).unwrap();

        let transition = runtime.begin_installation_acl_transition(FIRST).unwrap();
        runtime
            .finish_installation_acl_transition(FIRST, &transition)
            .unwrap();
        runtime.verify_installation(FIRST).unwrap();
    }

    #[test]
    fn a_model_file_linked_to_anything_but_the_store_object_is_refused() {
        let data = tempdir().unwrap();
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        stock_store(data.path(), &plan);
        let installation = linked_installation(data.path(), &plan, FIRST);
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        runtime.verify_installation(FIRST).unwrap();

        // A second, foreign name for the same inode does not change that it is
        // the store object, but a different inode with two names is not one.
        let foreign = installation.join("models/primary/config.json");
        fs::remove_file(&foreign).unwrap();
        let other = data.path().join("other-secret");
        fs::write(&other, b"primary").unwrap();
        fs::set_permissions(&other, fs::Permissions::from_mode(0o600)).unwrap();
        fs::hard_link(&other, &foreign).unwrap();
        assert!(matches!(
            runtime.verify_installation(FIRST),
            Err(OciError::Artifact)
        ));
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn an_object_a_workload_already_runs_from_still_links_into_a_new_installation() {
        let data = tempdir().unwrap();
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        let store = stock_store(data.path(), &plan);
        linked_installation(data.path(), &plan, FIRST);
        // The helper grants the runtime user read access when the first
        // installation starts; the shared inode carries it for every name.
        apply_acl(
            &store[0],
            &[
                (0x0001, 0o6, u32::MAX),
                (0x0002, 0o4, 10_001),
                (0x0004, 0, u32::MAX),
                (0x0010, 0o4, u32::MAX),
                (0x0020, 0, u32::MAX),
            ],
        );
        assert_eq!(fs::metadata(&store[0]).unwrap().mode() & 0o777, 0o640);

        linked_installation(data.path(), &plan, SECOND);
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        runtime.verify_installation(FIRST).unwrap();
        runtime.verify_installation(SECOND).unwrap();
    }

    #[test]
    fn an_installation_keeps_a_private_copy_it_already_holds() {
        let data = tempdir().unwrap();
        let (installation_id, installation, plan) = persisted_installation(data.path());
        stock_store(data.path(), &plan);
        let before = fs::metadata(installation.join("models/primary/config.json")).unwrap();

        materialize_compiled_models(data.path(), &plan, &installation_id).unwrap();

        let after = fs::metadata(installation.join("models/primary/config.json")).unwrap();
        assert_eq!((after.dev(), after.ino()), (before.dev(), before.ino()));
        assert_eq!(after.nlink(), 1);
        let runner = NoProcess;
        runtime(data.path(), &runner)
            .verify_installation(&installation_id)
            .unwrap();
    }

    #[test]
    fn a_store_object_that_is_not_private_owner_only_is_never_linked() {
        let data = tempdir().unwrap();
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        let store = stock_store(data.path(), &plan);
        fs::set_permissions(&store[0], fs::Permissions::from_mode(0o644)).unwrap();

        assert!(matches!(
            materialize_compiled_models(data.path(), &plan, FIRST),
            Err(OciError::Artifact)
        ));
        assert!(
            !data
                .path()
                .join("installations")
                .join(FIRST)
                .join("models/primary/config.json")
                .exists()
        );
    }

    #[test]
    fn model_materialization_copies_when_a_link_cannot_be_made() {
        let data = tempdir().unwrap();
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        let store = stock_store(data.path(), &plan);

        materialize_compiled_models_with(data.path(), &plan, FIRST, false, &mut |_, _| {}).unwrap();

        let copy = fs::metadata(
            data.path()
                .join("installations")
                .join(FIRST)
                .join("models/primary/config.json"),
        )
        .unwrap();
        let object = fs::metadata(&store[0]).unwrap();
        assert_ne!(copy.ino(), object.ino());
        assert_eq!((copy.nlink(), object.nlink()), (1, 1));
    }

    // This uses a hosted, owned bind mount: distribution/models is real tmpfs,
    // installations is the runner filesystem. No fake link function or link=false.
    #[cfg(target_os = "linux")]
    #[test]
    #[ignore = "requires the hosted owned cross-device model-store fixture"]
    fn real_cross_device_link_failure_logs_cause_copies_and_recovers() {
        const ROOT: &str = "VONK_OCI_CROSS_DEVICE_ROOT";
        const PHASE: &str = "VONK_OCI_CROSS_DEVICE_PHASE";
        const TEST: &str =
            "oci::tests::real_cross_device_link_failure_logs_cause_copies_and_recovers";
        let data = PathBuf::from(std::env::var_os(ROOT).expect("hosted fixture root"));
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        let model_root = data.join("distribution/models");
        assert_ne!(
            fs::metadata(&model_root).unwrap().dev(),
            fs::metadata(&data).unwrap().dev(),
            "the test must reach the real cross-device hard-link error"
        );
        if let Ok(phase) = std::env::var(PHASE) {
            if phase == "first" {
                stock_store(&data, &plan);
            }
            let runner = NoProcess;
            // A new process and runtime reopen the actual persisted installation.
            let instance = runtime(&data, &runner);
            let mut reports = Vec::new();
            instance
                .install_unlocked(
                    &plan,
                    FIRST,
                    &plan.identity.recipe_revision_sha256,
                    &mut |done, total| reports.push((done, total)),
                )
                .unwrap();
            instance.verify_installation(FIRST).unwrap();
            let total: u64 = plan
                .artifacts
                .iter()
                .map(|artifact| artifact.size_bytes)
                .sum();
            assert_eq!(reports.last(), Some(&(total, total)));
            for (artifact, bytes) in plan
                .artifacts
                .iter()
                .zip([b"primary".as_slice(), b"secondary".as_slice()])
            {
                let destination = data
                    .join("installations")
                    .join(FIRST)
                    .join("models")
                    .join(&artifact.selection_id)
                    .join(&artifact.path);
                let actual = fs::read(&destination).unwrap();
                assert_eq!(actual, bytes);
                assert_eq!(vonk_agent_protocol::hex_sha256(&actual), artifact.sha256);
                let stored = fs::metadata(model_root.join(&artifact.sha256)).unwrap();
                let copied = fs::metadata(&destination).unwrap();
                assert_ne!((copied.dev(), copied.ino()), (stored.dev(), stored.ino()));
                assert_eq!((copied.nlink(), stored.nlink()), (1, 1));
            }
            return;
        }
        let execute = |phase: &str| {
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args([TEST, "--exact", "--ignored", "--nocapture"])
                .env(PHASE, phase)
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}\n{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            String::from_utf8(output.stderr).unwrap()
        };
        let cause = std::io::Error::from_raw_os_error(18).to_string(); // Linux EXDEV.
        let assert_fallbacks =
            |stderr: &str, artifacts: &[crate::workloads::CompiledModelArtifact]| {
                let logs = stderr
                    .lines()
                    .filter(|line| line.contains("model.materialization_copy_fallback"))
                    .collect::<Vec<_>>();
                assert_eq!(logs.len(), artifacts.len(), "{stderr}");
                for artifact in artifacts {
                    let expected = format!(
                        "vonk-agent: model.materialization_copy_fallback sha256={} bytes={} cause={cause}",
                        artifact.sha256, artifact.size_bytes
                    );
                    assert!(
                        logs.contains(&expected.as_str()),
                        "missing safe cause: {stderr}"
                    );
                }
                assert!(
                    !stderr.contains(data.to_str().unwrap()),
                    "owned paths must not leak in the cause log"
                );
            };
        let first = execute("first");
        assert_fallbacks(&first, &plan.artifacts);
        let model = data
            .join("installations")
            .join(FIRST)
            .join("models/primary/config.json");
        let before = fs::metadata(&model).unwrap();
        let reused = execute("reuse");
        assert_fallbacks(&reused, &[]);
        let after = fs::metadata(&model).unwrap();
        assert_eq!(
            (before.dev(), before.ino(), before.ctime_nsec()),
            (after.dev(), after.ino(), after.ctime_nsec())
        );
        // A stale private copy is recovered through the same normal materializer.
        // Same-size damage prevents a length-only check from accidentally passing.
        fs::write(&model, b"damaged").unwrap();
        let recovered = execute("recover");
        assert_fallbacks(&recovered, &plan.artifacts[..1]);
        assert_eq!(fs::read(&model).unwrap(), b"primary");
        assert_ne!(fs::metadata(&model).unwrap().ino(), after.ino());
        let installation = data.join("installations").join(FIRST);
        assert!(
            installation
                .join(super::INSTALLATION_METADATA_FILE)
                .is_file()
        );
        assert!(
            !fs::read_dir(model.parent().unwrap())
                .unwrap()
                .any(|entry| entry
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .ends_with(".partial"))
        );
    }

    #[test]
    fn linkable_bytes_count_only_complete_store_objects() {
        let data = tempdir().unwrap();
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        assert_eq!(super::linkable_model_bytes(data.path(), &plan), 0);
        let store = stock_store(data.path(), &plan);
        assert_eq!(super::linkable_model_bytes(data.path(), &plan), 7 + 9);
        fs::write(&store[1], b"short").unwrap();
        assert_eq!(super::linkable_model_bytes(data.path(), &plan), 7);
    }

    #[test]
    fn installed_bytes_sum_file_lengths_and_refuse_anything_but_files_and_directories() {
        let data = tempdir().unwrap();
        let (installation_id, installation, _) = persisted_installation(data.path());
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        let expected: u64 = [
            "spec.json",
            "models/primary/config.json",
            "models/secondary/config.json",
            "model-metadata.json",
        ]
        .iter()
        .map(|name| fs::metadata(installation.join(name)).unwrap().len())
        .sum();
        assert_eq!(runtime.installed_bytes(&installation_id).unwrap(), expected);

        symlink(installation.join("spec.json"), installation.join("link")).unwrap();
        assert!(matches!(
            runtime.installed_bytes(&installation_id),
            Err(OciError::Artifact)
        ));
    }
}
