use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::{IpAddr, ToSocketAddrs},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path, PathBuf},
    time::Duration,
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use url::Url;

use crate::{
    health::readiness_endpoint,
    inventory::{available_disk_bytes, available_memory_bytes},
    process::{ProcessError, ProcessRunner, Program},
    workloads::{
        ArgumentValue, Placement, WorkloadError, WorkloadSpec, image_digest, managed_path,
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

#[derive(Debug, Serialize)]
struct RuntimeContract<'a> {
    schema_version: u8,
    interface: &'static str,
    installation_id: &'a str,
    run_id: &'a str,
    artifacts: Vec<RuntimeArtifact>,
    endpoint: RuntimeEndpoint<'a>,
    placement: RuntimePlacement<'a>,
}

#[derive(Debug, Serialize)]
struct RuntimeArtifact {
    kind: String,
    repository: String,
    revision: String,
    path: String,
}

#[derive(Debug, Serialize)]
struct RuntimeEndpoint<'a> {
    listen_host: &'static str,
    listen_port: u16,
    protocol: &'a str,
    model_aliases: &'a [String],
    health_path: &'a str,
}

#[derive(Debug, Serialize)]
struct RuntimePlacement<'a> {
    endpoint_address: Option<std::net::IpAddr>,
    rank: u32,
    role: &'a str,
    world_size: u32,
    local_address: Option<std::net::IpAddr>,
    master_address: Option<std::net::IpAddr>,
    master_port: Option<u16>,
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
}

struct RecipeRunProbe {
    run_id: String,
    address: IpAddr,
    port: u16,
    health_path: String,
}

pub struct RuntimeStartPlan {
    pub image_digest: String,
    pub pre_start: Vec<Vec<String>>,
    pub main: Vec<String>,
}

pub struct RuntimeStopPlan {
    pub remove: Vec<String>,
    pub image_digest: Option<String>,
    pub post_stop: Vec<Vec<String>>,
}

fn runtime_policy() -> Result<RuntimePolicy, OciError> {
    serde_json::from_str(include_str!(
        "../../../../schemas/global/container-runtime-policy-v1.json"
    ))
    .map_err(OciError::Json)
}

