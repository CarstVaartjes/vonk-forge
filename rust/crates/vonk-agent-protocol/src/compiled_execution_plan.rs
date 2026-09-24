//! Semantic validation of the generated canonical compiled execution plan.
use std::{collections::BTreeSet, path::Path};
use thiserror::Error;

pub use crate::generated::{
    CompiledArtifact as CompiledModelArtifact, CompiledArtifactMount, CompiledEndpoint,
    CompiledEnvironmentEntry, CompiledExecutionPlan, CompiledIdentity as CompiledWorkloadIdentity,
    CompiledJob, CompiledJobInput, CompiledJobInputSlot, CompiledLifecycle,
    CompiledModelIdentity as ModelArtifactIdentity, CompiledPlacement as CompiledRuntimePlacement,
    CompiledRuntime, CompiledRuntimeImage, CompiledRuntimeTelemetry, CompiledSecurity,
    CompiledSecurityMount as MountSpec, CompiledTopology,
};

#[derive(Debug, Error)]
pub enum WorkloadError {
    #[error("workload specification is invalid: {0}")]
    Invalid(&'static str),
}

pub const EMPTY_SHA256: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

// These bounds carry the compiler's structured argv without treating engine
// arguments as container-engine options.  Keep the first item non-empty (it
// is the executable), while subsequent items are opaque values and may be
// empty.  The total bound prevents a large number of individually valid
// values from creating an unbounded launch request.
/// The element ceiling for one compiled argv vector.
///
/// The generated wire schema declares this vector's `maxItems` from the Python
/// `MAX_ARGV_ITEMS`, so this is a structural mirror of the schema, not the size
/// authority. The size authority is `MAX_ARGV_BYTES`, derived below from the
/// ceiling on the whole host-runtime request.
const MAX_ARGV_ITEMS: usize = 4096;
const MAX_ARGV_ITEM_BYTES: usize = 65_536;
/// The byte ceiling on one compiled argv vector, derived from the ceiling on
/// the whole [`HostRuntimeRequest`](crate::MAX_HOST_RUNTIME_REQUEST_BYTES).
///
/// The request the agent sends is this argv plus the four image identities, the
/// `podman run` options, one `--mount` pair per mounted file and the recipe
/// environment, inside one bounded helper exchange. An argv budget equal to the
/// request ceiling therefore claimed headroom the request does not have: the
/// earlier comment described `1024 * 1024` as a backstop "below" the frame
/// budget while the two were the same number. Subtracting the measured maximum
/// non-argument envelope puts this budget strictly below the request ceiling.
/// The whole request, not this component, is still the authority, and
/// `HostRuntimeRequest::validate` names its limit and the observed byte count
/// instead of an opaque `invalid`.
const MAX_ARGV_BYTES: usize =
    crate::MAX_HOST_RUNTIME_REQUEST_BYTES - crate::HOST_RUNTIME_REQUEST_ENVELOPE_BYTES;
// A canonical launch can project one mount for each selected model artifact,
// plus the fixed input and output mounts.  This reuses the existing compiled
// artifact ceiling rather than imposing a small engine-specific cap.
pub const MAX_COMPILED_EXECUTION_PLAN_ARTIFACTS: usize = 4096;
pub const MAX_COMPILED_EXECUTION_PLAN_MOUNTS: usize = MAX_COMPILED_EXECUTION_PLAN_ARTIFACTS + 2;

pub fn same_installed_workload(
    installed: &CompiledExecutionPlan,
    requested: &CompiledExecutionPlan,
) -> bool {
    // Network authority is derived from signed placement at start time. Validate
    // both plans before comparing the installed process and filesystem identity.
    installed.validate().is_ok()
        && requested.validate().is_ok()
        && installed.identity == requested.identity
        && installed.artifacts == requested.artifacts
        && installed.runtime.executable == requested.runtime.executable
        && installed.runtime.argv == requested.runtime.argv
        && installed.runtime.env == requested.runtime.env
        && installed.runtime.telemetry == requested.runtime.telemetry
        && installed.runtime.image_digest == requested.runtime.image_digest
        && installed.runtime.placement.memory_kind
            == requested.runtime.placement.memory_kind
        && installed.runtime_image == requested.runtime_image
        && installed.security.devices == requested.security.devices
        && installed.security.capabilities == requested.security.capabilities
        && installed.security.privileged == requested.security.privileged
        && installed.security.user == requested.security.user
        && installed.security.mounts == requested.security.mounts
        && installed.security.read_only_root == requested.security.read_only_root
        && installed.security.no_new_privileges == requested.security.no_new_privileges
        && installed.lifecycle == requested.lifecycle
        && installed.endpoint == requested.endpoint
        && installed.job == requested.job
        && installed.topology.name == requested.topology.name
        && installed.topology.mode == requested.topology.mode
        && installed.topology.backend == requested.topology.backend
        && installed.topology.node_count == requested.topology.node_count
}

/// A signed job invocation may bind different settings and a shorter timeout,
/// while retaining every installed filesystem, image and process authority.
pub fn same_job_workload(
    installed: &CompiledExecutionPlan,
    invocation: &CompiledExecutionPlan,
) -> bool {
    let (Some(installed_job), Some(job)) = (&installed.job, &invocation.job) else {
        return false;
    };
    if job.timeout_seconds == 0 || job.timeout_seconds > installed_job.timeout_seconds {
        return false;
    }
    let mut identity = invocation.clone();
    identity.identity.execution_sha256 = installed.identity.execution_sha256.clone();
    identity.runtime.argv = installed.runtime.argv.clone();
    identity.job.as_mut().unwrap().timeout_seconds = installed_job.timeout_seconds;
    same_installed_workload(installed, &identity)
        && installed.runtime.placement == invocation.runtime.placement
        && installed.security.network_mode == invocation.security.network_mode
}

impl CompiledExecutionPlan {
    /// Validate the current document, immutable artifact identity, and paths
    /// without admitting its runtime for execution. Teardown needs this storage
    /// authority even when saved launch settings can no longer run.
    pub fn validate_storage(&self) -> Result<(), WorkloadError> {
        let mut value = serde_json::to_value(self)
            .map_err(|_| WorkloadError::Invalid("compiled wire document"))?;
        crate::wire_schema::validate_and_materialize("CompiledExecutionPlan", &mut value)
            .map_err(|_| WorkloadError::Invalid("compiled wire document"))?;
        if self.schema_version != 2
            || !lower_hex(&self.identity.recipe_revision_sha256, 64)
            || !lower_hex(&self.identity.harness_sha256, 64)
            || !lower_hex(&self.identity.execution_sha256, 64)
            || self
                .identity
                .build_input_sha256
                .as_ref()
                .is_some_and(|value| !lower_hex(value, 64))
            || !lower_hex(&self.identity.model_artifact_set_sha256, 64)
            || self.artifacts.is_empty()
            || self.artifacts.len() > MAX_COMPILED_EXECUTION_PLAN_ARTIFACTS
            || self.runtime.image_digest != self.runtime_image.image_digest
            || self.runtime.placement.rank != self.topology.rank
            || self.runtime.placement.role != self.topology.role
            || self.runtime.placement.world_size != self.topology.world_size
            || self.topology.world_size == 0
            || self.topology.rank >= self.topology.world_size
            || self.topology.node_count == 0
            || self.topology.world_size < self.topology.node_count
            || self.endpoint.is_some() == self.job.is_some()
            || self.endpoint.is_some() != self.runtime.placement.port.is_some()
        {
            return Err(WorkloadError::Invalid("compiled execution identity"));
        }
        let mut physical_by_path = std::collections::BTreeMap::new();
        let mut file_paths = std::collections::BTreeMap::new();
        let mut by_digest = std::collections::BTreeMap::new();
        let mut projection_targets = BTreeSet::new();
        for artifact in &self.artifacts {
            artifact.validate()?;
            let physical = (
                artifact.file_id.as_str(),
                artifact.sha256.as_str(),
                artifact.size_bytes,
                artifact.model.publisher.as_str(),
                artifact.model.slug.as_str(),
                artifact.model.content_sha256.as_str(),
                artifact.distribution_object.name.as_str(),
                artifact.distribution_object.sha256.as_str(),
                artifact.distribution_object.bytes,
                artifact.distribution_object.kind.as_str(),
            );
            let physical_key = (artifact.selection_id.as_str(), artifact.path.as_str());
            if let Some(previous) = physical_by_path.insert(physical_key, physical)
                && previous != physical
            {
                return Err(WorkloadError::Invalid(
                    "compiled model artifact physical identity",
                ));
            }
            let file_key = (artifact.selection_id.as_str(), artifact.file_id.as_str());
            if let Some(previous_path) = file_paths.insert(file_key, artifact.path.as_str())
                && previous_path != artifact.path.as_str()
            {
                return Err(WorkloadError::Invalid("compiled model artifact identity"));
            }
            if let Some(previous) = by_digest.insert(artifact.sha256.as_str(), artifact.size_bytes)
                && previous != artifact.size_bytes
            {
                return Err(WorkloadError::Invalid("compiled model artifact bytes"));
            }
            if !projection_targets.insert((artifact.mount.target.as_str(), artifact.path.as_str()))
            {
                return Err(WorkloadError::Invalid(
                    "compiled model artifact mount target",
                ));
            }
        }
        let total = by_digest
            .values()
            .copied()
            .try_fold(0_u64, |sum, value| sum.checked_add(value))
            .ok_or(WorkloadError::Invalid("compiled model artifact bytes"))?;
        if total != self.identity.model_artifact_bytes {
            return Err(WorkloadError::Invalid("compiled model artifact-set bytes"));
        }
        Ok(())
    }

