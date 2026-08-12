use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::Duration;

use ring::signature;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::{
    HostRuntimeAction, HostRuntimeRequest, canonical_json, hex_sha256, parse_strict,
};
use wait_timeout::ChildExt;

use crate::protocol::{
    AgentSlot, ContainerRuntimeAction, HostOperation, ManagedArea, RestartUnit,
    artifact_signing_bytes,
};

const MAX_ARTIFACT_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_RUNTIME_ARCHIVE_BYTES: u64 = 1024 * 1024 * 1024 * 1024;
const MAX_COMMAND_OUTPUT_BYTES: u64 = 4096;
const MAX_RUNTIME_REQUEST_BYTES: u64 = 64 * 1024;

#[derive(Debug, Error)]
pub enum OperationError {
    #[error("managed operation is invalid")]
    InvalidOperation,
    #[error("managed path is unsafe")]
    UnsafePath,
    #[error("artifact verification failed")]
    InvalidArtifact,
    #[error("compiled command failed")]
    CommandFailed,
    #[error("host mutation failed")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone)]
pub struct ManagedRoots {
    pub data: PathBuf,
    pub models: PathBuf,
    pub state: PathBuf,
    pub workloads: PathBuf,
    pub slots: PathBuf,
    pub incoming: PathBuf,
    pub runtime_requests: PathBuf,
    pub runtime_image_receipts: PathBuf,
    pub agent_data: PathBuf,
}

impl ManagedRoots {
    pub fn under(data: &Path) -> Self {
        Self {
            data: data.to_path_buf(),
            models: data.join("models"),
            state: data.join("state"),
            workloads: data.join("workloads"),
            slots: data.join("slots"),
            incoming: data.join("incoming"),
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
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub success: bool,
    pub stdout: Vec<u8>,
}

pub trait CommandRunner: Send + Sync {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String>;
}

#[derive(Debug, Clone, Copy)]
pub struct ProcessCommandRunner;

impl CommandRunner for ProcessCommandRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        if !matches!(
            executable.to_str(),
            Some(
                "/usr/bin/dpkg-deb"
                    | "/usr/bin/dpkg"
                    | "/usr/bin/systemctl"
                    | "/usr/bin/systemd-run"
                    | "/usr/lib/vonk-forge/vonk-agent-supervisor"
                    | "/usr/bin/docker"
                    | "/usr/bin/setfacl"
            )
        ) {
            return Err("executable is not compiled into the helper".to_owned());
        }
        let capture_output = matches!(
            executable.to_str(),
            Some("/usr/bin/dpkg-deb" | "/usr/bin/docker")
        );
        let mut command = Command::new(executable);
        command
            .args(arguments)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("PATH", "/usr/bin:/bin")
            .current_dir("/")
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .stdout(if capture_output {
                Stdio::piped()
            } else {
                Stdio::null()
            });
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
        let timeout = if executable == Path::new("/usr/bin/dpkg") {
            Duration::from_secs(120)
        } else if executable == Path::new("/usr/bin/docker") {
            Duration::from_secs(600)
        } else {
            Duration::from_secs(30)
        };
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
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OperationOutcome {
    pub schema_version: u8,
    pub status: String,
    pub evidence_sha256: String,
}

pub struct OperationExecutor<R> {
    roots: ManagedRoots,
    release_public_key: [u8; 32],
    runner: R,
    required_owner_uid: Option<u32>,
    runtime_request_owner_uid: Option<u32>,
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
        if !roots.data.is_absolute()
            || ![
                &roots.models,
                &roots.state,
                &roots.workloads,
                &roots.slots,
                &roots.incoming,
            ]
            .iter()
            .all(|path| path.starts_with(&roots.data))
        {
            return Err(OperationError::UnsafePath);
        }
        Ok(Self {
            roots,
            release_public_key,
            runner,
            required_owner_uid,
            runtime_request_owner_uid: required_owner_uid,
        })
    }

    pub fn with_runtime_request_owner(mut self, uid: u32) -> Self {
        self.runtime_request_owner_uid = Some(uid);
        self
    }

    pub fn execute(&self, operation: &HostOperation) -> Result<OperationOutcome, OperationError> {
        operation
            .validate()
            .map_err(|_| OperationError::InvalidOperation)?;
        self.require_directory(&self.roots.data)?;
        let (status, evidence) = match operation {
            HostOperation::CreateManagedDirectory {
                area,
                relative_path,
            } => {
                let path = self.create_managed_directory(area, relative_path)?;
                ("directory-created", path.to_string_lossy().into_owned())
            }
            HostOperation::ActivateAgentSlot {
                slot,
                artifact_sha256,
                artifact_signature,
            } => {
                self.activate_slot(slot, artifact_sha256, artifact_signature)?;
                ("slot-activated", artifact_sha256.clone())
            }
            HostOperation::InstallVonkDeb {
                package_sha256,
                package_signature,
            } => {
                self.install_package(package_sha256, package_signature)?;
                ("package-installed", package_sha256.clone())
            }
            HostOperation::RestartVonkUnit { unit } => {
                let unit_name = self.restart_unit(unit)?;
                ("unit-restarted", unit_name.to_owned())
            }
            HostOperation::ScheduleReboot { delay_seconds } => {
                self.schedule_reboot(*delay_seconds)?;
                ("reboot-scheduled", delay_seconds.to_string())
            }
            HostOperation::ExecuteContainerRuntimeRequest {
                action,
                job_id,
                operation_id,
                attempt,
                fence,
                request_sha256,
            } => {
                self.execute_runtime_request(
                    action,
                    job_id,
                    operation_id,
                    *attempt,
                    fence,
                    request_sha256,
                )?;
                ("container-runtime-request-executed", request_sha256.clone())
            }
        };
        Ok(OperationOutcome {
            schema_version: 1,
            status: status.to_owned(),
            evidence_sha256: hex_sha256(evidence.as_bytes()),
        })
    }

    fn create_managed_directory(
        &self,
        area: &ManagedArea,
        relative_path: &str,
    ) -> Result<PathBuf, OperationError> {
        let root = match area {
            ManagedArea::Models => &self.roots.models,
            ManagedArea::State => &self.roots.state,
            ManagedArea::Workloads => &self.roots.workloads,
        };
        let canonical_root = fs::canonicalize(root).map_err(|_| OperationError::UnsafePath)?;
        self.require_directory(root)?;
        let relative = Path::new(relative_path);
        if relative.is_absolute()
            || relative
                .components()
                .any(|component| !matches!(component, Component::Normal(_)))
        {
            return Err(OperationError::UnsafePath);
        }
        let mut current = root.clone();
        for component in relative.components() {
            let Component::Normal(component) = component else {
                return Err(OperationError::UnsafePath);
            };
            current.push(component);
            match fs::symlink_metadata(&current) {
                Ok(metadata) => {
                    if metadata.file_type().is_symlink() || !metadata.is_dir() {
                        return Err(OperationError::UnsafePath);
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    fs::create_dir(&current)?;
                    fs::set_permissions(&current, fs::Permissions::from_mode(0o750))?;
                }
                Err(error) => return Err(OperationError::Io(error)),
            }
            let canonical = fs::canonicalize(&current).map_err(|_| OperationError::UnsafePath)?;
            if !canonical.starts_with(&canonical_root) {
                return Err(OperationError::UnsafePath);
            }
            self.require_directory(&current)?;
        }
        sync_directory(root)?;
        Ok(current)
    }

    fn activate_slot(
        &self,
        slot: &AgentSlot,
        digest: &str,
        detached_signature: &str,
    ) -> Result<(), OperationError> {
        self.require_directory(&self.roots.slots)?;
        let slot_name = match slot {
            AgentSlot::A => "a",
            AgentSlot::B => "b",
        };
        let artifact = self.roots.slots.join(slot_name).join("vonk-agent");
        self.verify_artifact(&artifact, "agent", digest, detached_signature)?;

        let result = self
            .runner
            .run(
                Path::new("/usr/lib/vonk-forge/vonk-agent-supervisor"),
                &[
                    "activate".to_owned(),
                    "--slot".to_owned(),
                    slot_name.to_owned(),
                    "--sha256".to_owned(),
                    digest.to_owned(),
                ],
            )
            .map_err(|_| OperationError::CommandFailed)?;
        if !result.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(())
    }

    fn install_package(
        &self,
        digest: &str,
        detached_signature: &str,
    ) -> Result<(), OperationError> {
        self.require_directory(&self.roots.incoming)?;
        let package = self.roots.incoming.join(format!("{digest}.deb"));
        self.verify_artifact(&package, "deb", digest, detached_signature)?;
        let package_name = package.to_string_lossy().into_owned();
        self.require_field(&package_name, "Package", "vonk-forge-agent")?;
        self.require_field(&package_name, "Architecture", "arm64")?;
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
            .map_err(|_| OperationError::CommandFailed)?;
        if !result.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(())
    }

    fn require_field(
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
            .map_err(|_| OperationError::CommandFailed)?;
        if !result.success || result.stdout != format!("{expected}\n").as_bytes() {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(())
    }

    fn restart_unit(&self, unit: &RestartUnit) -> Result<&'static str, OperationError> {
        let unit = match unit {
            RestartUnit::Agent => "vonk-forge-agent.service",
            RestartUnit::Supervisor => "vonk-forge-agent-supervisor.service",
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

    fn schedule_reboot(&self, delay_seconds: u16) -> Result<(), OperationError> {
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
        job_id: &uuid::Uuid,
        operation_id: &uuid::Uuid,
        attempt: u32,
        fence: &uuid::Uuid,
        request_sha256: &str,
    ) -> Result<(), OperationError> {
        let request = self.read_runtime_request(request_sha256)?;
        let expected_action = match action {
            ContainerRuntimeAction::ImageImport => HostRuntimeAction::ImageImport,
            ContainerRuntimeAction::ImageInspect => HostRuntimeAction::ImageInspect,
            ContainerRuntimeAction::Start => HostRuntimeAction::Start,
            ContainerRuntimeAction::Stop => HostRuntimeAction::Stop,
        };
        if request.action != expected_action
            || &request.job_id != job_id
            || &request.operation_id != operation_id
            || request.attempt != attempt
            || &request.fence != fence
        {
            return Err(OperationError::InvalidOperation);
        }
        match request.action {
            HostRuntimeAction::ImageImport => {
                self.runtime_image_import(request.operation_id, &request.arguments)
            }
            HostRuntimeAction::ImageInspect => self.runtime_image_inspect(&request.arguments),
            HostRuntimeAction::Start => self.runtime_start(&request.arguments),
            HostRuntimeAction::Stop => {
                if request.arguments.get(1).map(String::as_str) == Some("run") {
                    self.runtime_start(&request.arguments)
                } else {
                    self.runtime_stop(&request.arguments)
                }
            }
        }
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

    fn runtime_image_import(
        &self,
        operation_id: uuid::Uuid,
        arguments: &[String],
    ) -> Result<(), OperationError> {
        let [archive, archive_sha256, archive_bytes, image_digest, image] = arguments else {
            return Err(OperationError::InvalidOperation);
        };
        let archive = Path::new(archive);
        let import_root = self.roots.agent_data.join("image-imports");
        let expected_parent = import_root.join(operation_id.to_string());
        let canonical_import_root =
            fs::canonicalize(&import_root).map_err(|_| OperationError::InvalidArtifact)?;
        let canonical_parent =
            fs::canonicalize(&expected_parent).map_err(|_| OperationError::InvalidArtifact)?;
        if !archive.is_absolute()
            || archive.parent() != Some(expected_parent.as_path())
            || canonical_parent.parent() != Some(canonical_import_root.as_path())
            || archive.file_name().and_then(|value| value.to_str()) != Some("image.docker.tar")
            || !lower_hex(archive_sha256, 64)
            || !valid_oci_digest(image_digest)
            || !valid_local_image(image)
        {
            return Err(OperationError::InvalidOperation);
        }
        let expected_bytes = archive_bytes
            .parse::<u64>()
            .ok()
            .filter(|value| (1..=MAX_RUNTIME_ARCHIVE_BYTES).contains(value))
            .ok_or(OperationError::InvalidOperation)?;
        self.verify_runtime_archive(archive, archive_sha256, expected_bytes)?;
        let loaded = self.run_docker(&[
            "load".to_owned(),
            "--input".to_owned(),
            archive.display().to_string(),
        ])?;
        if !loaded.success
            || !std::str::from_utf8(&loaded.stdout)
                .ok()
                .is_some_and(|value| {
                    value
                        .lines()
                        .any(|line| line == format!("Loaded image: {image}:latest"))
                })
        {
            return Err(OperationError::CommandFailed);
        }
        let inspected = self.inspect_runtime_image(image)?;
        if inspected.1 != "linux"
            || inspected.2 != "arm64"
            || inspected.3 != "v1"
            || !numeric_non_root_user(&inspected.4)
        {
            return Err(OperationError::InvalidArtifact);
        }
        self.write_image_receipt(image_digest, image, &inspected.0)
    }

    fn runtime_image_inspect(&self, arguments: &[String]) -> Result<(), OperationError> {
        let [image_reference, image_digest, user] = arguments else {
            return Err(OperationError::InvalidOperation);
        };
        let (image, embedded_digest) = parse_local_image_reference(image_reference)?;
        if &embedded_digest != image_digest || !numeric_non_root_user(user) {
            return Err(OperationError::InvalidOperation);
        }
        let inspected = self.inspect_runtime_image(&image)?;
        if inspected.1 != "linux"
            || inspected.2 != "arm64"
            || inspected.3 != "v1"
            || inspected.4 != *user
        {
            return Err(OperationError::InvalidArtifact);
        }
        self.require_image_receipt(image_digest, &image, &inspected.0)
    }

    fn runtime_start(&self, arguments: &[String]) -> Result<(), OperationError> {
        let (image_digest, docker) = arguments
            .split_first()
            .ok_or(OperationError::InvalidOperation)?;
        let validated = validate_docker_run(docker, &self.roots)?;
        if image_digest != &validated.image_digest {
            return Err(OperationError::InvalidOperation);
        }
        let inspected = self.inspect_runtime_image(&validated.image)?;
        self.require_image_receipt(image_digest, &validated.image, &inspected.0)?;
        let semantic_digest = hex_sha256(
            &canonical_json(&validated.arguments).map_err(|_| OperationError::InvalidOperation)?,
        );
        if validated.detached {
            let existing = self.run_docker(&[
                "container".to_owned(),
                "inspect".to_owned(),
                "--format".to_owned(),
                "{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.runtime-request-sha256\"}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}".to_owned(),
                format!("vonk-{}", validated.run_id),
            ])?;
            if existing.success {
                let expected = format!("true\t{semantic_digest}\ttrue\t{}", validated.run_id);
                if std::str::from_utf8(&existing.stdout).ok().map(str::trim)
                    != Some(expected.as_str())
                {
                    return Err(OperationError::InvalidArtifact);
                }
                return Ok(());
            }
        }
        self.prepare_runtime_access(&validated)?;
        let mut compiled = validated.arguments.clone();
        if validated.detached {
            compiled.splice(
                validated.image_index..validated.image_index,
                [
                    "--label".to_owned(),
                    format!("ai.vonkforge.runtime-request-sha256={semantic_digest}"),
                    "--label".to_owned(),
                    "ai.vonkforge.managed=true".to_owned(),
                    "--label".to_owned(),
                    format!("ai.vonkforge.run-id={}", validated.run_id),
                ],
            );
        }
        let output = self.run_docker(&compiled)?;
        let identifier = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .unwrap_or("");
        if !output.success || validated.detached && !lower_hex(identifier, 64) {
            return Err(OperationError::CommandFailed);
        }
        Ok(())
    }

    fn prepare_runtime_access(&self, run: &ValidatedDockerRun) -> Result<(), OperationError> {
        for (path, access) in [
            (&run.models, "rX"),
            (&run.state, "rwx"),
            (&run.runtime_contract, "r"),
        ] {
            let output = self
                .runner
                .run(
                    Path::new("/usr/bin/setfacl"),
                    &[
                        if path == &run.models { "-R" } else { "-m" }.to_owned(),
                        if path == &run.models { "-m" } else { "--" }.to_owned(),
                        format!("u:{}:{access}", run.uid),
                        path.display().to_string(),
                    ]
                    .into_iter()
                    .filter(|value| value != "--")
                    .collect::<Vec<_>>(),
                )
                .map_err(|_| OperationError::CommandFailed)?;
            if !output.success {
                return Err(OperationError::CommandFailed);
            }
        }
        Ok(())
    }

    fn runtime_stop(&self, arguments: &[String]) -> Result<(), OperationError> {
        let [run_id, timeout] = arguments else {
            return Err(OperationError::InvalidOperation);
        };
        let timeout = timeout
            .parse::<u16>()
            .ok()
            .filter(|value| (1..=600).contains(value))
            .ok_or(OperationError::InvalidOperation)?;
        if uuid::Uuid::parse_str(run_id)
            .ok()
            .map(|value| value.to_string())
            != Some(run_id.clone())
        {
            return Err(OperationError::InvalidOperation);
        }
        let name = format!("vonk-{run_id}");
        let existing = self.run_docker(&[
            "container".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}".to_owned(),
            name.clone(),
        ])?;
        if !existing.success {
            let daemon = self.run_docker(&[
                "version".to_owned(),
                "--format".to_owned(),
                "{{.Server.Version}}".to_owned(),
            ])?;
            return if daemon.success {
                Ok(())
            } else {
                Err(OperationError::CommandFailed)
            };
        }
        let expected = format!("true\t{run_id}");
        if std::str::from_utf8(&existing.stdout).ok().map(str::trim) != Some(expected.as_str()) {
            return Err(OperationError::InvalidArtifact);
        }
        let output = self.run_docker(&[
            "rm".to_owned(),
            "--force".to_owned(),
            "--time".to_owned(),
            timeout.to_string(),
            name,
        ])?;
        if !output.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(())
    }

    fn run_docker(&self, arguments: &[String]) -> Result<CommandOutput, OperationError> {
        self.runner
            .run(Path::new("/usr/bin/docker"), arguments)
            .map_err(|_| OperationError::CommandFailed)
    }

    fn inspect_runtime_image(
        &self,
        image: &str,
    ) -> Result<(String, String, String, String, String), OperationError> {
        let output = self.run_docker(&[
            "image".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{.Id}}\t{{.Os}}\t{{.Architecture}}\t{{index .Config.Labels \"ai.vonkforge.runtime-interface\"}}\t{{.Config.User}}".to_owned(),
            image.to_owned(),
        ])?;
        let fields = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .map(|value| value.split('\t').map(str::to_owned).collect::<Vec<_>>())
            .unwrap_or_default();
        if !output.success || fields.len() != 5 || !valid_oci_digest(&fields[0]) {
            return Err(OperationError::InvalidArtifact);
        }
        Ok((
            fields[0].clone(),
            fields[1].clone(),
            fields[2].clone(),
            fields[3].clone(),
            fields[4].clone(),
        ))
    }

    fn write_image_receipt(
        &self,
        image_digest: &str,
        image: &str,
        image_id: &str,
    ) -> Result<(), OperationError> {
        fs::create_dir_all(&self.roots.runtime_image_receipts)?;
        fs::set_permissions(
            &self.roots.runtime_image_receipts,
            fs::Permissions::from_mode(0o700),
        )?;
        let path = self
            .roots
            .runtime_image_receipts
            .join(image_digest.trim_start_matches("sha256:"));
        let body = format!("{image}\n{image_id}\n");
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                file.write_all(body.as_bytes())?;
                file.sync_all()?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                if fs::read_to_string(&path)? != body {
                    return Err(OperationError::InvalidArtifact);
                }
            }
            Err(error) => return Err(error.into()),
        }
        sync_directory(&self.roots.runtime_image_receipts)
    }

    fn require_image_receipt(
        &self,
        image_digest: &str,
        image: &str,
        image_id: &str,
    ) -> Result<(), OperationError> {
        if !valid_oci_digest(image_digest) {
            return Err(OperationError::InvalidOperation);
        }
        let path = self
            .roots
            .runtime_image_receipts
            .join(image_digest.trim_start_matches("sha256:"));
        let expected = format!("{image}\n{image_id}\n");
        let metadata = fs::symlink_metadata(&path).map_err(|_| OperationError::InvalidArtifact)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
            || metadata.nlink() != 1
            || metadata.mode() & 0o022 != 0
            || metadata.len() > 2048
            || fs::read_to_string(path).map_err(|_| OperationError::InvalidArtifact)? != expected
        {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(())
    }

    fn verify_runtime_archive(
        &self,
        path: &Path,
        expected_digest: &str,
        expected_bytes: u64,
    ) -> Result<(), OperationError> {
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
        let mut file = File::open(path).map_err(|_| OperationError::InvalidArtifact)?;
        let before = file
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        let mut digest = Sha256::new();
        let mut consumed = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = file
                .read(&mut buffer)
                .map_err(|_| OperationError::InvalidArtifact)?;
            if count == 0 {
                break;
            }
            consumed += count as u64;
            digest.update(&buffer[..count]);
        }
        let after = file
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        if consumed != expected_bytes
            || stable_identity(&before) != stable_identity(&after)
            || hex::encode(digest.finalize()) != expected_digest
        {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(())
    }

    fn verify_artifact(
        &self,
        path: &Path,
        kind: &str,
        expected_digest: &str,
        detached_signature: &str,
    ) -> Result<(), OperationError> {
        let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::InvalidArtifact)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.len() == 0
            || metadata.len() > MAX_ARTIFACT_BYTES
            || metadata.mode() & 0o022 != 0
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(OperationError::InvalidArtifact);
        }
        let mut file = File::open(path).map_err(|_| OperationError::InvalidArtifact)?;
        let before = file
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        let mut digest = Sha256::new();
        let mut consumed = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = file
                .read(&mut buffer)
                .map_err(|_| OperationError::InvalidArtifact)?;
            if count == 0 {
                break;
            }
            consumed += count as u64;
            if consumed > MAX_ARTIFACT_BYTES {
                return Err(OperationError::InvalidArtifact);
            }
            digest.update(&buffer[..count]);
        }
        let after = file
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        if stable_identity(&before) != stable_identity(&after)
            || hex::encode(digest.finalize()) != expected_digest
        {
            return Err(OperationError::InvalidArtifact);
        }
        let signature_bytes =
            hex::decode(detached_signature).map_err(|_| OperationError::InvalidArtifact)?;
        signature::UnparsedPublicKey::new(&signature::ED25519, self.release_public_key)
            .verify(
                &artifact_signing_bytes(kind, expected_digest)
                    .map_err(|_| OperationError::InvalidArtifact)?,
                &signature_bytes,
            )
            .map_err(|_| OperationError::InvalidArtifact)
    }

    fn require_directory(&self, path: &Path) -> Result<(), OperationError> {
        let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || metadata.mode() & 0o022 != 0
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(OperationError::UnsafePath);
        }
        Ok(())
    }
}

struct ValidatedDockerRun {
    image: String,
    image_digest: String,
    arguments: Vec<String>,
    detached: bool,
    image_index: usize,
    run_id: String,
    uid: u32,
    models: PathBuf,
    state: PathBuf,
    runtime_contract: PathBuf,
}

fn validate_docker_run(
    arguments: &[String],
    roots: &ManagedRoots,
) -> Result<ValidatedDockerRun, OperationError> {
    if arguments.first().map(String::as_str) != Some("run") {
        return Err(OperationError::InvalidOperation);
    }
    let mut index = 1;
    let mut detach = false;
    let mut remove = false;
    let mut name: Option<String> = None;
    let mut restart = false;
    let mut read_only = false;
    let mut init = false;
    let mut pull_never = false;
    let mut local_logging = false;
    let mut log_max_size = false;
    let mut log_max_file = false;
    let mut cap_drop = false;
    let mut no_new_privileges = false;
    let mut network = false;
    let mut pids = false;
    let mut memory: Option<u64> = None;
    let mut memory_swap: Option<u64> = None;
    let mut shm_size: Option<u64> = None;
    let mut user: Option<(u32, Option<u32>)> = None;
    let mut publishes = 0_usize;
    let mut environments = 0_usize;
    let mut gpu = false;
    let mut models = None;
    let mut state = None;
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
            "--restart" if !restart => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("no") {
                    return Err(OperationError::InvalidOperation);
                }
                restart = true;
            }
            "--network" if !network => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("bridge") {
                    return Err(OperationError::InvalidOperation);
                }
                network = true;
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
                if !valid_publication(
                    arguments
                        .get(index)
                        .ok_or(OperationError::InvalidOperation)?,
                ) {
                    return Err(OperationError::InvalidOperation);
                }
                publishes += 1;
            }
            "--env" if environments < 160 => {
                index += 1;
                if !valid_environment(
                    arguments
                        .get(index)
                        .ok_or(OperationError::InvalidOperation)?,
                ) {
                    return Err(OperationError::InvalidOperation);
                }
                environments += 1;
            }
            "--mount" => {
                index += 1;
                let value = arguments
                    .get(index)
                    .ok_or(OperationError::InvalidOperation)?;
                let (source, target, readonly) = parse_mount(value)?;
                if target == "/models"
                    && readonly
                    && source == roots.agent_data.join("models")
                    && models.is_none()
                {
                    models = Some(source);
                } else if target == "/state"
                    && !readonly
                    && source.starts_with(roots.agent_data.join("runs"))
                    && state.is_none()
                {
                    state = Some(source);
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
            "--gpus" if !gpu => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("all") {
                    return Err(OperationError::InvalidOperation);
                }
                gpu = true;
            }
            _ => return Err(OperationError::InvalidOperation),
        }
        index += 1;
    }
    let image_reference = arguments
        .get(index)
        .cloned()
        .ok_or(OperationError::InvalidOperation)?;
    let (image, embedded_digest) = parse_local_image_reference(&image_reference)?;
    let (uid, _gid) = user.ok_or(OperationError::InvalidOperation)?;
    let state = state.ok_or(OperationError::InvalidOperation)?;
    let runtime_contract = runtime_contract.ok_or(OperationError::InvalidOperation)?;
    let state_run_id = state
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or(OperationError::InvalidOperation)?;
    let named_run_id = name.as_deref();
    if !((detach && !remove && restart && named_run_id == Some(state_run_id))
        || (!detach && remove && !restart && named_run_id.is_none()))
        || !read_only
        || !init
        || !pull_never
        || !local_logging
        || !log_max_size
        || !log_max_file
        || !cap_drop
        || !no_new_privileges
        || !network
        || !pids
        || memory.is_none()
        || memory_swap != memory
        || shm_size.is_none_or(|value| value > memory.unwrap_or_default())
        || (detach && publishes == 0)
        || (!detach && publishes != 0)
        || environments == 0
        || state.parent() != Some(roots.agent_data.join("runs").as_path())
        || runtime_contract.parent().and_then(Path::parent)
            != Some(roots.agent_data.join("run-metadata").as_path())
        || runtime_contract
            .parent()
            .and_then(Path::file_name)
            .and_then(|value| value.to_str())
            != Some(state_run_id)
    {
        return Err(OperationError::InvalidOperation);
    }
    let models = models.ok_or(OperationError::InvalidOperation)?;
    for path in [&models, &state, &runtime_contract] {
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
    let mut compiled_arguments = arguments.to_vec();
    compiled_arguments[index] = image.clone();
    Ok(ValidatedDockerRun {
        image,
        image_digest: embedded_digest,
        arguments: compiled_arguments,
        detached: detach,
        image_index: index,
        run_id: state_run_id.to_owned(),
        uid,
        models,
        state,
        runtime_contract,
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

fn valid_publication(value: &str) -> bool {
    let (address, ports) = if let Some(value) = value.strip_prefix('[') {
        let Some((address, ports)) = value.split_once("]:") else {
            return false;
        };
        (address, ports)
    } else {
        let Some((address, ports)) = value.split_once(':') else {
            return false;
        };
        (address, ports)
    };
    let Ok(std::net::IpAddr::V4(address)) = address.parse::<std::net::IpAddr>() else {
        return false;
    };
    if address.is_unspecified()
        || address.is_loopback()
        || address.is_multicast()
        || address.is_link_local()
    {
        return false;
    }
    let Some((host, container)) = ports.split_once(':') else {
        return false;
    };
    if container.contains(':') {
        return false;
    }
    [host, container].iter().all(|part| {
        part.parse::<u16>()
            .is_ok_and(|port| (1024..=65535).contains(&port))
    })
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
    value
        .strip_prefix("localhost/vonk/recipe-build-")
        .and_then(|value| uuid::Uuid::parse_str(value).ok().map(|id| (value, id)))
        .is_some_and(|(value, id)| id.to_string() == value)
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

fn sync_directory(path: &Path) -> Result<(), OperationError> {
    OpenOptions::new().read(true).open(path)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::valid_publication;

    #[test]
    fn runtime_publications_require_an_explicit_routable_bind_address() {
        assert!(valid_publication("192.168.1.211:8101:8000"));
        assert!(valid_publication("192.168.100.10:29500:29500"));

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
            assert!(!valid_publication(value), "{value}");
        }
    }
}