impl<R: ProcessRunner> OciRuntime<'_, R> {
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
        spec: &WorkloadSpec,
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
        let models = self.data_root.join("models").join("sha256");
        fs::create_dir_all(&models)?;
        fs::create_dir_all(&installation)?;
        fs::set_permissions(&installation, fs::Permissions::from_mode(0o700))?;
        for artifact in &spec.artifacts {
            self.materialize_artifact(&models, artifact)?;
        }
        atomic_write(&installation, "spec.json", &serde_json::to_vec(spec)?)?;
        atomic_write(
            &installation,
            "recipe-content.sha256",
            recipe_content_sha256.as_bytes(),
        )?;
        File::open(&installation)?.sync_all()?;
        Ok(())
    }

    pub fn verify_image(&self, spec: &WorkloadSpec) -> Result<(), OciError> {
        spec.validate()?;
        let policy = runtime_policy()?;
        let image_name = spec
            .runtime
            .image
            .split_once('@')
            .map(|(name, _)| name)
            .ok_or(OciError::ImageDigest)?;
        let build_id = image_name
            .strip_prefix("localhost/vonk/recipe-build-")
            .ok_or(OciError::ImageDigest)?;
        if spec.runtime.interface != policy.runtime_interface
            || spec.runtime.architecture != policy.architecture
            || policy.required_image_label.name != "ai.vonkforge.runtime-interface"
            || policy.required_image_label.value != "v1"
            || uuid::Uuid::parse_str(build_id)
                .ok()
                .is_none_or(|value| value.to_string() != build_id)
            || image_digest(&spec.runtime.image).is_none()
        {
            return Err(OciError::ImageDigest);
        }
        Ok(())
    }

    pub fn start_arguments(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
    ) -> Result<Vec<String>, OciError> {
        spec.validate()?;
        placement.validate()?;
        if spec.security.host_network
            && (placement.world_size < 2 || placement.port != spec.endpoint.port)
        {
            return Err(OciError::Runtime);
        }
        managed_path(self.data_root, "installations", installation_id)?;
        let models = self.data_root.join("models");
        let state = managed_path(self.data_root, "runs", run_id)?;
        let metadata = self.run_metadata_path(run_id)?;
        let shared_memory_bytes =
            (placement.reserved_memory_bytes / 8).clamp(64 * 1024 * 1024, 16 * 1024 * 1024 * 1024);
        let mut arguments = vec![
            "run".to_owned(),
            "--detach".to_owned(),
            "--name".to_owned(),
            format!("vonk-{run_id}"),
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
            if spec.security.host_network {
                "host".to_owned()
            } else {
                "bridge".to_owned()
            },
            "--pids-limit".to_owned(),
            "4096".to_owned(),
            "--memory".to_owned(),
            placement.reserved_memory_bytes.to_string(),
            "--memory-swap".to_owned(),
            placement.reserved_memory_bytes.to_string(),
            "--shm-size".to_owned(),
            shared_memory_bytes.to_string(),
            "--user".to_owned(),
            spec.security.user.clone(),
            "--env".to_owned(),
            format!("VONK_RANK={}", placement.rank),
            "--env".to_owned(),
            format!("VONK_WORLD_SIZE={}", placement.world_size),
            "--env".to_owned(),
            "VONK_RUNTIME_SPEC=/run/vonk/runtime.json".to_owned(),
            "--env".to_owned(),
            "VONK_MODEL_ROOT=/models".to_owned(),
            "--env".to_owned(),
            "VONK_STATE_ROOT=/state".to_owned(),
            "--env".to_owned(),
            "VONK_LISTEN_HOST=0.0.0.0".to_owned(),
            "--env".to_owned(),
            format!("VONK_LISTEN_PORT={}", spec.endpoint.port),
        ];
        if spec.security.host_network {
            arguments.extend([
                "--ipc".to_owned(),
                "host".to_owned(),
                "--device".to_owned(),
                "/dev/infiniband:/dev/infiniband".to_owned(),
                "--ulimit".to_owned(),
                "memlock=-1:-1".to_owned(),
                "--ulimit".to_owned(),
                "stack=67108864:67108864".to_owned(),
            ]);
        } else {
            arguments.extend([
                "--publish".to_owned(),
                match placement.endpoint_address {
                    Some(IpAddr::V4(address)) => {
                        format!("{address}:{}:{}", placement.port, spec.endpoint.port)
                    }
                    Some(IpAddr::V6(address)) => {
                        format!("[{address}]:{}:{}", placement.port, spec.endpoint.port)
                    }
                    None => format!("{}:{}", placement.port, spec.endpoint.port),
                },
            ]);
        }
        if let Some(master) = placement.master_address {
            arguments.extend(["--env".to_owned(), format!("VONK_MASTER_ADDR={master}")]);
        }
        if let Some(local) = placement.local_address {
            arguments.extend(["--env".to_owned(), format!("VONK_LOCAL_ADDR={local}")]);
        }
        if let Some(master_port) = placement.master_port {
            arguments.extend([
                "--env".to_owned(),
                format!("VONK_MASTER_PORT={master_port}"),
            ]);
            if placement.rank == 0 && !spec.security.host_network {
                let master_address = placement.master_address.ok_or(OciError::Runtime)?;
                let publication = match master_address {
                    IpAddr::V4(address) => {
                        format!("{address}:{master_port}:{master_port}")
                    }
                    IpAddr::V6(address) => {
                        format!("[{address}]:{master_port}:{master_port}")
                    }
                };
                arguments.extend(["--publish".to_owned(), publication]);
            }
        }
        if spec
            .security
            .devices
            .iter()
            .any(|device| device == "nvidia.com/gpu=all")
        {
            arguments.extend(["--gpus".to_owned(), "all".to_owned()]);
        }
        for environment in &spec.runtime.environment {
            let Some(value) = &environment.value else {
                return Err(OciError::Runtime);
            };
            let value = match value {
                ArgumentValue::Boolean(value) => value.to_string(),
                ArgumentValue::Integer(value) => value.to_string(),
                ArgumentValue::String(value) => value.clone(),
            };
            arguments.extend(["--env".to_owned(), format!("{}={value}", environment.name)]);
        }
        for mount in &spec.security.mounts {
            let source = if mount.source == "model" {
                &models
            } else {
                &state
            };
            let mut value = format!("type=bind,src={},dst={}", source.display(), mount.target);
            if mount.read_only {
                value.push_str(",readonly");
            }
            arguments.extend(["--mount".to_owned(), value]);
        }
        arguments.extend([
            "--mount".to_owned(),
            format!(
                "type=bind,src={},dst=/run/vonk/runtime.json,readonly",
                metadata.join("runtime.json").display()
            ),
        ]);
        arguments.push(spec.runtime.image.clone());
        arguments.extend(spec.runtime.entrypoint.iter().cloned());
        for argument in &spec.runtime.arguments {
            arguments.push(format!("--{}", argument.name.replace('_', "-")));
            match &argument.value {
                ArgumentValue::Boolean(true) => {}
                ArgumentValue::Boolean(false) => arguments.push("false".to_owned()),
                ArgumentValue::Integer(value) => arguments.push(value.to_string()),
                ArgumentValue::String(value) => arguments.push(value.clone()),
            }
        }
        Ok(arguments)
    }

    pub fn prepare_start(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.verify_image(spec)?;
        let state = managed_path(self.data_root, "runs", run_id)?;
        fs::create_dir_all(&state)?;
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
        let metadata = self.ensure_run_metadata(run_id)?;
        self.write_runtime_contract(spec, installation_id, run_id, placement)?;
        let main = self.start_arguments(spec, installation_id, run_id, placement)?;
        let pre_start = spec
            .lifecycle
            .pre_start
            .iter()
            .map(|hook| hook_arguments(&main, &spec.runtime.image, hook))
            .collect::<Result<Vec<_>, _>>()?;
        atomic_write(
            &metadata,
            "lifecycle.json",
            &serde_json::to_vec(&RunLifecycle {
                installation_id: installation_id.to_owned(),
                placement: placement.clone(),
            })?,
        )?;
        Ok(RuntimeStartPlan {
            image_digest: format!(
                "sha256:{}",
                image_digest(&spec.runtime.image).ok_or(OciError::ImageDigest)?
            ),
            pre_start,
            main,
        })
    }

    pub fn prepare_stop(&self, run_id: &str) -> Result<RuntimeStopPlan, OciError> {
        let lifecycle = self.load_run_lifecycle(run_id)?;
        let stop_timeout = lifecycle
            .as_ref()
            .map(|(spec, _, _)| spec.lifecycle.stop_timeout_seconds)
            .unwrap_or(30);
        let (image_digest, post_stop) = match lifecycle {
            Some((spec, installation_id, placement)) => {
                let main = self.start_arguments(&spec, &installation_id, run_id, &placement)?;
                (
                    Some(format!(
                        "sha256:{}",
                        image_digest(&spec.runtime.image).ok_or(OciError::ImageDigest)?
                    )),
                    spec.lifecycle
                        .post_stop
                        .iter()
                        .map(|hook| hook_arguments(&main, &spec.runtime.image, hook))
                        .collect::<Result<Vec<_>, _>>()?,
                )
            }
            None => (None, Vec::new()),
        };
        Ok(RuntimeStopPlan {
            remove: vec![run_id.to_owned(), stop_timeout.to_string()],
            image_digest,
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

    pub fn recipe_run_observations(&self) -> Result<Vec<RecipeRunObservation>, OciError> {
        let probes = self.recipe_run_probes()?;
        probes
            .into_iter()
            .map(|probe| {
                let ready = self.readiness_request(probe.address, probe.port, &probe.health_path);
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
            let Some((spec, _, placement)) = lifecycle else {
                continue;
            };
            if spec.endpoint.health_path.contains(['?', '#', '\0'])
                || !spec
                    .endpoint
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
                address: placement
                    .endpoint_address
                    .unwrap_or(IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)),
                port: placement.port,
                health_path: spec.endpoint.health_path,
            });
        }
        Ok(probes)
    }

    fn readiness_request(&self, address: IpAddr, port: u16, health_path: &str) -> bool {
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

    fn load_run_lifecycle(
        &self,
        run_id: &str,
    ) -> Result<Option<(WorkloadSpec, String, Placement)>, OciError> {
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
        Ok(Some((spec, record.installation_id, record.placement)))
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

    pub fn uninstall(&self, installation_id: &str) -> Result<(), OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let metadata = fs::symlink_metadata(&installation)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        self.verify_installation(installation_id)?;
        fs::remove_dir_all(installation)?;
        File::open(self.data_root.join("installations"))?.sync_all()?;
        Ok(())
    }

    pub fn load_spec(&self, installation_id: &str) -> Result<WorkloadSpec, OciError> {
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
        let spec: WorkloadSpec = serde_json::from_slice(&read_regular_file(&path, 64 * 1024)?)?;
        spec.validate()?;
        Ok(spec)
    }

    pub fn verify_installation(&self, installation_id: &str) -> Result<(), OciError> {
        let spec = self.load_spec(installation_id)?;
        let models = self.data_root.join("models").join("sha256");
        for artifact in &spec.artifacts {
            let destination = models.join(artifact_key(artifact)?);
            verify_manifest(&destination)?;
        }
        Ok(())
    }

    pub fn recipe_digest(&self, installation_id: &str) -> Result<String, OciError> {
        let path = managed_path(self.data_root, "installations", installation_id)?
            .join("recipe-content.sha256");
        let value = fs::read_to_string(path)?;
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
        let spec = self.load_spec(installation_id)?;
        for artifact in &spec.artifacts {
            let manifest = read_manifest(
                &self
                    .data_root
                    .join("models")
                    .join("sha256")
                    .join(artifact_key(artifact)?),
            )?;
            total = total
                .checked_add(manifest.total_bytes)
                .ok_or(OciError::Artifact)?;
        }
        Ok(total)
    }

    pub fn artifact_set_digest(&self, installation_id: &str) -> Result<String, OciError> {
        let spec = self.load_spec(installation_id)?;
        let mut identities = Vec::with_capacity(spec.artifacts.len());
        for artifact in &spec.artifacts {
            let key = artifact_key(artifact)?;
            let manifest = read_manifest(&self.data_root.join("models").join("sha256").join(&key))?;
            identities.push(serde_json::json!({
                "artifact": artifact,
                "key": key,
                "manifest": manifest,
            }));
        }
        Ok(hex::encode(Sha256::digest(serde_json::to_vec(
            &identities,
        )?)))
    }

    fn write_runtime_contract(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
    ) -> Result<(), OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        let artifacts = spec
            .artifacts
            .iter()
            .map(|artifact| {
                Ok(RuntimeArtifact {
                    kind: artifact.kind.clone(),
                    repository: artifact.repository.clone(),
                    revision: artifact.revision.clone(),
                    path: format!("/models/sha256/{}", artifact_key(artifact)?),
                })
            })
            .collect::<Result<Vec<_>, OciError>>()?;
        let contract = RuntimeContract {
            schema_version: 1,
            interface: "vonk.runtime.v1",
            installation_id,
            run_id,
            artifacts,
            endpoint: RuntimeEndpoint {
                listen_host: "0.0.0.0",
                listen_port: spec.endpoint.port,
                protocol: &spec.endpoint.protocol,
                model_aliases: &spec.endpoint.model_aliases,
                health_path: &spec.endpoint.health_path,
            },
            placement: RuntimePlacement {
                endpoint_address: placement.endpoint_address,
                rank: placement.rank,
                role: &placement.role,
                world_size: placement.world_size,
                local_address: placement.local_address,
                master_address: placement.master_address,
                master_port: placement.master_port,
            },
        };
        atomic_write(&metadata, "runtime.json", &serde_json::to_vec(&contract)?)?;
        Ok(())
    }

    fn materialize_artifact(
        &self,
        models: &Path,
        artifact: &crate::workloads::ArtifactSpec,
    ) -> Result<(), OciError> {
        let key = artifact_key(artifact)?;
        let destination = models.join(&key);
        if destination.exists() {
            return verify_manifest(&destination);
        }
        let staging = models.join(format!(".{key}.{}.staging", std::process::id()));
        fs::create_dir(&staging)?;
        let download = match artifact.kind.as_str() {
            "huggingface.snapshot" => self.download_huggingface(&staging, artifact),
            "http.file" => Url::parse(&artifact.repository)
                .map_err(|_| OciError::Artifact)
                .and_then(|url| {
                    self.download_https(
                        &url,
                        &staging.join("artifact"),
                        artifact.download_bytes,
                        false,
                    )
                }),
            "oci.artifact" => oci_endpoint(&artifact.repository).and_then(|endpoint| {
                let address = match endpoint.address {
                    IpAddr::V4(address) => address.to_string(),
                    IpAddr::V6(address) => format!("[{address}]"),
                };
                let arguments = vec![
                    "pull".to_owned(),
                    format!("{}@{}", artifact.repository, artifact.revision),
                    "--resolve".to_owned(),
                    format!("{}:{}:{}", endpoint.hostname, endpoint.port, address),
                    "--output".to_owned(),
                    staging.display().to_string(),
                ];
                let output = self.runner.run_bounded_directory(
                    Program::Oras,
                    &arguments,
                    Duration::from_secs(3600),
                    &staging,
                    artifact.download_bytes,
                )?;
                if output.success {
                    Ok(())
                } else {
                    Err(OciError::Artifact)
                }
            }),
            _ => Err(OciError::Artifact),
        };
        if download.is_err() {
            let _ = fs::remove_dir_all(&staging);
            return Err(OciError::Artifact);
        }
        let manifest = create_manifest(&staging)?;
        if manifest.total_bytes != artifact.download_bytes
            || artifact.kind == "http.file"
                && manifest.files.get("artifact").map(String::as_str)
                    != artifact.revision.strip_prefix("sha256:")
        {
            let _ = fs::remove_dir_all(&staging);
            return Err(OciError::Artifact);
        }
        atomic_write(
            &staging,
            ".vonk-manifest.json",
            &serde_json::to_vec(&manifest)?,
        )?;
        fs::rename(&staging, &destination)?;
        File::open(models)?.sync_all()?;
        Ok(())
    }

    fn download_huggingface(
        &self,
        staging: &Path,
        artifact: &crate::workloads::ArtifactSpec,
    ) -> Result<(), OciError> {
        let repository = huggingface_repository(&artifact.repository)?;
        let mut metadata_url =
            Url::parse("https://huggingface.co/api/models").map_err(|_| OciError::Artifact)?;
        metadata_url
            .path_segments_mut()
            .map_err(|_| OciError::Artifact)?
            .extend(repository)
            .push("revision")
            .push(&artifact.revision);
        let metadata_path = staging.join(".huggingface-model.json");
        self.download_https(&metadata_url, &metadata_path, 8 * 1024 * 1024, true)?;
        let metadata = fs::read(&metadata_path)?;
        fs::remove_file(&metadata_path)?;
        if metadata.len() > 8 * 1024 * 1024 {
            return Err(OciError::Artifact);
        }
        let model: HuggingFaceModel = serde_json::from_slice(&metadata)?;
        if model.siblings.is_empty() || model.siblings.len() > 20_000 {
            return Err(OciError::Artifact);
        }
        let mut remaining = artifact.download_bytes;
        for file in model.siblings {
            let relative = safe_relative_path(&file.rfilename)?;
            let destination = staging.join(&relative);
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent)?;
            }
            let mut url = Url::parse("https://huggingface.co/").map_err(|_| OciError::Artifact)?;
            url.path_segments_mut()
                .map_err(|_| OciError::Artifact)?
                .extend(repository.iter().copied())
                .push("resolve")
                .push(&artifact.revision)
                .extend(
                    relative
                        .components()
                        .filter_map(|component| match component {
                            Component::Normal(value) => value.to_str(),
                            _ => None,
                        }),
                );
            url.query_pairs_mut().append_pair("download", "true");
            self.download_https(&url, &destination, remaining, true)?;
            let downloaded = fs::metadata(&destination)?.len();
            remaining = remaining
                .checked_sub(downloaded)
                .ok_or(OciError::Artifact)?;
            if let Some(lfs) = file.lfs
                && (!lower_hex(&lfs.sha256, 64) || sha256_file(&destination)? != lfs.sha256)
            {
                return Err(OciError::Artifact);
            }
        }
        Ok(())
    }

    fn download_https(
        &self,
        url: &Url,
        destination: &Path,
        maximum_bytes: u64,
        allow_huggingface_auth: bool,
    ) -> Result<(), OciError> {
        if maximum_bytes == 0 {
            return Err(OciError::Artifact);
        }
        let mut current = url.clone();
        for _ in 0..=5 {
            let endpoint = public_https_endpoint(&current)?;
            let mut arguments = curl_arguments(&current, destination, maximum_bytes, &endpoint);
            if allow_huggingface_auth
                && current.host_str() == Some("huggingface.co")
                && let Some(path) = self.huggingface_curl_config
            {
                validate_curl_config(path)?;
                arguments.splice(0..0, ["--config".to_owned(), path.display().to_string()]);
            }
            let output = self
                .runner
                .run(Program::Curl, &arguments, Duration::from_secs(3600))?;
            if !output.success {
                return Err(OciError::Artifact);
            }
            let status = std::str::from_utf8(&output.stdout)
                .map_err(|_| OciError::Artifact)?
                .trim_end_matches(['\r', '\n']);
            let (code, redirect) = status.split_once('\t').ok_or(OciError::Artifact)?;
            let code = code.parse::<u16>().map_err(|_| OciError::Artifact)?;
            if (200..300).contains(&code) {
                if fs::metadata(destination)?.len() > maximum_bytes {
                    return Err(OciError::Artifact);
                }
                return Ok(());
            }
            if !(300..400).contains(&code) || redirect.is_empty() {
                return Err(OciError::Artifact);
            }
            current = current.join(redirect).map_err(|_| OciError::Artifact)?;
        }
        Err(OciError::Artifact)
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ArtifactManifest {
    schema_version: u8,
    files: BTreeMap<String, String>,
    total_bytes: u64,
}

#[derive(Debug, Deserialize)]
struct HuggingFaceModel {
    siblings: Vec<HuggingFaceFile>,
}

#[derive(Debug, Deserialize)]
struct HuggingFaceFile {
    rfilename: String,
    lfs: Option<HuggingFaceLfs>,
}

#[derive(Debug, Deserialize)]
struct HuggingFaceLfs {
    sha256: String,
}

fn huggingface_repository(value: &str) -> Result<[&str; 2], OciError> {
    let parts = value.split('/').collect::<Vec<_>>();
    if parts.len() != 2
        || parts.iter().any(|part| {
            part.is_empty()
                || part.len() > 96
                || !part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        })
    {
        return Err(OciError::Artifact);
    }
    Ok([parts[0], parts[1]])
}

fn safe_relative_path(value: &str) -> Result<PathBuf, OciError> {
    if value.is_empty()
        || value.len() > 512
        || value.contains('\0')
        || value.contains('\\')
        || value.split('/').count() > 32
    {
        return Err(OciError::Artifact);
    }
    let path = PathBuf::from(value);
    if path
        .components()
        .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(OciError::Artifact);
    }
    Ok(path)
}