    pub fn validate(&self) -> Result<(), WorkloadError> {
        self.validate_storage()?;
        self.runtime.validate()?;
        self.runtime_image.validate()?;
        let placement = &self.runtime.placement;
        if placement.world_size > 1
            && placement.master_port.is_some()
            && (self.topology.node_count < 2
                || placement.local_address.is_none()
                || placement.master_address.is_none()
                || self.endpoint.is_none()
                || self.security.devices != ["nvidia.com/gpu=all"])
        {
            return Err(WorkloadError::Invalid("native fabric placement is incomplete"));
        }
        self.security.validate(&self.runtime.placement)?;
        self.topology.validate()?;
        self.lifecycle.validate()?;
        match (&self.endpoint, &self.job) {
            (Some(endpoint), None) => endpoint.validate()?,
            (None, Some(job)) => job.validate()?,
            _ => unreachable!("validated endpoint/job discriminator"),
        }
        Ok(())
    }
}

impl CompiledModelArtifact {
    fn validate(&self) -> Result<(), WorkloadError> {
        if !valid_name(&self.selection_id)
            || !valid_name(&self.file_id)
            || !valid_model_path(&self.path)
            || !lower_hex(&self.sha256, 64)
            || (self.size_bytes == 0 && self.sha256 != EMPTY_SHA256)
            || !self.roles.is_empty()
                && (self.roles.windows(2).any(|pair| pair[0] >= pair[1])
                    || self.roles.iter().any(|role| !valid_role(role)))
            || !valid_model_publisher(&self.model.publisher)
            || !valid_name(&self.model.slug)
            || !lower_hex(&self.model.content_sha256, 64)
            || self.distribution_object.kind.as_str() != "model"
            || self.distribution_object.name != self.path
            || self.distribution_object.sha256 != self.sha256
            || self.distribution_object.bytes != self.size_bytes
            || !(self.mount.target == "/models" || self.mount.target.starts_with("/models/"))
            || !self.mount.read_only
            || (self.size_bytes == 0
                && self
                    .roles
                    .iter()
                    .any(|role| matches!(role.as_str(), "model" | "weight" | "weights")))
        {
            return Err(WorkloadError::Invalid("compiled model artifact"));
        }
        if self.roles.is_empty() {
            return Err(WorkloadError::Invalid("compiled model artifact roles"));
        }
        if self.size_bytes > 0 {
            validate_distribution_object(&self.distribution_object)?;
        }
        Ok(())
    }
}

impl CompiledRuntime {
    fn validate(&self) -> Result<(), WorkloadError> {
        let telemetry = &self.telemetry;
        if telemetry.engine.is_empty()
            || telemetry.engine.len() > 64
            || telemetry.engine.contains('\0')
            || telemetry
                .engine_version
                .as_ref()
                .is_some_and(|v| v.is_empty() || v.len() > 128 || v.contains('\0'))
            || telemetry.metrics_format.is_some() != telemetry.metrics_path.is_some()
            || telemetry
                .metrics_format
                .as_deref()
                .is_some_and(|v| !matches!(v, "prometheus" | "comfyui-queue"))
            || telemetry.metrics_path.as_ref().is_some_and(|v| {
                v.len() > 256
                    || !v.starts_with('/')
                    || (v != "/" && !valid_model_path(&v[1..]))
                    || v.contains(['?', '#', '\r', '\n'])
            })
        {
            return Err(WorkloadError::Invalid("compiled telemetry"));
        }
        if self.executable.is_empty()
            || !self.executable.starts_with('/')
            || self.executable.len() > MAX_ARGV_ITEM_BYTES
            || self.executable.contains(['\0', '\r', '\n'])
            || !valid_opaque_argv(&self.argv)
            || self.env.len() > 128
        {
            return Err(WorkloadError::Invalid("compiled runtime"));
        }
        for entry in &self.env {
            if entry.name.is_empty()
                || entry.name.len() > 128
                || !entry.name.bytes().enumerate().all(|(index, byte)| {
                    if index == 0 {
                        byte.is_ascii_uppercase()
                    } else {
                        byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_'
                    }
                })
                || entry.value.len() > MAX_ARGV_ITEM_BYTES
                || entry.value.contains('\0')
            {
                return Err(WorkloadError::Invalid("compiled runtime environment"));
            }
        }
        if self.placement.world_size == 0
            || self.placement.rank >= self.placement.world_size
            || !valid_role(&self.placement.role)
            || self.placement.port.is_some_and(|port| port == 0)
            || self.placement.reserved_memory_bytes == 0
        {
            return Err(WorkloadError::Invalid("compiled runtime placement"));
        }
        Ok(())
    }
}

impl CompiledSecurity {
    fn validate(&self, placement: &CompiledRuntimePlacement) -> Result<(), WorkloadError> {
        let native_fabric = placement.world_size > 1 && placement.master_port.is_some();
        let expected_network_mode = if native_fabric {
            "host"
        } else if placement.endpoint_address.is_some() {
            "bridge"
        } else {
            "none"
        };
        if self.privileged
            || !self.no_new_privileges
            || !self.read_only_root
            || self.network_mode.as_str() != expected_network_mode
            || self.host_network != native_fabric
            || !self.capabilities.is_empty()
            || self.devices.len() > 1
            || self
                .devices
                .iter()
                .any(|value| value != "nvidia.com/gpu=all")
            || !numeric_non_root_user(&self.user)
            || self.mounts.len() > MAX_COMPILED_EXECUTION_PLAN_MOUNTS
            || self.mounts.iter().any(|mount| !valid_mount_policy(mount))
            || self
                .mounts
                .iter()
                .any(|mount| !valid_mount_target(&mount.target))
            || self
                .mounts
                .iter()
                .map(|mount| mount.target.as_str())
                .collect::<BTreeSet<_>>()
                .len()
                != self.mounts.len()
        {
            return Err(WorkloadError::Invalid("compiled security"));
        }
        Ok(())
    }
}

impl CompiledTopology {
    fn validate(&self) -> Result<(), WorkloadError> {
        if !valid_name(&self.name)
            || !matches!(
                self.mode.as_str(),
                "single"
                    | "distributed"
                    | "tensor_parallel"
                    | "pipeline_parallel"
                    | "data_parallel"
                    | "hybrid"
                    | "ray"
                    | "mpi"
            )
            || self.backend.is_empty()
            || self.backend.chars().count() > 64
            || self.node_count == 0
            || self.world_size == 0
            || self.rank >= self.world_size
            || !valid_role(&self.role)
        {
            return Err(WorkloadError::Invalid("compiled topology"));
        }
        Ok(())
    }
}

impl CompiledLifecycle {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.pre_start.len() > 16
            || self.post_stop.len() > 16
            || !(1..=600).contains(&self.stop_timeout_seconds)
            || self
                .pre_start
                .iter()
                .chain(&self.post_stop)
                .any(|argv| !valid_argv(argv))
        {
            return Err(WorkloadError::Invalid("compiled lifecycle"));
        }
        Ok(())
    }
}

