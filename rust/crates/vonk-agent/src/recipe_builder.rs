//! Rootless, typed recipe image build execution.

use std::{
    fmt,
    fs::{self, File},
    io::{Seek, SeekFrom},
    os::unix::{
        ffi::OsStrExt,
        fs::{MetadataExt, PermissionsExt},
    },
    path::Path,
    time::Duration,
};

use serde::Serialize;
use sha2::{Digest, Sha256};
use tempfile::{Builder, TempDir};
use thiserror::Error;
use uuid::Uuid;
use vonk_agent_protocol::RecipeBuildRequest;

use crate::{
    base_images::{BaseImageError, BaseImageStore},
    build_source::{BuildSourceError, materialize_source_bundle},
    process::{ProcessError, ProcessInputBounds, ProcessOutputBounds, ProcessRunner, Program},
    source_policy::{SourcePolicyReport, dockerfile_base_images, inspect_build_source},
};

const MAX_EGRESS_BINARY_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Debug, Error)]
pub enum RecipeBuildError {
    #[error("source bundle failed canonical verification")]
    Source(#[from] BuildSourceError),
    #[error("source policy rejected the build")]
    Policy(SourcePolicyReport),
    #[error("rootless image build failed")]
    Process(#[from] ProcessError),
    #[error("build evidence is invalid")]
    Evidence,
    #[error("base image registry content is invalid")]
    BaseImageContent,
    #[error("base image manifest transfer or evidence failed")]
    BaseImageManifest,
    #[error("base image blob transfer or evidence failed")]
    BaseImageBlob,
    #[error("base image OCI archive verification failed")]
    BaseImageArchive,
    #[error("Podman could not import the verified base image ({diagnostic})")]
    BaseImageImport { diagnostic: PodmanImportDiagnostic },
    #[error("Podman imported base image evidence is invalid")]
    BaseImageInspect,
    #[error("Podman recipe image build failed ({diagnostic})")]
    ImageBuild { diagnostic: PodmanBuildDiagnostic },
    #[error("built recipe image evidence is invalid")]
    ImageInspect,
    #[error("Podman could not export the built recipe image")]
    ImageExport,
    #[error("build output exceeded its declared limit")]
    OutputLimit,
    #[error(
        "source build network policy is unsupported: public host allowlists require an installed egress boundary"
    )]
    NetworkPolicy,
    #[error("build storage is unavailable")]
    Io(#[from] std::io::Error),
}

/// Stable, secret-free evidence extracted from bounded Podman output. Raw
/// subprocess output can contain host paths and registry details, so operation
/// results expose only this reviewed classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PodmanImportDiagnostic {
    TemporaryStorageExhausted,
    DeclaredStorageLimitExceeded,
    SubordinateIdMappingUnavailable,
    ArchiveFormatRejected,
    PermissionDenied,
    DeadlineExceeded,
    DiagnosticOutputLimitExceeded,
    SubprocessUnavailable,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PodmanBuildDiagnostic {
    TemporaryStorageExhausted,
    SubordinateIdMappingUnavailable,
    PermissionDenied,
    MemoryLimitExceeded,
    StorageDriverFailure,
    SystemdScopeFailure,
    NonzeroWithoutOutput,
    Unknown,
}

impl fmt::Display for PodmanBuildDiagnostic {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::TemporaryStorageExhausted => "temporary-storage-exhausted",
            Self::SubordinateIdMappingUnavailable => "subordinate-id-mapping-unavailable",
            Self::PermissionDenied => "permission-denied",
            Self::MemoryLimitExceeded => "memory-limit-exceeded",
            Self::StorageDriverFailure => "storage-driver-failure",
            Self::SystemdScopeFailure => "systemd-scope-failure",
            Self::NonzeroWithoutOutput => "nonzero-without-output",
            Self::Unknown => "unclassified-podman-build-failure",
        })
    }
}

impl fmt::Display for PodmanImportDiagnostic {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::TemporaryStorageExhausted => "temporary-storage-exhausted",
            Self::DeclaredStorageLimitExceeded => "declared-storage-limit-exceeded",
            Self::SubordinateIdMappingUnavailable => "subordinate-id-mapping-unavailable",
            Self::ArchiveFormatRejected => "archive-format-rejected",
            Self::PermissionDenied => "permission-denied",
            Self::DeadlineExceeded => "deadline-exceeded",
            Self::DiagnosticOutputLimitExceeded => "diagnostic-output-limit-exceeded",
            Self::SubprocessUnavailable => "subprocess-unavailable",
            Self::Unknown => "unclassified-podman-load-failure",
        })
    }
}