struct PublicEndpoint {
    hostname: String,
    port: u16,
    address: IpAddr,
}

fn public_https_endpoint(url: &Url) -> Result<PublicEndpoint, OciError> {
    public_https_endpoint_with(url, |hostname, port| {
        (hostname, port)
            .to_socket_addrs()
            .map_err(|_| OciError::Artifact)
            .map(|addresses| addresses.map(|address| address.ip()).collect())
    })
}

fn public_https_endpoint_with<F>(url: &Url, resolver: F) -> Result<PublicEndpoint, OciError>
where
    F: FnOnce(&str, u16) -> Result<Vec<IpAddr>, OciError>,
{
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err(OciError::Artifact);
    }
    let hostname = url.host_str().ok_or(OciError::Artifact)?.to_owned();
    let port = url.port_or_known_default().ok_or(OciError::Artifact)?;
    let addresses = resolver(&hostname, port)?;
    if addresses.is_empty() || addresses.iter().any(|address| !is_public_ip(*address)) {
        return Err(OciError::Artifact);
    }
    Ok(PublicEndpoint {
        hostname,
        port,
        address: addresses[0],
    })
}

fn oci_endpoint(repository: &str) -> Result<PublicEndpoint, OciError> {
    if repository.contains("://")
        || repository.contains('@')
        || repository.contains('?')
        || repository.contains('#')
    {
        return Err(OciError::Artifact);
    }
    let url = Url::parse(&format!("https://{repository}")).map_err(|_| OciError::Artifact)?;
    if url.host_str() != Some("ghcr.io")
        || url.path() == "/"
        || url.path().contains("//")
        || url.path().contains("..")
    {
        return Err(OciError::Artifact);
    }
    public_https_endpoint(&url)
}