impl CompiledEndpoint {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.protocol != "openai"
            || self.port < 1024
            || self.model_aliases.is_empty()
            || self.health_path.len() > 256
            || !self.health_path.starts_with('/')
            || self.health_path.contains("..")
        {
            return Err(WorkloadError::Invalid("compiled endpoint"));
        }
        Ok(())
    }
}

impl CompiledJobInputSlot {
    fn validate(&self, input_media_types: &BTreeSet<&str>, max_bytes: u64) -> bool {
        let mut media_types = BTreeSet::new();
        let mut extensions = BTreeSet::new();
        self.id.len() <= 32
            && valid_job_slot_id(&self.id)
            && !self.label.is_empty()
            && self.label.chars().count() <= 64
            && !self.description.is_empty()
            && self.description.chars().count() <= 256
            && (1..=16).contains(&self.media_types.len())
            && self.media_types.iter().all(|media_type| {
                valid_job_media_type(media_type)
                    && input_media_types.contains(media_type.as_str())
                    && media_types.insert(media_type.as_str())
            })
            && self.extensions.len() <= 16
            && self.extensions.iter().all(|extension| {
                valid_job_extension(extension) && extensions.insert(extension.as_str())
            })
            && self.min_files <= self.max_files
            && (1..=32).contains(&self.max_files)
            && self.max_file_bytes > 0
            && self.max_file_bytes <= 512 * 1024 * 1024
            && self.max_total_bytes >= self.max_file_bytes
            && u64::from(self.max_total_bytes) <= max_bytes
    }
}