impl RecipeBuildError {
    pub(crate) fn failure_evidence(&self) -> serde_json::Value {
        match self {
            Self::BaseImageImport { diagnostic } => serde_json::json!({
                "diagnostic": diagnostic.to_string(),
                "reason": self.to_string(),
                "stage": "base-image-import",
            }),
            Self::ImageBuild { diagnostic } => serde_json::json!({
                "diagnostic": diagnostic.to_string(),
                "reason": self.to_string(),
                "stage": "image-build",
            }),
            _ => serde_json::json!({"reason": self.to_string()}),
        }
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct RecipeBuildEvidence {
    pub build_input_sha256: String,
    pub image_bytes: u64,
    pub image_digest: String,
    // Protocol-v1 name retained for compatibility; Spark binds the complete
    // docker-save archive here.
    pub oci_layout_sha256: String,
    pub policy: SourcePolicyReport,
}

pub struct RecipeBuilder<'a, R> {
    pub runner: &'a R,
    pub data_root: &'a Path,
    pub runtime_root: &'a Path,
    pub egress_binary: &'a Path,
}

struct PodmanBuildStaging(TempDir);

impl PodmanBuildStaging {
    fn create(root: &Path) -> std::io::Result<Self> {
        Builder::new().prefix("source-").tempdir_in(root).map(Self)
    }

    fn path(&self) -> &Path {
        self.0.path()
    }
}

impl Drop for PodmanBuildStaging {
    fn drop(&mut self) {
        // Rootless Podman may leave overlay layer directories mode 0555. The
        // agent owns this operation-private tree, but TempDir cannot remove a
        // file from a non-writable directory and silently abandons the whole
        // graphroot. Restore owner traversal/removal before TempDir runs. Do
        // not follow symlinks created in container-image metadata.
        let _ = make_owned_directories_removable(self.path());
    }
}

impl<R: ProcessRunner> RecipeBuilder<'_, R> {
    pub fn layout_path(&self, operation_id: Uuid) -> std::path::PathBuf {
        self.data_root
            .join("builds")
            .join(operation_id.to_string())
            .join("image.docker.tar")
    }

    pub fn build(
        &self,
        request: &RecipeBuildRequest,
        operation_id: Uuid,
        archive: &[u8],
    ) -> Result<RecipeBuildEvidence, RecipeBuildError> {
        self.build_cancellable(request, operation_id, archive, &|| false)
    }

