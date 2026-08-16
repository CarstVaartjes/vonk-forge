//! Rootless, typed recipe image build execution.

use std::{
    fs,
    os::unix::{ffi::OsStrExt, fs::PermissionsExt},
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
    build_source::{BuildSourceError, materialize_source_bundle},
    process::{ProcessError, ProcessRunner, Program},
    source_policy::{SourcePolicyReport, dockerfile_base_images, inspect_build_source},
};

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
    #[error("build output exceeded its declared limit")]
    OutputLimit,
    #[error(
        "source build network policy is unsupported: public host allowlists require an installed egress boundary"
    )]
    NetworkPolicy,
    #[error("build storage is unavailable")]
    Io(#[from] std::io::Error),
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
        // `slirp4netns` only isolates the host network namespace; it does not
        // enforce a destination allowlist.  Never silently widen a declared
        // host policy into unrestricted egress.  Until the dedicated egress
        // boundary is installed, only explicitly networkless builds run.
        let network = build_network(&request.network)?;
        let staging_root = self.data_root.join("build-staging");
        fs::create_dir_all(&staging_root)?;
        let staging = PodmanBuildStaging::create(&staging_root)?;
        let context = staging.path().join("context");
        let storage = staging.path().join("podman-storage");
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
        self.import_base_images(request, &storage, runroot.path())?;
        let tag = format!("localhost/vonk/recipe-build-{}", request.build_id);
        // Ubuntu 24.04's supported Podman 4.9 build command does not accept
        // the `--cpus` or `--pids-limit` aliases. Express the same boundaries
        // with the portable CFS quota and nproc ulimit forms.
        let cpu_period = 100_000_u64;
        let cpu_quota = u64::from(request.limits.cpu_cores) * cpu_period;
        let mut arguments = podman_storage_arguments(&storage, runroot.path());
        arguments.extend([
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
            format!("--cpu-period={cpu_period}"),
            format!("--cpu-quota={cpu_quota}"),
            format!("--memory={}b", request.limits.memory_bytes),
            format!("--ulimit=nproc={0}:{0}", request.limits.processes),
            format!("--network={network}"),
        ]);
        for argument in &request.arguments {
            arguments.push("--build-arg".to_owned());
            arguments.push(format!("{}={}", argument.name, scalar(&argument.value)?));
        }
        arguments.push(context.display().to_string());
        let timeout = Duration::from_secs(request.limits.timeout_seconds.into());
        let output = self.runner.run_bounded_directory_with_output_limit(
            Program::Podman,
            &arguments,
            timeout,
            staging.path(),
            request
                .limits
                .temporary_bytes
                .checked_add(request.base_image_storage_bytes)
                .ok_or(RecipeBuildError::Evidence)?,
            request.limits.output_bytes,
        )?;
        if !output.success {
            return Err(RecipeBuildError::Evidence);
        }
        let mut inspect_arguments = podman_storage_arguments(&storage, runroot.path());
        inspect_arguments.extend([
            "image".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{.Os}}\t{{.Architecture}}\t{{index .Config.Labels \"ai.vonkforge.runtime-interface\"}}\t{{.Config.User}}".to_owned(),
            tag.clone(),
        ]);
        let inspected =
            self.runner
                .run(Program::Podman, &inspect_arguments, Duration::from_secs(60))?;
        inspect_image(&inspected.stdout)?;
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
        let saved = self.runner.run_bounded_directory_with_output_limit(
            Program::Podman,
            &push_arguments,
            Duration::from_secs(600),
            &operation_root,
            request.limits.temporary_bytes,
            request.limits.output_bytes,
        )?;
        if !saved.success {
            return Err(RecipeBuildError::Evidence);
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
    ) -> Result<(), RecipeBuildError> {
        if request.base_images.is_empty() {
            return Ok(());
        }
        let supply_root = self.data_root.join("base-images");
        let canonical_supply_root = supply_root.canonicalize()?;
        let mut archive_bytes = 0_u64;
        for image in &request.base_images {
            let digest = image
                .manifest_digest
                .strip_prefix("sha256:")
                .ok_or(RecipeBuildError::Evidence)?;
            let archive = supply_root
                .join("sha256")
                .join(digest)
                .join("image.oci.tar");
            let metadata = fs::symlink_metadata(&archive)?;
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || metadata.len() == 0
                || !archive.canonicalize()?.starts_with(&canonical_supply_root)
            {
                return Err(RecipeBuildError::Evidence);
            }
            archive_bytes = archive_bytes
                .checked_add(metadata.len())
                .ok_or(RecipeBuildError::Evidence)?;
            if archive_bytes > request.base_image_storage_bytes {
                return Err(RecipeBuildError::OutputLimit);
            }
            let mut load_arguments = podman_storage_arguments(storage, runroot);
            load_arguments.extend([
                "load".to_owned(),
                "--input".to_owned(),
                archive.display().to_string(),
            ]);
            let loaded = self.runner.run_bounded_directory(
                Program::Podman,
                &load_arguments,
                Duration::from_secs(600),
                storage,
                request.base_image_storage_bytes,
            )?;
            if !loaded.success {
                return Err(RecipeBuildError::Evidence);
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
            inspect_base_image(&inspected, &image.manifest_digest)?;
        }
        Ok(())
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

fn build_network(
    network: &vonk_agent_protocol::RecipeBuildNetwork,
) -> Result<&'static str, RecipeBuildError> {
    if network.mode == "none" && network.hosts.is_empty() {
        Ok("none")
    } else {
        Err(RecipeBuildError::NetworkPolicy)
    }
}

fn podman_storage_arguments(storage: &Path, runroot: &Path) -> Vec<String> {
    vec![
        "--cgroup-manager=systemd".to_owned(),
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