fn is_public_ip(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => {
            let octets = address.octets();
            !address.is_private()
                && !address.is_loopback()
                && !address.is_link_local()
                && !address.is_broadcast()
                && !address.is_documentation()
                && !address.is_multicast()
                && !address.is_unspecified()
                && octets[0] != 0
                && !(octets[0] == 100 && (64..=127).contains(&octets[1]))
                && !(octets[0] == 192 && octets[1] == 0 && octets[2] == 0)
                && !(octets[0] == 198 && matches!(octets[1], 18 | 19))
                && octets[0] < 240
        }
        IpAddr::V6(address) => {
            if let Some(mapped) = address.to_ipv4_mapped() {
                return is_public_ip(IpAddr::V4(mapped));
            }
            let segments = address.segments();
            // Fail closed to ordinary globally routed unicast space. Exclude
            // the IETF special-purpose blocks at the start of 2001::/16,
            // deprecated 6to4, and the documentation allocation.
            segments[0] & 0xe000 == 0x2000
                && !(segments[0] == 0x2001 && segments[1] < 0x0200)
                && !(segments[0] == 0x2001 && segments[1] == 0x0db8)
                && segments[0] != 0x2002
                && segments[0] != 0x3ffe
                && !(segments[0] == 0x3fff && segments[1] & 0xf000 == 0)
        }
    }
}