    pub fn build_cancellable(
        &self,
        request: &RecipeBuildRequest,
        operation_id: Uuid,
        archive: &[u8],
        cancelled: &dyn Fn() -> bool,
    ) -> Result<RecipeBuildEvidence, RecipeBuildError> {
        if cancelled() {
            return Err(ProcessError::Cancelled.into());
        }
        // `slirp4netns` only isolates the host network namespace; it does not
        // enforce a destination allowlist.  Never silently widen a declared
        // host policy into unrestricted egress. Public builds therefore get
        // an operation-private internal network and dual-homed proxy below.
        let network = build_network(&request.network)?;
        if network == BuildNetwork::Public
            && request.arguments.iter().any(|argument| {
                matches!(
                    argument.name.as_str(),
                    "HTTP_PROXY"
                        | "HTTPS_PROXY"
                        | "NO_PROXY"
                        | "http_proxy"
                        | "https_proxy"
                        | "no_proxy"
                )
            })
        {
            return Err(RecipeBuildError::NetworkPolicy);
        }
        let staging_root = self.data_root.join("build-staging");
        fs::create_dir_all(&staging_root)?;
        let staging = PodmanBuildStaging::create(&staging_root)?;
        let context = staging.path().join("context");
        let storage = staging.path().join("podman-storage");
        let podman_image_tmp = staging.path().join("podman-image-tmp");
        fs::create_dir_all(self.runtime_root)?;
        // Ubuntu 24.04 ships Podman 4.9, which rejects runroot path strings
        // longer than 50 bytes. Keep the durable, storage-accounted graphroot
        // under the build staging tree while putting only Podman's ephemeral
        // runtime metadata in the private systemd RuntimeDirectory.
        let runroot = Builder::new().prefix("b-").tempdir_in(self.runtime_root)?;
        if runroot.path().as_os_str().as_bytes().len() > 50 {
            return Err(RecipeBuildError::Evidence);
        }
        fs::create_dir_all(&storage)?;
        fs::create_dir(&podman_image_tmp)?;
        let source = materialize_source_bundle(archive, &request.source_bundle_sha256, &context)?;
        let policy = inspect_build_source(&source.files, &request.dockerfile);
        if !policy.passed {
            return Err(RecipeBuildError::Policy(policy));
        }
        let source_bases = dockerfile_base_images(&source.files, &request.dockerfile)
            .ok_or(RecipeBuildError::Evidence)?;
        if source_bases
            != request
                .base_images
                .iter()
                .map(|image| image.reference.clone())
                .collect::<Vec<_>>()
        {
            return Err(RecipeBuildError::Evidence);
        }
        self.import_base_images(request, &storage, runroot.path(), cancelled)?;
        let egress = match network {
            BuildNetwork::None => None,
            BuildNetwork::Public => Some(BuildEgress::start(
                self.runner,
                BuildEgressStart {
                    storage: &storage,
                    runroot: runroot.path(),
                    staging: staging.path(),
                    binary: self.egress_binary,
                    operation_id,
                    hosts: &request.network.hosts,
                    staging_limit: build_staging_limit(request)?,
                    cancelled,
                },
            )?),
        };
        let tag = format!("localhost/vonk/recipe-build-{}", request.build_id);
        // Rootless Podman 4.9 cannot reliably create a user-manager scope when
        // invoked by the agent's system service. Start the build itself in a
        // transient user scope, then let cgroupfs children inherit that
        // scope's resource envelope.
        let mut podman_arguments =
            podman_storage_arguments_with_cgroup_manager(&storage, runroot.path(), "cgroupfs");
        podman_arguments.extend([
            "--runtime=/usr/bin/crun".to_owned(),
            "build".to_owned(),
            "--no-cache".to_owned(),
            "--pull=never".to_owned(),
            "--platform".to_owned(),
            request.platform.clone(),
            "--file".to_owned(),
            context.join(&request.dockerfile).display().to_string(),
            "--tag".to_owned(),
            tag.clone(),
            "--cap-drop=all".to_owned(),
            "--security-opt=no-new-privileges".to_owned(),
            format!("--ulimit=nproc={0}:{0}", request.limits.processes),
            format!(
                "--network={}",
                egress
                    .as_ref()
                    .map_or("none", |value| value.internal_network.as_str())
            ),
            format!("--format={}", request.options.format),
            format!("--identity-label={}", request.options.identity_label),
            format!("--jobs={}", request.options.jobs),
            format!(
                "--disable-compression={}",
                request.options.layer_compression == "disabled"
            ),
            format!("--layers={}", request.options.layers),
            format!("--no-hostname={}", request.options.no_hostname),
            format!("--no-hosts={}", request.options.no_hosts),
            format!("--omit-history={}", request.options.omit_history),
            format!("--shm-size={}", request.options.shm_bytes),
            format!(
                "--skip-unused-stages={}",
                request.options.skip_unused_stages
            ),
        ]);
        for item in &request.options.additional_contexts {
            podman_arguments.push("--build-context".to_owned());
            podman_arguments.push(format!(
                "{}={}",
                item.name,
                context.join(&item.path).display()
            ));
        }
        for (flag, entries) in [
            ("--annotation", &request.options.annotations),
            ("--label", &request.options.labels),
            ("--layer-label", &request.options.layer_labels),
        ] {
            for item in entries {
                podman_arguments.push(flag.to_owned());
                podman_arguments.push(format!("{}={}", item.name, item.value));
            }
        }
        for item in &request.options.environment {
            podman_arguments.push("--env".to_owned());
            podman_arguments.push(format!("{}={}", item.name, scalar(&item.value)?));
        }
        if let Some(ignorefile) = &request.options.ignorefile {
            podman_arguments.push("--ignorefile".to_owned());
            podman_arguments.push(context.join(ignorefile).display().to_string());
        }
        for feature in &request.options.os_features {
            podman_arguments.push("--os-feature".to_owned());
            podman_arguments.push(feature.clone());
        }
        if let Some(version) = &request.options.os_version {
            podman_arguments.push("--os-version".to_owned());
            podman_arguments.push(version.clone());
        }
        match request.options.squash.as_str() {
            "new" => podman_arguments.push("--squash".to_owned()),
            "all" => podman_arguments.push("--squash-all".to_owned()),
            _ => {}
        }
        if let Some(timestamp) = request.options.timestamp {
            podman_arguments.push(format!("--timestamp={timestamp}"));
        }
        for name in &request.options.unset_environment {
            podman_arguments.push("--unsetenv".to_owned());
            podman_arguments.push(name.clone());
        }
        for name in &request.options.unset_labels {
            podman_arguments.push("--unsetlabel".to_owned());
            podman_arguments.push(name.clone());
        }
        if let Some(egress) = &egress {
            let proxy = format!("http://{}:18080", egress.proxy_name);
            for name in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"] {
                podman_arguments.push("--build-arg".to_owned());
                podman_arguments.push(format!("{name}={proxy}"));
            }
            for name in ["NO_PROXY", "no_proxy"] {
                podman_arguments.push("--build-arg".to_owned());
                podman_arguments.push(format!("{name}="));
            }
        }
        for argument in &request.arguments {
            podman_arguments.push("--build-arg".to_owned());
            podman_arguments.push(format!("{}={}", argument.name, scalar(&argument.value)?));
        }
        for capability in &request.capabilities {
            podman_arguments.push(format!("--cap-add={capability}"));
        }
        if let Some(target) = &request.target {
            podman_arguments.push("--target".to_owned());
            podman_arguments.push(target.clone());
        }
        podman_arguments.push(context.display().to_string());
        let mut arguments = vec![
            "--user".to_owned(),
            "--scope".to_owned(),
            "--collect".to_owned(),
            "--quiet".to_owned(),
            format!("--property=MemoryMax={}", request.limits.memory_bytes),
            format!(
                "--property=CPUQuota={}%",
                u64::from(request.limits.cpu_cores) * 100
            ),
            format!("--property=TasksMax={}", request.limits.processes),
            "/usr/bin/podman".to_owned(),
        ];
        arguments.extend(podman_arguments);
        let timeout = Duration::from_secs(request.limits.timeout_seconds.into());
        let output = self
            .runner
            .run_bounded_directory_with_output_limit_cancellable(
                Program::SystemdRun,
                &arguments,
                timeout,
                ProcessOutputBounds::new(
                    staging.path(),
                    build_staging_limit(request)?,
                    request.limits.output_bytes,
                ),
                cancelled,
            )?;
        if !output.success {
            return Err(RecipeBuildError::ImageBuild {
                diagnostic: podman_build_diagnostic(&output),
            });
        }
        let mut inspect_arguments = podman_storage_arguments(&storage, runroot.path());
        inspect_arguments.extend([
            "image".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{.Os}}\t{{.Architecture}}\t{{index .Config.Labels \"ai.vonkforge.runtime-interface\"}}\t{{.Config.User}}".to_owned(),
            tag.clone(),
        ]);
        let inspected = self.runner.run_cancellable(
            Program::Podman,
            &inspect_arguments,
            Duration::from_secs(60),
            cancelled,
        )?;
        inspect_image(&inspected.stdout).map_err(|_| RecipeBuildError::ImageInspect)?;
        let build_root = self.data_root.join("builds");
        fs::create_dir_all(&build_root)?;
        let operation_root = build_root.join(operation_id.to_string());
        fs::create_dir(&operation_root)?;
        let layout = self.layout_path(operation_id);
        let digest_file = staging.path().join("image.digest");
        let mut push_arguments = podman_storage_arguments(&storage, runroot.path());
        push_arguments.extend([
            "push".to_owned(),
            "--digestfile".to_owned(),
            digest_file.display().to_string(),
            tag.clone(),
            // Spark's supported runtime is Docker. Export a docker-save
            // archive so the privileged helper can use Docker's native
            // load path without exposing the daemon to the rootless builder.
            format!("docker-archive:{}", layout.display()),
        ]);
        let saved = self
            .runner
            .run_bounded_directory_with_output_limit_cancellable(
                Program::Podman,
                &push_arguments,
                Duration::from_secs(600),
                ProcessOutputBounds::new(
                    &operation_root,
                    request.limits.temporary_bytes,
                    request.limits.output_bytes,
                ),
                cancelled,
            )?;
        if !saved.success {
            return Err(RecipeBuildError::ImageExport);
        }
        let image_digest = fs::read_to_string(&digest_file)?.trim().to_owned();
        if image_digest.strip_prefix("sha256:").is_none_or(|digest| {
            digest.len() != 64
                || !digest
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        }) {
            return Err(RecipeBuildError::Evidence);
        }
        let image_bytes = fs::metadata(&layout)?.len();
        if image_bytes == 0 {
            return Err(RecipeBuildError::Evidence);
        }
        if image_bytes > request.limits.output_bytes {
            return Err(RecipeBuildError::OutputLimit);
        }
        if image_bytes > request.limits.temporary_bytes {
            return Err(RecipeBuildError::Process(ProcessError::StorageLimit));
        }
        let oci_layout_sha256 = sha256_file(&layout)?;
        let mut remove_arguments = podman_storage_arguments(&storage, runroot.path());
        remove_arguments.extend(["image".to_owned(), "rm".to_owned(), tag]);
        let _ = self
            .runner
            .run(Program::Podman, &remove_arguments, Duration::from_secs(60));
        Ok(RecipeBuildEvidence {
            build_input_sha256: request.build_input_sha256.clone(),
            image_bytes,
            image_digest,
            oci_layout_sha256,
            policy,
        })
    }
}

