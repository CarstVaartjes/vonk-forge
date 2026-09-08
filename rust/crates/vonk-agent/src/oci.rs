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
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES, RecipeRunInspectionBinding,
    canonical_json as canonical_protocol_json, hex_sha256 as protocol_sha256,
};

use crate::{
    compiled_oci::{CompiledOciPaths, project},
    health::readiness_endpoint,
    inventory::{available_disk_bytes, available_memory_bytes},
    process::{ProcessError, ProcessRunner, Program},
    workloads::{
        CompiledExecutionPlan, Placement, WorkloadError, managed_path, same_installed_workload,
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
    #[error("install {stage} failed: {source}")]
    Install {
        stage: &'static str,
        #[source]
        source: Box<OciError>,
    },
}

impl OciError {
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
            Self::ImageDigest => "image-digest",
            Self::Artifact => "artifact",
            Self::Io(_) => "storage",
            Self::Json(_) => "metadata",
            Self::Capacity => "capacity",
            Self::Install { source, .. } => source.safe_category(),
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
    Placement,
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

#[derive(Debug, Deserialize, Serialize)]
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
    placement: Placement,
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
        meminfo_path: &Path,
    ) -> Result<(), OciError> {
        let required = required_bytes
            .checked_add(4_000_000_000)
            .ok_or(OciError::Capacity)?;
        if available_memory_bytes(self.runner, meminfo_path).map_err(|_| OciError::Capacity)?
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
        placement: &Placement,
    ) -> Result<Vec<String>, OciError> {
        spec.validate()?;
        placement.validate()?;
        if placement.rank != spec.runtime.placement.rank
            || placement.role != spec.runtime.placement.role
            || placement.world_size != spec.runtime.placement.world_size
            || placement.port != spec.runtime.placement.port
            || placement.reserved_memory_bytes != spec.runtime.placement.reserved_memory_bytes
        {
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
        placement: &Placement,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.prepare_start_internal(spec, installation_id, run_id, placement, None)
    }

    pub fn prepare_start_with_inspection_identity(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
        identity: &RecipeRunStartIdentity,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.prepare_start_internal(spec, installation_id, run_id, placement, Some(identity))
    }

    fn prepare_start_internal(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
        identity: Option<&RecipeRunStartIdentity>,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.verify_image(spec)?;
        let state = managed_path(self.data_root, "runs", run_id)?;
        fs::create_dir_all(&state)?;
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
        let outputs = state.join("outputs");
        fs::create_dir_all(&outputs)?;
        fs::set_permissions(&outputs, fs::Permissions::from_mode(0o700))?;
        reset_runtime_tmp(&outputs)?;
        self.ensure_runtime_cache(installation_id)?;
        if spec.job.is_some() {
            let inputs = state.join("inputs");
            let metadata = fs::symlink_metadata(&inputs)?;
            if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                return Err(OciError::Artifact);
            }
        }
        let metadata = self.ensure_run_metadata(run_id)?;
        self.write_runtime_contract(spec, installation_id, run_id, placement, None)?;
        let main = self.start_arguments(spec, installation_id, run_id, placement)?;
        let runtime_image_digest = spec.runtime_image.image_digest.clone();
        let runtime_image_reference = spec.runtime_image.local_image_reference();
        let pre_start = spec
            .lifecycle
            .pre_start
            .iter()
            .map(|hook| hook_arguments(&main, &runtime_image_reference, hook))
            .collect::<Result<Vec<_>, _>>()?;
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
                    rank: placement.rank,
                    recipe_content_sha256: identity.recipe_content_sha256.clone(),
                    recipe_revision_id: identity.recipe_revision_id,
                    role: placement.role.clone(),
                    run_id: uuid::Uuid::parse_str(run_id).map_err(|_| OciError::Artifact)?,
                    run_generation: identity.run_generation,
                    runtime_arguments_sha256: protocol_sha256(
                        &canonical_protocol_json(&arguments).map_err(|_| OciError::Artifact)?,
                    ),
                    world_size: placement.world_size,
                };
                binding.validate().map_err(|_| OciError::Artifact)?;
                Ok(binding)
            })
            .transpose()?;
        atomic_write(
            &metadata,
            "lifecycle.json",
            &serde_json::to_vec(&RunLifecycle {
                installation_id: installation_id.to_owned(),
                placement: placement.clone(),
                observation,
            })?,
        )?;
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
        placement: &Placement,
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
        // Reset writable state only in prepare_start_internal for a real start.
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

    pub fn prepare_retained_start_with_inspection_identity(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
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
            || binding.run_generation != identity.run_generation
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
        placement: &Placement,
        parameters: &serde_json::Value,
        timeout_seconds: u16,
    ) -> Result<RuntimeStartPlan, OciError> {
        let Some(installed_job) = spec.job.as_ref() else {
            return Err(OciError::Runtime);
        };
        if timeout_seconds == 0 || timeout_seconds > installed_job.timeout_seconds {
            return Err(OciError::Runtime);
        }
        let mut effective = spec.clone();
        effective
            .job
            .as_mut()
            .ok_or(OciError::Runtime)?
            .timeout_seconds = timeout_seconds;
        let plan = self.prepare_start(&effective, installation_id, run_id, placement)?;
        self.write_runtime_contract(
            &effective,
            installation_id,
            run_id,
            placement,
            Some(parameters),
        )?;
        Ok(plan)
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
        match fs::remove_file(metadata.join("lifecycle.json")) {
            Ok(()) => File::open(metadata)?.sync_all().map_err(OciError::Io),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
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
                || binding.rank != placement.rank
                || binding.role != placement.role
                || binding.world_size != placement.world_size
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

    fn load_run_lifecycle(&self, run_id: &str) -> Result<Option<LoadedRunLifecycle>, OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        let path = metadata.join("lifecycle.json");
        let Some(record) = self.read_run_lifecycle(&path)? else {
            return Ok(None);
        };
        record.placement.validate()?;
        if !canonical_uuid(&record.installation_id) {
            return Err(OciError::Artifact);
        }
        managed_path(self.data_root, "installations", &record.installation_id)?;
        let spec: CompiledExecutionPlan = serde_json::from_slice(&read_regular_file(
            &metadata.join("runtime.json"),
            MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES as u64,
        )?)?;
        spec.validate()?;
        let mut installed = self.load_spec(&record.installation_id)?;
        // A job may select a shorter timeout at start, within its installed limit.
        if let (Some(installed_job), Some(retained_job)) = (&mut installed.job, &spec.job)
            && retained_job.timeout_seconds <= installed_job.timeout_seconds
        {
            installed_job.timeout_seconds = retained_job.timeout_seconds;
        }
        let placement = &spec.runtime.placement;
        if !same_installed_workload(&installed, &spec)
            || placement.endpoint_address != record.placement.endpoint_address
            || placement.rank != record.placement.rank
            || placement.role != record.placement.role
            || placement.world_size != record.placement.world_size
            || placement.local_address != record.placement.local_address
            || placement.master_address != record.placement.master_address
            || placement.master_port != record.placement.master_port
            || placement.port != record.placement.port
            || placement.reserved_memory_bytes != record.placement.reserved_memory_bytes
        {
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
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let metadata = fs::symlink_metadata(&installation)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        self.load_spec(installation_id)?;
        if self.recipe_digest(installation_id)? != expected_recipe_digest {
            return Err(OciError::Artifact);
        }
        fs::remove_dir_all(installation)?;
        File::open(self.data_root.join("installations"))?.sync_all()?;
        Ok(())
    }

    /// Remove one installation's materialized model files when the signed
    /// Controller plan proves that this node is the last consumer of the
    /// model. The global distribution cache is reusable shared state and is
    /// retained for future installs.
    pub fn uninstall_with_model_cleanup(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
        model_content_sha256: &str,
    ) -> Result<u64, OciError> {
        if !lower_hex(model_content_sha256, 64) {
            return Err(OciError::Artifact);
        }
        let (_, persisted) = self.load_persisted_spec(installation_id)?;
        if self.recipe_digest(installation_id)? != expected_recipe_digest
            || !spec_references_model(&persisted, model_content_sha256)
        {
            return Err(OciError::Artifact);
        }
        let remaining = self.installed_specs_except(&[installation_id])?;
        if remaining
            .iter()
            .any(|(_, spec)| spec_references_model(spec, model_content_sha256))
        {
            return Err(OciError::Artifact);
        }
        let removed_model_bytes =
            materialized_model_bytes(self.data_root, installation_id, &persisted)?;
        self.uninstall(installation_id, expected_recipe_digest)?;
        Ok(removed_model_bytes)
    }

    /// Explicit model cleanup is a Controller-authorized cascade.  Every
    /// installation identity and recipe digest is checked before any storage
    /// mutation, and shared model objects remain when another installation
    /// still references the same physical object. The global distribution
    /// cache is retained; this operation only removes installation state.
    pub fn uninstall_model(
        &self,
        installations: &[(String, String)],
        model_content_sha256: &str,
    ) -> Result<u64, OciError> {
        if installations.is_empty() || !lower_hex(model_content_sha256, 64) {
            return Err(OciError::Artifact);
        }
        let target_ids = installations
            .iter()
            .map(|(installation_id, _)| installation_id.as_str())
            .collect::<BTreeSet<_>>();
        if target_ids.len() != installations.len() {
            return Err(OciError::Artifact);
        }
        let mut removed_model_bytes = 0_u64;
        for (installation_id, expected_recipe_digest) in installations {
            let (_, persisted) = self.load_persisted_spec(installation_id)?;
            if self.recipe_digest(installation_id)? != *expected_recipe_digest
                || !spec_references_model(&persisted, model_content_sha256)
            {
                return Err(OciError::Artifact);
            }
            removed_model_bytes = removed_model_bytes
                .checked_add(materialized_model_bytes(
                    self.data_root,
                    installation_id,
                    &persisted,
                )?)
                .ok_or(OciError::Artifact)?;
        }
        let excluded = target_ids.iter().copied().collect::<Vec<_>>();
        let remaining = self.installed_specs_except(&excluded)?;
        if remaining
            .iter()
            .any(|(_, spec)| spec_references_model(spec, model_content_sha256))
        {
            return Err(OciError::Artifact);
        }
        for (installation_id, expected_recipe_digest) in installations {
            self.uninstall(installation_id, expected_recipe_digest)?;
        }
        Ok(removed_model_bytes)
    }

    fn installed_specs_except(
        &self,
        excluded: &[&str],
    ) -> Result<Vec<(String, CompiledExecutionPlan)>, OciError> {
        let root = self.data_root.join("installations");
        let metadata = fs::symlink_metadata(&root)?;
        if metadata.file_type().is_symlink() || !metadata.file_type().is_dir() {
            return Err(OciError::Artifact);
        }
        let excluded = excluded.iter().copied().collect::<BTreeSet<_>>();
        let mut entries = fs::read_dir(&root)?.collect::<Result<Vec<_>, _>>()?;
        if entries.len() > MAX_RUN_DIRECTORY_ENTRIES {
            return Err(OciError::Artifact);
        }
        entries.sort_by_key(fs::DirEntry::file_name);
        let mut result = Vec::with_capacity(entries.len());
        for entry in entries {
            let installation_id = entry
                .file_name()
                .into_string()
                .map_err(|_| OciError::Artifact)?;
            if excluded.contains(installation_id.as_str()) {
                continue;
            }
            let file_type = entry.file_type()?;
            if !file_type.is_dir() || file_type.is_symlink() {
                return Err(OciError::Artifact);
            }
            result.push((installation_id.clone(), self.load_spec(&installation_id)?));
        }
        Ok(result)
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
        _installation_id: &str,
        _run_id: &str,
        _placement: &Placement,
        _parameters: Option<&serde_json::Value>,
    ) -> Result<(), OciError> {
        let metadata = self.run_metadata_path(_run_id)?;
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
    for entry in value[4..].chunks_exact(8) {
        let tag = u16::from_le_bytes(entry[..2].try_into().unwrap());
        let permissions = u16::from_le_bytes(entry[2..4].try_into().unwrap());
        let identifier = u32::from_le_bytes(entry[4..8].try_into().unwrap());
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
            artifact.distribution_object.kind.clone(),
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
        if sha256_open_file(&mut source_file, &source_metadata)? != artifact.sha256 {
            return Err(OciError::Artifact);
        }
        source_file.seek(SeekFrom::Start(0))?;
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
        let copied = std::io::copy(&mut source_file, &mut output)?;
        output.sync_all()?;
        let source_after = source_file.metadata()?;
        let output_metadata = output.metadata()?;
        if copied != artifact.size_bytes
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

/// Reset the per-run temporary tree before handing the writable output mount
/// to the container. The helper grants the compiled runtime UID access to this
/// tree, while the agent owns its lifecycle and ensures stale temporary files
/// cannot survive a retained or retried start. The persistent cache remains a
/// separate bind mount and is deliberately untouched.
fn reset_runtime_tmp(outputs: &Path) -> Result<(), OciError> {
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
            fs::remove_dir_all(&temporary)?;
            fs::create_dir(&temporary)?;
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
        OciError, OciRuntime, SHA256_OPEN_FILE_CALLS, materialize_compiled_models,
        read_installation_metadata, reset_runtime_tmp, unique_plan_artifacts,
        write_installation_metadata,
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

    fn digest(value: &[u8]) -> String {
        hex::encode(sha2::Sha256::digest(value))
    }

    fn compiled_plan() -> Value {
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
                    "reserved_memory_bytes": 4096
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
        let placement: crate::workloads::Placement =
            serde_json::from_value(serde_json::to_value(&plan.runtime.placement).unwrap()).unwrap();
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
    fn explicit_model_cleanup_removes_materialized_install_and_retains_shared_cache() {
        let data = tempdir().unwrap();
        let (installation_id, installation, plan) = persisted_installation(data.path());
        let recipe_digest = "1".repeat(64);
        authorize_installation(&installation, &recipe_digest);

        let cached = data.path().join("distribution").join("models");
        fs::create_dir_all(&cached).unwrap();
        fs::write(cached.join(&plan.artifacts[0].sha256), b"primary").unwrap();
        fs::write(cached.join(&plan.artifacts[1].sha256), b"secondary").unwrap();

        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        let removed = runtime
            .uninstall_model(&[(installation_id.clone(), recipe_digest)], &"e".repeat(64))
            .unwrap();

        assert_eq!(removed, 16);
        assert!(!installation.exists());
        assert_eq!(
            fs::read(cached.join(&plan.artifacts[0].sha256)).unwrap(),
            b"primary"
        );
        assert_eq!(
            fs::read(cached.join(&plan.artifacts[1].sha256)).unwrap(),
            b"secondary"
        );
    }

    #[test]
    fn explicit_auxiliary_model_cleanup_removes_selected_install_and_retains_shared_other_install()
    {
        let data = tempdir().unwrap();
        let (installation_id, installation, plan) = persisted_installation(data.path());
        let recipe_digest = "3".repeat(64);
        authorize_installation(&installation, &recipe_digest);

        let mut other_value = compiled_plan();
        other_value["identity"]["model_artifact_bytes"] = json!(7);
        other_value["artifacts"] = json!([other_value["artifacts"][0].clone()]);
        let other_plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(other_value).unwrap();
        let other_id = "cb555393-764b-4eb6-8f15-b416d2894290".to_owned();
        let (_, other_installation, _) =
            persisted_plan_installation(data.path(), other_id.clone(), other_plan);
        authorize_installation(&other_installation, &"4".repeat(64));

        let cached = data.path().join("distribution").join("models");
        fs::create_dir_all(&cached).unwrap();
        fs::write(cached.join(&plan.artifacts[0].sha256), b"primary").unwrap();
        fs::write(cached.join(&plan.artifacts[1].sha256), b"secondary").unwrap();

        let runner = NoProcess;
        let removed = runtime(data.path(), &runner)
            .uninstall_model(&[(installation_id, recipe_digest)], &"f".repeat(64))
            .unwrap();

        assert_eq!(removed, 16);
        assert!(!installation.exists());
        assert!(other_installation.exists());
        assert_eq!(
            fs::read(cached.join(&plan.artifacts[0].sha256)).unwrap(),
            b"primary"
        );
        assert_eq!(
            fs::read(cached.join(&plan.artifacts[1].sha256)).unwrap(),
            b"secondary"
        );
    }

    #[test]
    fn routine_recipe_uninstall_retains_shared_model_cache() {
        let data = tempdir().unwrap();
        let (installation_id, installation, plan) = persisted_installation(data.path());
        let recipe_digest = "2".repeat(64);
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
    fn trusted_installation_verification_reuses_exact_runtime_acl_receipt_after_metadata_refresh() {
        let data = tempdir().unwrap();
        let (installation_id, installation, _) = persisted_installation(data.path());
        let primary = installation.join("models/primary/config.json");
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        let before = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        runtime.verify_installation(&installation_id).unwrap();
        assert_eq!(SHA256_OPEN_FILE_CALLS.with(|calls| calls.get()), before);

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
        runtime.verify_installation(&installation_id).unwrap();
        let after_acl = SHA256_OPEN_FILE_CALLS.with(|calls| calls.get());
        assert_eq!(after_acl, before + 1);

        runtime.verify_installation(&installation_id).unwrap();
        assert_eq!(SHA256_OPEN_FILE_CALLS.with(|calls| calls.get()), after_acl);
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
    fn runtime_tmp_is_reset_and_kept_private_between_starts() {
        let data = tempdir().unwrap();
        let outputs = data.path().join("outputs");
        fs::create_dir_all(outputs.join("tmp")).unwrap();
        fs::write(outputs.join("tmp").join("stale.marker"), b"stale").unwrap();

        reset_runtime_tmp(&outputs).unwrap();

        let temporary = outputs.join("tmp");
        assert!(!temporary.join("stale.marker").exists());
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
            reset_runtime_tmp(&outputs),
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
