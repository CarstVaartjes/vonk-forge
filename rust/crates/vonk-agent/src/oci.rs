use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::IpAddr,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::{
    RecipeRunInspectionBinding, canonical_json as canonical_protocol_json,
    hex_sha256 as protocol_sha256,
};

use crate::{
    compiled_oci::{CompiledOciPaths, project},
    health::readiness_endpoint,
    inventory::{available_disk_bytes, available_memory_bytes},
    process::{ProcessError, ProcessRunner, Program},
    workloads::{CompiledExecutionPlan, Placement, WorkloadError, managed_path},
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
}

pub struct OciRuntime<'a, R> {
    pub runner: &'a R,
    pub data_root: &'a Path,
    pub huggingface_curl_config: Option<&'a Path>,
}

pub const MAX_MANAGED_RECIPE_RUNS: usize = 64;
const MAX_RUN_DIRECTORY_ENTRIES: usize = 4096;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeRunObservation {
    pub run_id: String,
    pub ready: bool,
}

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

struct RecipeRunProbe {
    run_id: String,
    address: Option<IpAddr>,
    port: u16,
    health_path: String,
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
        self.verify_image(spec)?;
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        fs::create_dir_all(&installation)?;
        fs::set_permissions(&installation, fs::Permissions::from_mode(0o700))?;
        self.ensure_runtime_cache(installation_id)?;
        let distribution_root = self
            .data_root
            .join("distribution")
            .join(&spec.identity.execution_sha256);
        self.materialize_compiled_models(spec, &distribution_root, installation_id)?;
        self.verify_compiled_image_archive(spec)?;
        atomic_write(&installation, "spec.json", &serde_json::to_vec(spec)?)?;
        atomic_write(
            &installation,
            "recipe-content.sha256",
            recipe_content_sha256.as_bytes(),
        )?;
        File::open(&installation)?.sync_all()?;
        Ok(())
    }

    /// Materialize only the model files authorized by a compiled Controller
    /// plan. The distribution client must preserve the selection scope in its
    /// staging layout; a flat path would make colliding files such as
    /// ``config.json`` ambiguous and is rejected by this boundary.
    pub fn materialize_compiled_models(
        &self,
        plan: &CompiledExecutionPlan,
        distribution_root: &Path,
        installation_id: &str,
    ) -> Result<Vec<PathBuf>, OciError> {
        materialize_compiled_models(self.data_root, plan, distribution_root, installation_id)
    }

    pub fn verify_image(&self, spec: &CompiledExecutionPlan) -> Result<(), OciError> {
        spec.validate()?;
        let policy = runtime_policy()?;
        if spec.runtime_image.runtime_interface != policy.runtime_interface
            || spec.runtime_image.architecture != policy.architecture
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
            || !metadata.file_type().is_file()
            || metadata.len() != plan.runtime_image.image_bytes
            || sha256_file(&archive)? != plan.runtime_image.oci_layout_sha256
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
        let invocation = project(
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
        )
        .map_err(|_| OciError::Runtime)?;
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
                let local_address = placement.local_address.ok_or(OciError::Artifact)?;
                let master_address = placement.master_address.ok_or(OciError::Artifact)?;
                let master_port = placement.master_port.ok_or(OciError::Artifact)?;
                if placement.world_size <= 1
                    || identity.mapping_generation == 0
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
                    port: placement.port,
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
                || Some(binding.local_address) != placement.local_address
                || Some(binding.master_address) != placement.master_address
                || Some(binding.master_port) != placement.master_port
                || binding.port != placement.port
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
                endpoint_address: endpoint_owner
                    .then_some(placement.endpoint_address.ok_or(OciError::Artifact)?),
                endpoint_port: placement.port,
                health_path,
            });
        }
        Ok(plans)
    }

    pub fn recipe_run_observations(&self) -> Result<Vec<RecipeRunObservation>, OciError> {
        let probes = self.recipe_run_probes()?;
        probes
            .into_iter()
            .map(|probe| {
                let ready = probe.address.is_some_and(|address| {
                    self.readiness_request(address, probe.port, &probe.health_path)
                });
                Ok(RecipeRunObservation {
                    run_id: probe.run_id,
                    ready,
                })
            })
            .collect()
    }

    fn recipe_run_probes(&self) -> Result<Vec<RecipeRunProbe>, OciError> {
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

        let mut probes = Vec::with_capacity(run_ids.len());
        for run_id in run_ids {
            let lifecycle = match self.load_run_lifecycle(&run_id) {
                Ok(lifecycle) => lifecycle,
                Err(OciError::Artifact | OciError::Json(_) | OciError::Workload(_)) => continue,
                Err(error) => return Err(error),
            };
            let Some((spec, _, placement, observation)) = lifecycle else {
                continue;
            };
            if observation.is_some() {
                continue;
            }
            if placement.world_size > 1 {
                return Err(OciError::Artifact);
            }
            let Some(endpoint) = spec.endpoint.as_ref() else {
                continue;
            };
            if endpoint.health_path.contains(['?', '#', '\0'])
                || !endpoint
                    .health_path
                    .bytes()
                    .all(|byte| byte.is_ascii_graphic())
            {
                continue;
            }
            if probes.len() == MAX_MANAGED_RECIPE_RUNS {
                return Err(OciError::Artifact);
            }
            probes.push(RecipeRunProbe {
                run_id,
                address: Some(
                    placement
                        .endpoint_address
                        .unwrap_or(IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)),
                ),
                port: placement.port,
                health_path: endpoint.health_path.clone(),
            });
        }
        Ok(probes)
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
        let spec = self.load_spec(&record.installation_id)?;
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
            || metadata.len() > 64 * 1024
        {
            return Err(OciError::Artifact);
        }
        serde_json::from_slice(&read_regular_file(&path, 64 * 1024)?).map_err(OciError::Json)
    }

    pub fn verify_installation(&self, installation_id: &str) -> Result<(), OciError> {
        let (_, plan) = self.load_persisted_spec(installation_id)?;
        let models = managed_path(self.data_root, "installations", installation_id)?.join("models");
        for artifact in &plan.artifacts {
            let destination = models.join(&artifact.selection_id).join(&artifact.path);
            let metadata = fs::symlink_metadata(&destination)?;
            if metadata.file_type().is_symlink()
                || !metadata.file_type().is_file()
                || metadata.len() != artifact.size_bytes
                || sha256_file(&destination)? != artifact.sha256
            {
                return Err(OciError::Artifact);
            }
        }
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

fn sha256_file(path: &Path) -> Result<String, OciError> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hex::encode(hasher.finalize()))
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
    distribution_root: &Path,
    installation_id: &str,
) -> Result<Vec<PathBuf>, OciError> {
    if !distribution_root.is_absolute() || !data_root.is_absolute() {
        return Err(OciError::Artifact);
    }
    plan.validate()?;
    let installation = managed_path(data_root, "installations", installation_id)?;
    let destination_root = installation.join("models");
    fs::create_dir_all(&installation)?;
    fs::set_permissions(&installation, fs::Permissions::from_mode(0o700))?;
    fs::create_dir_all(&destination_root)?;
    fs::set_permissions(&destination_root, fs::Permissions::from_mode(0o700))?;

    let scoped_root = distribution_root
        .join("models")
        .join(&plan.identity.model_artifact_set_sha256);
    let mut materialized = Vec::with_capacity(plan.artifacts.len());
    for artifact in &plan.artifacts {
        let source = scoped_root
            .join(&artifact.selection_id)
            .join(&artifact.path);
        if !source.starts_with(&scoped_root) {
            return Err(OciError::Artifact);
        }
        let source_metadata = fs::symlink_metadata(&source)?;
        if !source_metadata.file_type().is_file()
            || source_metadata.file_type().is_symlink()
            || source_metadata.len() != artifact.size_bytes
            || sha256_file(&source)? != artifact.sha256
        {
            return Err(OciError::Artifact);
        }

        let destination = destination_root
            .join(&artifact.selection_id)
            .join(&artifact.path);
        if !destination.starts_with(&destination_root) {
            return Err(OciError::Artifact);
        }
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
            fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
        }
        if destination.exists() {
            let metadata = fs::symlink_metadata(&destination)?;
            if metadata.file_type().is_symlink()
                || !metadata.file_type().is_file()
                || metadata.len() != artifact.size_bytes
                || sha256_file(&destination)? != artifact.sha256
            {
                return Err(OciError::Artifact);
            }
        } else {
            let temporary = destination.with_extension(format!(
                "{}.{}.partial",
                std::process::id(),
                artifact.file_id
            ));
            if temporary.exists() {
                return Err(OciError::Artifact);
            }
            fs::copy(&source, &temporary)?;
            let metadata = fs::symlink_metadata(&temporary)?;
            if metadata.file_type().is_symlink()
                || !metadata.file_type().is_file()
                || metadata.len() != artifact.size_bytes
                || sha256_file(&temporary)? != artifact.sha256
            {
                let _ = fs::remove_file(&temporary);
                return Err(OciError::Artifact);
            }
            fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))?;
            fs::rename(&temporary, &destination)?;
        }
        materialized.push(destination);
    }
    File::open(&destination_root)?.sync_all()?;
    Ok(materialized)
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
    use super::{OciError, materialize_compiled_models};
    use serde_json::{Value, json};
    use sha2::Digest;
    use std::{fs, path::Path};
    use tempfile::tempdir;

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
                    "mount": {"target": "/models", "read_only": true},
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

    #[test]
    fn compiled_models_materialize_selection_scoped_colliding_paths() {
        let plan: crate::workloads::CompiledExecutionPlan =
            serde_json::from_value(compiled_plan()).unwrap();
        plan.validate().unwrap();
        let data = tempdir().unwrap();
        let distribution = tempdir().unwrap();
        let root = distribution
            .path()
            .join("models")
            .join(&plan.identity.model_artifact_set_sha256);
        fs::create_dir_all(root.join("primary")).unwrap();
        fs::create_dir_all(root.join("secondary")).unwrap();
        fs::write(root.join("primary/config.json"), b"primary").unwrap();
        fs::write(root.join("secondary/config.json"), b"secondary").unwrap();

        let paths = materialize_compiled_models(
            data.path(),
            &plan,
            distribution.path(),
            "cb555393-764b-4eb6-8f15-b416d289428f",
        )
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
        let distribution = tempdir().unwrap();
        let source = distribution
            .path()
            .join("models")
            .join(&plan.identity.model_artifact_set_sha256)
            .join("primary");
        fs::create_dir_all(&source).unwrap();
        fs::write(source.join("tokenizer_config.json"), &[]).unwrap();
        materialize_compiled_models(
            data.path(),
            &plan,
            distribution.path(),
            "cb555393-764b-4eb6-8f15-b416d289428f",
        )
        .unwrap();
        assert_eq!(
            fs::metadata(data.path().join("installations/cb555393-764b-4eb6-8f15-b416d289428f/models/primary/tokenizer_config.json")).unwrap().len(),
            0
        );
    }

    #[test]
    fn compiled_models_reject_duplicate_path_within_one_selection() {
        let mut value = compiled_plan();
        let duplicate = value["artifacts"][0].clone();
        value["artifacts"] = json!([duplicate.clone(), duplicate]);
        let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let result = materialize_compiled_models(
            Path::new("/tmp/vonk-agent-test-data"),
            &plan,
            Path::new("/tmp/vonk-agent-test-distribution"),
            "cb555393-764b-4eb6-8f15-b416d289428f",
        );
        assert!(matches!(result, Err(OciError::Workload(_))));
    }
}