impl<R: ProcessRunner> RecipeBuilder<'_, R> {
    fn import_base_images(
        &self,
        request: &RecipeBuildRequest,
        storage: &Path,
        runroot: &Path,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<(), RecipeBuildError> {
        if request.base_images.is_empty() {
            return Ok(());
        }
        let store = BaseImageStore::open(self.data_root).map_err(recipe_base_image_error)?;
        let mut archive_bytes = 0_u64;
        for image in &request.base_images {
            let remaining = request
                .base_image_storage_bytes
                .checked_sub(archive_bytes)
                .ok_or(RecipeBuildError::OutputLimit)?;
            let mut archive = store
                .materialize(
                    self.runner,
                    image,
                    &request.platform,
                    remaining,
                    request.limits.temporary_bytes,
                )
                .map_err(recipe_base_image_error)?;
            archive_bytes = archive_bytes
                .checked_add(archive.bytes)
                .ok_or(RecipeBuildError::Evidence)?;
            if archive_bytes > request.base_image_storage_bytes {
                return Err(RecipeBuildError::OutputLimit);
            }
            archive.file.seek(SeekFrom::Start(0))?;
            let mut load_arguments = podman_storage_arguments(storage, runroot);
            load_arguments.extend(["load".to_owned(), "--quiet".to_owned()]);
            let loaded = self
                .runner
                .run_bounded_directory_with_input_cancellable(
                    Program::Podman,
                    &load_arguments,
                    Duration::from_secs(600),
                    ProcessInputBounds::new(
                        &archive.file,
                        storage.parent().ok_or(RecipeBuildError::Evidence)?,
                        build_staging_limit(request)?,
                    ),
                    cancelled,
                )
                .map_err(podman_import_process_error)?;
            if !loaded.success {
                return Err(RecipeBuildError::BaseImageImport {
                    diagnostic: podman_import_diagnostic(&loaded),
                });
            }
            let mut inspect_arguments = podman_storage_arguments(storage, runroot);
            inspect_arguments.extend([
                "image".to_owned(),
                "inspect".to_owned(),
                "--format".to_owned(),
                "{{.Digest}}\t{{.Os}}\t{{.Architecture}}".to_owned(),
                image.reference.clone(),
            ]);
            let inspected =
                self.runner
                    .run(Program::Podman, &inspect_arguments, Duration::from_secs(60))?;
            inspect_base_image(&inspected, &image.manifest_digest)
                .map_err(|_| RecipeBuildError::BaseImageInspect)?;
        }
        Ok(())
    }
}

