use std::path::PathBuf;

use crate::compiled_execution_plan::{
    CompiledEndpoint, CompiledEnvironmentEntry, CompiledJob, CompiledLifecycle, CompiledTopology,
};

/// Host paths for the selected, materialized model files (the runtime image
/// is pulled into Docker by digest and has no host path).  Paths are lexical inputs to this pure projection; the caller
/// remains responsible for checking that receipts match bytes on disk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledOciPaths {
    pub model_root: PathBuf,
    pub input_root: Option<PathBuf>,
    pub output_root: PathBuf,
    pub cache_root: PathBuf,
    pub runtime_spec: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OciImageReceipt {
    pub image_digest: String,
    pub local_image_config_id: String,
    pub runtime_interface_label: String,
    pub oci_layout_sha256: String,
    pub image_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OciMount {
    pub source: PathBuf,
    pub target: String,
    pub read_only: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OciSecurityOptions {
    pub user: String,
    pub devices: Vec<String>,
    /// Network mode is bound to the signed endpoint and native fabric placement.
    pub network_mode: OciNetworkMode,
    pub memory_bytes: u64,
    pub shared_memory_bytes: u64,
    pub pids_limit: u64,
    pub tmpfs: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OciNetworkMode {
    None,
    Bridge,
    Host,
}

impl OciNetworkMode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Bridge => "bridge",
            Self::Host => "host",
        }
    }
}

/// All pieces needed by the production executor to launch one compiled plan.
/// `command` is passed directly to Podman after the image.  It is never parsed
/// as a shell command, and every item in `runtime.argv` is preserved verbatim.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledOciInvocation {
    pub image_receipt: OciImageReceipt,
    pub image: String,
    pub command: Vec<String>,
    pub environment: Vec<CompiledEnvironmentEntry>,
    pub mounts: Vec<OciMount>,
    pub publishes: Vec<String>,
    pub detach: bool,
    pub security: OciSecurityOptions,
    pub topology: CompiledTopology,
    pub lifecycle: CompiledLifecycle,
    pub endpoint: Option<CompiledEndpoint>,
    pub job: Option<CompiledJob>,
}

impl CompiledOciInvocation {
    /// Build the complete direct `podman run` argument vector.  A caller that
    /// needs a run-specific `--name` can insert it before these options; this
    /// method intentionally has no run-id or shell input.
    pub fn podman_arguments(&self) -> Vec<String> {
        let mut arguments = vec!["run".to_owned()];
        if self.detach {
            arguments.push("--detach".to_owned());
        }
        arguments.extend([
            "--read-only".to_owned(),
            "--tmpfs".to_owned(),
            self.security.tmpfs.clone(),
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
            "--user".to_owned(),
            self.security.user.clone(),
            "--memory".to_owned(),
            self.security.memory_bytes.to_string(),
            "--memory-swap".to_owned(),
            self.security.memory_bytes.to_string(),
            "--shm-size".to_owned(),
            self.security.shared_memory_bytes.to_string(),
            "--pids-limit".to_owned(),
            self.security.pids_limit.to_string(),
        ]);
        arguments.extend([
            "--network".to_owned(),
            self.security.network_mode.as_str().to_owned(),
        ]);
        for device in &self.security.devices {
            arguments.extend(["--device".to_owned(), device.clone()]);
        }
        if self.security.network_mode == OciNetworkMode::Host {
            arguments.extend([
                "--device".to_owned(),
                "/dev/infiniband:/dev/infiniband".to_owned(),
                "--ulimit".to_owned(),
                "memlock=-1:-1".to_owned(),
                "--ulimit".to_owned(),
                "stack=67108864:67108864".to_owned(),
            ]);
        }
        for environment in &self.environment {
            arguments.extend([
                "--env".to_owned(),
                format!("{}={}", environment.name, environment.value),
            ]);
        }
        for publish in &self.publishes {
            arguments.extend(["--publish".to_owned(), publish.clone()]);
        }
        for mount in &self.mounts {
            let mut value = format!(
                "type=bind,src={},dst={}",
                mount.source.display(),
                mount.target
            );
            if mount.read_only {
                value.push_str(",readonly");
            }
            arguments.extend(["--mount".to_owned(), value]);
        }
        let Some(executable) = self.command.first() else {
            return arguments;
        };
        arguments.extend(["--entrypoint".to_owned(), executable.clone()]);
        arguments.push(self.image.clone());
        // Keep the executable as the first post-image argument as well as the
        // explicit entrypoint. The privileged helper validates this exact
        // argv shape before it permits a start, and the remaining argv items
        // stay opaque data supplied by the compiled plan.
        arguments.extend(self.command.iter().cloned());
        arguments
    }
}