impl CompiledJobInput {
    fn validate(&self) -> Result<(), WorkloadError> {
        let media_types = self
            .media_types
            .iter()
            .map(String::as_str)
            .collect::<BTreeSet<_>>();
        if self.path != "/inputs"
            || !(1..=16).contains(&self.media_types.len())
            || media_types.len() != self.media_types.len()
            || self
                .media_types
                .iter()
                .any(|value| !valid_job_media_type(value))
            || self.max_bytes == 0
            || self.max_bytes > 1024 * 1024 * 1024
            || self.slots.as_ref().is_some_and(|slots| {
                !(1..=32).contains(&slots.len())
                    || slots
                        .iter()
                        .map(|slot| slot.id.as_str())
                        .collect::<BTreeSet<_>>()
                        .len()
                        != slots.len()
                    || slots
                        .iter()
                        .any(|slot| !slot.validate(&media_types, u64::from(self.max_bytes)))
            })
        {
            return Err(WorkloadError::Invalid("compiled job input"));
        }
        Ok(())
    }
}

impl CompiledJob {
    fn validate(&self) -> Result<(), WorkloadError> {
        if !matches!(
            self.interface.as_str(),
            "image-job" | "audio-job" | "video-job" | "mesh-job" | "artifact-job"
        ) || self.output_path != "/outputs"
            || !(1..=3600).contains(&self.timeout_seconds)
        {
            return Err(WorkloadError::Invalid("compiled job"));
        }
        if let Some(input) = &self.input {
            input.validate()?;
        }
        Ok(())
    }
}