fn build_staging_limit(request: &RecipeBuildRequest) -> Result<u64, RecipeBuildError> {
    request
        .base_image_storage_bytes
        .checked_add(request.limits.temporary_bytes)
        .and_then(|bytes| bytes.checked_add(request.source_bundle_bytes))
        .ok_or(RecipeBuildError::Evidence)
}

fn podman_import_diagnostic(output: &crate::process::ProcessOutput) -> PodmanImportDiagnostic {
    let mut evidence = Vec::with_capacity(output.stdout.len() + output.stderr.len() + 1);
    evidence.extend_from_slice(&output.stdout);
    evidence.push(b'\n');
    evidence.extend_from_slice(&output.stderr);
    let evidence = String::from_utf8_lossy(&evidence).to_ascii_lowercase();
    if evidence.contains("no space left on device") || evidence.contains("disk quota exceeded") {
        PodmanImportDiagnostic::TemporaryStorageExhausted
    } else if evidence.contains("insufficient uids or gids")
        || evidence.contains("subuid")
        || evidence.contains("subgid")
        || evidence.contains("newuidmap")
        || evidence.contains("newgidmap")
        || evidence.contains("lchown")
    {
        PodmanImportDiagnostic::SubordinateIdMappingUnavailable
    } else if evidence.contains("permission denied") || evidence.contains("operation not permitted")
    {
        PodmanImportDiagnostic::PermissionDenied
    } else if evidence.contains("payload does not match any of the supported image formats")
        || evidence.contains("oci archive")
        || evidence.contains("oci-archive")
        || evidence.contains("invalid reference format")
        || evidence.contains("invalid image name")
    {
        PodmanImportDiagnostic::ArchiveFormatRejected
    } else {
        PodmanImportDiagnostic::Unknown
    }
}

fn podman_build_diagnostic(output: &crate::process::ProcessOutput) -> PodmanBuildDiagnostic {
    let mut evidence = Vec::with_capacity(output.stdout.len() + output.stderr.len() + 1);
    evidence.extend_from_slice(&output.stdout);
    evidence.push(b'\n');
    evidence.extend_from_slice(&output.stderr);
    let evidence = String::from_utf8_lossy(&evidence).to_ascii_lowercase();
    if output.stdout.is_empty() && output.stderr.is_empty() {
        PodmanBuildDiagnostic::NonzeroWithoutOutput
    } else if evidence.contains("no space left on device")
        || evidence.contains("disk quota exceeded")
    {
        PodmanBuildDiagnostic::TemporaryStorageExhausted
    } else if evidence.contains("insufficient uids or gids")
        || evidence.contains("subuid")
        || evidence.contains("subgid")
        || evidence.contains("newuidmap")
        || evidence.contains("newgidmap")
        || evidence.contains("lchown")
    {
        PodmanBuildDiagnostic::SubordinateIdMappingUnavailable
    } else if evidence.contains("permission denied") || evidence.contains("operation not permitted")
    {
        PodmanBuildDiagnostic::PermissionDenied
    } else if evidence.contains("out of memory")
        || evidence.contains("memory limit")
        || evidence.contains("oom-kill")
        || evidence.contains("signal: killed")
    {
        PodmanBuildDiagnostic::MemoryLimitExceeded
    } else if evidence.contains("fuse-overlayfs")
        || evidence.contains("overlay mount")
        || evidence.contains("error committing")
        || evidence.contains("writing blob")
        || evidence.contains("storage driver")
    {
        PodmanBuildDiagnostic::StorageDriverFailure
    } else if evidence.contains("transient scope")
        || evidence.contains("scope unit")
        || evidence.contains("systemd-run")
        || evidence.contains("failed with result")
    {
        PodmanBuildDiagnostic::SystemdScopeFailure
    } else {
        PodmanBuildDiagnostic::Unknown
    }
}