fn curl_arguments(
    url: &Url,
    destination: &Path,
    maximum_bytes: u64,
    endpoint: &PublicEndpoint,
) -> Vec<String> {
    let resolve_address = match endpoint.address {
        IpAddr::V4(address) => address.to_string(),
        IpAddr::V6(address) => format!("[{address}]"),
    };
    vec![
        "--fail".to_owned(),
        "--proto".to_owned(),
        "=https".to_owned(),
        "--tlsv1.3".to_owned(),
        "--max-redirs".to_owned(),
        "0".to_owned(),
        "--max-filesize".to_owned(),
        maximum_bytes.to_string(),
        "--resolve".to_owned(),
        format!(
            "{}:{}:{}",
            endpoint.hostname, endpoint.port, resolve_address
        ),
        "--connect-timeout".to_owned(),
        "15".to_owned(),
        "--retry".to_owned(),
        "3".to_owned(),
        "--retry-all-errors".to_owned(),
        "--write-out".to_owned(),
        "%{http_code}\t%{redirect_url}\n".to_owned(),
        "--output".to_owned(),
        destination.display().to_string(),
        url.as_str().to_owned(),
    ]
}

fn validate_curl_config(path: &Path) -> Result<(), OciError> {
    let metadata = fs::symlink_metadata(path)?;
    let effective_uid = rustix::process::geteuid().as_raw();
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() > 4096
        || !matches!(metadata.uid(), 0) && metadata.uid() != effective_uid
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(OciError::Artifact);
    }
    Ok(())
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

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn artifact_key(artifact: &crate::workloads::ArtifactSpec) -> Result<String, OciError> {
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(artifact)?)))
}