impl CompiledRuntimeImage {
    /// The local imported reference for this exact archive and
    /// registry/platform identity.
    ///
    /// The Controller owns and sends its own derivation in the transported
    /// `local_image_reference` field, and `validate()` requires that field to
    /// equal this derivation. The accessor therefore answers with the
    /// derivation itself: a Controller transport path can never become a
    /// container-engine image argument, even if a caller reads the reference
    /// before validation.
    pub fn local_image_reference(&self) -> String {
        self.expected_local_image_reference()
    }

    fn validate(&self) -> Result<(), WorkloadError> {
        if !self.image_digest.starts_with("sha256:")
            || !lower_hex(&self.image_digest[7..], 64)
            || self
                .registry_manifest_digest
                .as_ref()
                .is_some_and(|value| !valid_sha256_prefixed(value))
            || !valid_sha256_prefixed(&self.platform_manifest_digest)
            || !valid_sha256_prefixed(&self.local_image_config_id)
            || self.local_image_reference != self.expected_local_image_reference()
            || self.platform_manifest_digest != self.image_digest
            || self.runtime_interface_label.is_empty()
            || self.runtime_interface_label.len() > 128
            || !lower_hex(&self.oci_layout_sha256, 64)
            || self.image_bytes == 0
            || self.architecture != "linux-arm64"
            || self.runtime_interface != "vonk.runtime.v1"
            || !matches!(self.source.as_str(), "published" | "controller-build")
            || (self.source.as_str() == "published" && self.build_id.is_some())
            || (self.source.as_str() == "published" && self.registry_manifest_digest.is_none())
            || (self.source.as_str() == "controller-build"
                && self.build_id.as_deref().is_none_or(str::is_empty))
            || (self.source.as_str() == "controller-build"
                && self.registry_manifest_digest.is_some())
            || self.distribution_object.kind.as_str() != "oci-archive"
            || self.distribution_object.name != "image.oci.tar"
            || self.distribution_object.sha256 != self.oci_layout_sha256
            || self.distribution_object.bytes != self.image_bytes
        {
            return Err(WorkloadError::Invalid("compiled runtime image"));
        }
        validate_distribution_object(&self.distribution_object)
    }