fn podman_import_process_error(error: ProcessError) -> RecipeBuildError {
    let diagnostic = match error {
        ProcessError::StorageLimit => PodmanImportDiagnostic::DeclaredStorageLimitExceeded,
        ProcessError::Timeout => PodmanImportDiagnostic::DeadlineExceeded,
        ProcessError::OutputLimit => PodmanImportDiagnostic::DiagnosticOutputLimitExceeded,
        ProcessError::Io(_) => PodmanImportDiagnostic::SubprocessUnavailable,
        ProcessError::Cancelled => {
            return RecipeBuildError::Process(ProcessError::Cancelled);
        }
    };
    RecipeBuildError::BaseImageImport { diagnostic }
}

fn recipe_base_image_error(error: BaseImageError) -> RecipeBuildError {
    match error {
        BaseImageError::Invalid => RecipeBuildError::BaseImageContent,
        BaseImageError::Limit => RecipeBuildError::OutputLimit,
        BaseImageError::ManifestTransfer
        | BaseImageError::ManifestProcess(_)
        | BaseImageError::ManifestEvidence => RecipeBuildError::BaseImageManifest,
        BaseImageError::BlobTransfer
        | BaseImageError::BlobProcess(_)
        | BaseImageError::BlobEvidence => RecipeBuildError::BaseImageBlob,
        BaseImageError::ArchiveEvidence => RecipeBuildError::BaseImageArchive,
        BaseImageError::Io(error) => RecipeBuildError::Io(error),
    }
}

fn make_owned_directories_removable(path: &Path) -> std::io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Ok(());
    }
    let mut permissions = metadata.permissions();
    permissions.set_mode(permissions.mode() | 0o700);
    fs::set_permissions(path, permissions)?;
    for entry in fs::read_dir(path)? {
        make_owned_directories_removable(&entry?.path())?;
    }
    Ok(())
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum BuildNetwork {
    None,
    Public,
}

fn build_network(
    network: &vonk_agent_protocol::RecipeBuildNetwork,
) -> Result<BuildNetwork, RecipeBuildError> {
    if network.mode == "none" && network.hosts.is_empty() {
        Ok(BuildNetwork::None)
    } else if network.mode == "public" && !network.hosts.is_empty() {
        Ok(BuildNetwork::Public)
    } else {
        Err(RecipeBuildError::NetworkPolicy)
    }
}

struct BuildEgress<'a, R: ProcessRunner> {
    runner: &'a R,
    storage: &'a Path,
    runroot: &'a Path,
    internal_network: String,
    outbound_network: String,
    proxy_name: String,
    image: String,
}

struct BuildEgressStart<'a> {
    storage: &'a Path,
    runroot: &'a Path,
    staging: &'a Path,
    binary: &'a Path,
    operation_id: Uuid,
    hosts: &'a [String],
    staging_limit: u64,
    cancelled: &'a dyn Fn() -> bool,
}

impl<'a, R: ProcessRunner> BuildEgress<'a, R> {
    fn start(runner: &'a R, context: BuildEgressStart<'a>) -> Result<Self, RecipeBuildError> {
        let suffix = context.operation_id.simple().to_string();
        let internal_network = format!("vonk-build-in-{suffix}");
        let outbound_network = format!("vonk-build-out-{suffix}");
        let proxy_name = format!("vonk-build-proxy-{suffix}");
        let image = format!("localhost/vonk/build-egress:{suffix}");
        let rootfs = context.staging.join("build-egress-rootfs.tar");
        write_proxy_rootfs(context.binary, &rootfs)?;
        let result = Self {
            runner,
            storage: context.storage,
            runroot: context.runroot,
            internal_network,
            outbound_network,
            proxy_name,
            image,
        };
        let imported = result.run_with_file(
            &[
                "import",
                "--quiet",
                "--change",
                "ENTRYPOINT [\"/vonk-build-egress\"]",
                "-",
                &result.image,
            ],
            &rootfs,
            context.staging,
            context.staging_limit,
            Duration::from_secs(120),
            context.cancelled,
        )?;
        if !imported.success {
            return Err(RecipeBuildError::NetworkPolicy);
        }
        for arguments in [
            vec![
                "network".to_owned(),
                "create".to_owned(),
                "--internal".to_owned(),
                result.internal_network.clone(),
            ],
            vec![
                "network".to_owned(),
                "create".to_owned(),
                result.outbound_network.clone(),
            ],
        ] {
            let output =
                result.run_cancellable(&arguments, Duration::from_secs(30), context.cancelled)?;
            if !output.success {
                return Err(RecipeBuildError::NetworkPolicy);
            }
        }
        let mut arguments = vec![
            "run".to_owned(),
            "--detach".to_owned(),
            "--rm".to_owned(),
            "--name".to_owned(),
            result.proxy_name.clone(),
            "--network".to_owned(),
            format!("{},{}", result.outbound_network, result.internal_network),
            "--read-only".to_owned(),
            "--cap-drop=all".to_owned(),
            "--security-opt=no-new-privileges".to_owned(),
            "--pids-limit=96".to_owned(),
            "--memory=134217728b".to_owned(),
            "--cpus=1".to_owned(),
            "--user=65532:65532".to_owned(),
            result.image.clone(),
        ];
        for host in context.hosts {
            arguments.push("--allow-host".to_owned());
            arguments.push(host.clone());
        }
        let started =
            result.run_cancellable(&arguments, Duration::from_secs(30), context.cancelled)?;
        if !started.success {
            return Err(RecipeBuildError::NetworkPolicy);
        }
        let probed = result.run_cancellable(
            &[
                "exec".to_owned(),
                result.proxy_name.clone(),
                "/vonk-build-egress".to_owned(),
                "--probe".to_owned(),
            ],
            Duration::from_secs(10),
            context.cancelled,
        )?;
        if !probed.success {
            return Err(RecipeBuildError::NetworkPolicy);
        }
        Ok(result)
    }