fn create_manifest(root: &Path) -> Result<ArtifactManifest, OciError> {
    let mut files = BTreeMap::new();
    let mut total = 0_u64;
    visit_files(root, root, &mut files, &mut total)?;
    if files.is_empty() {
        return Err(OciError::Artifact);
    }
    Ok(ArtifactManifest {
        schema_version: 1,
        files,
        total_bytes: total,
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
            if name == ".vonk-manifest.json" {
                continue;
            }
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

fn verify_manifest(root: &Path) -> Result<(), OciError> {
    let expected = read_manifest(root)?;
    let observed = create_manifest(root)?;
    if expected.files != observed.files || expected.total_bytes != observed.total_bytes {
        return Err(OciError::Artifact);
    }
    Ok(())
}

fn read_manifest(root: &Path) -> Result<ArtifactManifest, OciError> {
    let raw = fs::read(root.join(".vonk-manifest.json"))?;
    if raw.len() > 64 * 1024 {
        return Err(OciError::Artifact);
    }
    let manifest: ArtifactManifest = serde_json::from_slice(&raw)?;
    if manifest.schema_version != 1 {
        return Err(OciError::Artifact);
    }
    Ok(manifest)
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
    use super::{OciError, is_public_ip, public_https_endpoint_with};
    use std::{cell::RefCell, collections::VecDeque, net::IpAddr};
    use url::Url;

    #[test]
    fn only_globally_routable_ipv6_is_public() {
        for value in [
            "::1",
            "fe80::1",
            "fec0::1",
            "fc00::1",
            "2001:db8::1",
            "3ffe::1",
        ] {
            assert!(!is_public_ip(value.parse::<IpAddr>().unwrap()), "{value}");
        }
        assert!(is_public_ip("2606:4700:4700::1111".parse().unwrap()));
    }

    #[test]
    fn endpoint_validation_rejects_private_and_changed_dns_answers() {
        let url = Url::parse("https://models.example/weights").unwrap();
        let answers = RefCell::new(VecDeque::from([
            vec!["93.184.216.34".parse().unwrap()],
            vec!["127.0.0.1".parse().unwrap()],
        ]));
        let resolve = |_: &str, _: u16| answers.borrow_mut().pop_front().ok_or(OciError::Artifact);

        let endpoint = public_https_endpoint_with(&url, resolve).unwrap();
        assert_eq!(endpoint.address, "93.184.216.34".parse::<IpAddr>().unwrap());
        assert!(public_https_endpoint_with(&url, resolve).is_err());
    }
}