    fn expected_local_image_reference(&self) -> String {
        let parent = &self.platform_manifest_digest;
        format!(
            "localhost/vonk/compiled-runtime-{}@{}",
            self.oci_layout_sha256, parent
        )
    }
}

pub fn materialized_model_path(
    root: &Path,
    artifact: &CompiledModelArtifact,
) -> Result<std::path::PathBuf, WorkloadError> {
    if !root.is_absolute() {
        return Err(WorkloadError::Invalid("compiled model root"));
    }
    artifact.validate()?;
    let relative = Path::new(&artifact.selection_id).join(&artifact.path);
    if relative
        .components()
        .any(|component| !matches!(component, std::path::Component::Normal(_)))
    {
        return Err(WorkloadError::Invalid("compiled model path"));
    }
    Ok(root.join(relative))
}

fn valid_model_path(value: &str) -> bool {
    !value.is_empty()
        && value.chars().count() <= 512
        && !value.contains(['\\', '\0'])
        && value
            .split('/')
            .all(|part| !part.is_empty() && !matches!(part, "." | ".."))
}

fn valid_mount_target(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 512
        && value.starts_with('/')
        && value != "/"
        && !value.contains(['\\', '\0'])
        && value
            .split('/')
            .skip(1)
            .all(|part| !part.is_empty() && !matches!(part, "." | ".."))
}

fn valid_mount_policy(mount: &MountSpec) -> bool {
    match mount.source.as_str() {
        "model" => {
            mount.read_only && (mount.target == "/models" || mount.target.starts_with("/models/"))
        }
        "inputs" => mount.read_only && mount.target == "/inputs",
        "outputs" => !mount.read_only && mount.target == "/outputs",
        _ => false,
    }
}

impl CompiledRuntimePlacement {
    /// Validate placement after Controller assignment has resolved execution addresses.
    pub fn validate_bound(&self) -> Result<(), WorkloadError> {
        if self.rank >= self.world_size
            || self.world_size == 0
            || !valid_role(&self.role)
            || self.port.is_some_and(|port| port < 1024)
            || self.reserved_memory_bytes == 0
            || if self.world_size == 1 {
                self.local_address.is_some()
                    || self.master_address.is_some()
                    || self.master_port.is_some()
                    || self.rank != 0
            } else {
                self.local_address.is_none()
                    || self.master_address.is_none()
                    || self.master_port.is_none_or(|port| port < 1024)
            }
        {
            return Err(WorkloadError::Invalid("placement"));
        }
        Ok(())
    }
}

pub fn managed_path(
    root: &Path,
    category: &str,
    identifier: &str,
) -> Result<std::path::PathBuf, WorkloadError> {
    if !matches!(category, "installations" | "models" | "runs")
        || identifier.is_empty()
        || identifier.len() > 128
        || !identifier
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(WorkloadError::Invalid("managed path"));
    }
    Ok(root.join(category).join(identifier))
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_sha256_prefixed(value: &str) -> bool {
    value.starts_with("sha256:") && lower_hex(&value[7..], 64)
}

fn valid_role(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.bytes().enumerate().all(|(index, byte)| {
            if index == 0 {
                byte.is_ascii_lowercase()
            } else {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
            }
        })
}

fn valid_job_slot_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 32
        && value.bytes().enumerate().all(|(index, byte)| {
            if index == 0 {
                byte.is_ascii_alphabetic()
            } else {
                byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-')
            }
        })
}

fn valid_job_media_type(value: &str) -> bool {
    let mut parts = value.split('/');
    let valid_token = |token: &str| {
        !token.is_empty()
            && token.bytes().all(|byte| {
                byte.is_ascii_lowercase()
                    || byte.is_ascii_digit()
                    || matches!(
                        byte,
                        b'!' | b'#' | b'$' | b'&' | b'^' | b'_' | b'.' | b'+' | b'-'
                    )
            })
    };
    valid_token(parts.next().unwrap_or_default())
        && valid_token(parts.next().unwrap_or_default())
        && parts.next().is_none()
}

fn valid_job_extension(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() >= 2
        && bytes.len() <= 17
        && bytes[0] == b'.'
        && (bytes[1].is_ascii_lowercase() || bytes[1].is_ascii_digit())
        && bytes[2..].iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
}