    fn arguments(&self) -> Vec<String> {
        podman_storage_arguments(self.storage, self.runroot)
    }

    fn run(
        &self,
        extra: &[String],
        timeout: Duration,
    ) -> Result<crate::process::ProcessOutput, ProcessError> {
        let mut arguments = self.arguments();
        arguments.extend_from_slice(extra);
        self.runner.run(Program::Podman, &arguments, timeout)
    }

    fn run_cancellable(
        &self,
        extra: &[String],
        timeout: Duration,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<crate::process::ProcessOutput, ProcessError> {
        let mut arguments = self.arguments();
        arguments.extend_from_slice(extra);
        self.runner
            .run_cancellable(Program::Podman, &arguments, timeout, cancelled)
    }

    fn run_with_file(
        &self,
        extra: &[&str],
        path: &Path,
        monitored_directory: &Path,
        maximum_bytes: u64,
        timeout: Duration,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<crate::process::ProcessOutput, ProcessError> {
        let mut arguments = self.arguments();
        arguments.extend(extra.iter().map(|value| (*value).to_owned()));
        let file = File::open(path)?;
        self.runner.run_bounded_directory_with_input_cancellable(
            Program::Podman,
            &arguments,
            timeout,
            ProcessInputBounds::new(&file, monitored_directory, maximum_bytes),
            cancelled,
        )
    }
}

impl<R: ProcessRunner> Drop for BuildEgress<'_, R> {
    fn drop(&mut self) {
        for arguments in [
            vec![
                "stop".to_owned(),
                "--time=1".to_owned(),
                self.proxy_name.clone(),
            ],
            vec![
                "rm".to_owned(),
                "--force".to_owned(),
                self.proxy_name.clone(),
            ],
            vec![
                "network".to_owned(),
                "rm".to_owned(),
                "--force".to_owned(),
                self.internal_network.clone(),
            ],
            vec![
                "network".to_owned(),
                "rm".to_owned(),
                "--force".to_owned(),
                self.outbound_network.clone(),
            ],
            vec![
                "image".to_owned(),
                "rm".to_owned(),
                "--force".to_owned(),
                self.image.clone(),
            ],
        ] {
            let _ = self.run(&arguments, Duration::from_secs(15));
        }
    }
}

fn write_proxy_rootfs(binary: &Path, destination: &Path) -> Result<(), RecipeBuildError> {
    let descriptor = rustix::fs::open(
        binary,
        rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(std::io::Error::from)?;
    let mut source = File::from(descriptor);
    let metadata = source.metadata()?;
    if !metadata.is_file()
        || metadata.uid() != 0
        || metadata.nlink() != 1
        || !(64..=MAX_EGRESS_BINARY_BYTES).contains(&metadata.len())
        || metadata.permissions().mode() & 0o022 != 0
        || metadata.permissions().mode() & 0o111 == 0
    {
        return Err(RecipeBuildError::NetworkPolicy);
    }
    let destination = File::create(destination)?;
    let mut archive = tar::Builder::new(destination);
    let mut header = tar::Header::new_gnu();
    header.set_size(metadata.len());
    header.set_mode(0o555);
    header.set_uid(0);
    header.set_gid(0);
    header.set_mtime(0);
    header.set_cksum();
    archive.append_data(&mut header, "vonk-build-egress", &mut source)?;
    archive.finish()?;
    Ok(())
}

fn podman_storage_arguments(storage: &Path, runroot: &Path) -> Vec<String> {
    podman_storage_arguments_with_cgroup_manager(storage, runroot, "systemd")
}

fn podman_storage_arguments_with_cgroup_manager(
    storage: &Path,
    runroot: &Path,
    cgroup_manager: &str,
) -> Vec<String> {
    vec![
        format!("--cgroup-manager={cgroup_manager}"),
        "--root".to_owned(),
        storage.display().to_string(),
        "--runroot".to_owned(),
        runroot.display().to_string(),
        "--storage-opt".to_owned(),
        "overlay.ignore_chown_errors=true".to_owned(),
        "--storage-opt".to_owned(),
        "overlay.mount_program=/usr/bin/fuse-overlayfs".to_owned(),
        "--storage-opt".to_owned(),
        "overlay.force_mask=shared".to_owned(),
    ]
}

fn scalar(value: &serde_json::Value) -> Result<String, RecipeBuildError> {
    match value {
        serde_json::Value::Bool(value) => Ok(value.to_string()),
        serde_json::Value::Number(value) if value.as_i64().is_some() => Ok(value.to_string()),
        serde_json::Value::String(value) if !value.contains('\0') => Ok(value.clone()),
        _ => Err(RecipeBuildError::Evidence),
    }
}

fn inspect_image(payload: &[u8]) -> Result<(), RecipeBuildError> {
    let text = std::str::from_utf8(payload).map_err(|_| RecipeBuildError::Evidence)?;
    let fields = text.trim().split('\t').collect::<Vec<_>>();
    if fields.len() != 4
        || fields[0] != "linux"
        || fields[1] != "arm64"
        || fields[2] != "v1"
        || !non_root_user(fields[3])
    {
        return Err(RecipeBuildError::Evidence);
    }
    Ok(())
}

fn inspect_base_image(
    output: &crate::process::ProcessOutput,
    manifest_digest: &str,
) -> Result<(), RecipeBuildError> {
    let text = std::str::from_utf8(&output.stdout).map_err(|_| RecipeBuildError::Evidence)?;
    let fields = text.trim().split('\t').collect::<Vec<_>>();
    if !output.success || fields != [manifest_digest, "linux", "arm64"] {
        return Err(RecipeBuildError::Evidence);
    }
    Ok(())
}

fn non_root_user(value: &str) -> bool {
    let mut parts = value.split(':');
    let valid = |part: &str| {
        !part.is_empty() && !part.starts_with('0') && part.bytes().all(|byte| byte.is_ascii_digit())
    };
    valid(parts.next().unwrap_or_default())
        && parts.next().is_none_or(valid)
        && parts.next().is_none()
}

fn sha256_file(path: &Path) -> Result<String, std::io::Error> {
    use std::io::Read;
    let mut file = fs::File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(hex::encode(digest.finalize()))
}

#[cfg(test)]
mod tests {
    use super::{RecipeBuildError, podman_build_diagnostic, podman_import_process_error};
    use crate::process::{ProcessError, ProcessOutput};

    #[test]
    fn podman_build_failures_have_stable_secret_free_diagnostics() {
        for (stdout, stderr, diagnostic) in [
            (b"".as_slice(), b"".as_slice(), "nonzero-without-output"),
            (
                b"".as_slice(),
                b"write /private/secret: no space left on device".as_slice(),
                "temporary-storage-exhausted",
            ),
            (
                b"".as_slice(),
                b"fuse-overlayfs: operation failed for /private/secret".as_slice(),
                "storage-driver-failure",
            ),
            (
                b"".as_slice(),
                b"Failed to start transient scope unit".as_slice(),
                "systemd-scope-failure",
            ),
            (
                b"opaque /private/secret".as_slice(),
                b"".as_slice(),
                "unclassified-podman-build-failure",
            ),
        ] {
            let classified = podman_build_diagnostic(&ProcessOutput {
                success: false,
                stdout: stdout.to_vec(),
                stderr: stderr.to_vec(),
            });
            let error = RecipeBuildError::ImageBuild {
                diagnostic: classified,
            };
            assert_eq!(error.failure_evidence()["stage"], "image-build");
            assert_eq!(error.failure_evidence()["diagnostic"], diagnostic);
            assert!(!error.failure_evidence().to_string().contains("private"));
            assert!(!error.failure_evidence().to_string().contains("secret"));
        }
    }

    #[test]
    fn podman_import_process_failures_have_stable_secret_free_evidence() {
        for (error, diagnostic) in [
            (
                ProcessError::StorageLimit,
                "declared-storage-limit-exceeded",
            ),
            (ProcessError::Timeout, "deadline-exceeded"),
            (
                ProcessError::OutputLimit,
                "diagnostic-output-limit-exceeded",
            ),
            (
                ProcessError::Io(std::io::Error::other("/private/secret")),
                "subprocess-unavailable",
            ),
        ] {
            let error = podman_import_process_error(error);
            assert!(matches!(error, RecipeBuildError::BaseImageImport { .. }));
            assert_eq!(error.failure_evidence()["stage"], "base-image-import");
            assert_eq!(error.failure_evidence()["diagnostic"], diagnostic);
            assert!(!error.failure_evidence().to_string().contains("private"));
            assert!(!error.failure_evidence().to_string().contains("secret"));
        }
    }
}