fn valid_name(value: &str) -> bool {
    valid_role(value)
        || !value.is_empty()
            && value.len() <= 64
            && value.bytes().all(|byte| {
                byte.is_ascii_lowercase()
                    || byte.is_ascii_digit()
                    || matches!(byte, b'.' | b'_' | b'-')
            })
}

fn valid_model_publisher(value: &str) -> bool {
    !value.is_empty() && value.chars().count() <= 128 && !value.contains('\0')
}

fn valid_argv(value: &[String]) -> bool {
    !value.is_empty()
        && value.len() <= MAX_ARGV_ITEMS
        && !value[0].is_empty()
        && value
            .iter()
            .all(|item| item.len() <= MAX_ARGV_ITEM_BYTES && !item.contains('\0'))
        && value
            .iter()
            .try_fold(0_usize, |total, item| total.checked_add(item.len()))
            .is_some_and(|bytes| bytes <= MAX_ARGV_BYTES)
}

fn valid_opaque_argv(value: &[String]) -> bool {
    value.len() <= MAX_ARGV_ITEMS
        && value
            .iter()
            .all(|item| item.len() <= MAX_ARGV_ITEM_BYTES && !item.contains('\0'))
        && value
            .iter()
            .try_fold(0_usize, |total, item| total.checked_add(item.len()))
            .is_some_and(|bytes| bytes <= MAX_ARGV_BYTES)
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

fn validate_distribution_object(
    value: &crate::generated::CompiledDistributionObject,
) -> Result<(), WorkloadError> {
    let mut document = serde_json::to_value(value)
        .map_err(|_| WorkloadError::Invalid("compiled distribution object"))?;
    crate::wire_schema::validate_and_materialize("CompiledDistributionObject", &mut document)
        .map_err(|_| WorkloadError::Invalid("compiled distribution object"))
}


#[cfg(test)]
mod argv_bound_tests {
    use super::{
        CompiledExecutionPlan, MAX_ARGV_BYTES, MAX_ARGV_ITEM_BYTES, MAX_ARGV_ITEMS,
        same_installed_workload, valid_argv, valid_opaque_argv,
    };

    #[test]
    fn a_long_command_is_admitted_and_an_absurd_one_is_refused() {
        // Wrong implementation: MAX_ARGV_ITEMS = 512 refused a 600-element
        // engine command while the derived argv byte ceiling was nowhere near.
        let long: Vec<String> = (0..600).map(|index| format!("--flag-{index}")).collect();
        assert!(valid_opaque_argv(&long));
        let mut with_executable = vec!["/opt/vonk/bin/vllm".to_owned()];
        with_executable.extend(long);
        assert!(valid_argv(&with_executable));

        let absurd: Vec<String> = (0..MAX_ARGV_ITEMS + 1)
            .map(|index| format!("--flag-{index}"))
            .collect();
        assert!(!valid_opaque_argv(&absurd));
    }

    #[test]
    fn the_argv_byte_budget_is_admitted_exactly_and_refused_one_byte_over() {
        // Wrong implementation: this budget was `1024 * 1024`, exactly the
        // helper frame budget, while its comment called it a backstop *below*
        // that budget -- so an argv the plan admitted could still be
        // unframeable by the request that has to carry it. The envelope test in
        // `lib.rs` proves the budget still derives below that request ceiling.
        // The item ceiling is separate, so reach this total in item-sized
        // pieces.
        let mut exact = vec!["x".repeat(MAX_ARGV_ITEM_BYTES); MAX_ARGV_BYTES / MAX_ARGV_ITEM_BYTES];
        exact.push("x".repeat(MAX_ARGV_BYTES % MAX_ARGV_ITEM_BYTES));
        assert_eq!(exact.iter().map(String::len).sum::<usize>(), MAX_ARGV_BYTES);
        assert!(valid_opaque_argv(&exact));
        exact.push("x".to_owned());
        assert!(!valid_opaque_argv(&exact));
    }

    #[test]
    fn installed_workload_identity_binds_the_physical_memory_pool() {
        let installed: CompiledExecutionPlan = serde_json::from_str(include_str!(
            "../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
        ))
        .unwrap();
        let mut requested = installed.clone();
        requested.runtime.placement.memory_kind = "host".parse().unwrap();
        assert!(!same_installed_workload(&installed, &requested));
    }
}
