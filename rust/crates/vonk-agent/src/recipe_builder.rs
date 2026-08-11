//! Rootless, typed recipe image build execution.

use std::{fs, path::Path, time::Duration};

use serde::Serialize;
use sha2::{Digest, Sha256};
use tempfile::Builder;
use thiserror::Error;
use uuid::Uuid;
use vonk_agent_protocol::RecipeBuildRequest;

use crate::{
    build_source::{BuildSourceError, materialize_source_bundle},
    process::{ProcessError, ProcessRunner, Program},
    source_policy::{SourcePolicyReport, inspect_build_source},
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
    pub oci_layout_sha256: String,
    pub policy: SourcePolicyReport,
}

pub struct RecipeBuilder<'a, R> {
    pub runner: &'a R,
    pub data_root: &'a Path,
}

impl<R: ProcessRunner> RecipeBuilder<'_, R> {
    pub fn layout_path(&self, operation_id: Uuid) -> std::path::PathBuf {
        self.data_root
            .join("builds")
            .join(operation_id.to_string())
            .join("image.oci.tar")
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
        let staging = Builder::new().prefix("source-").tempdir_in(&staging_root)?;
        let context = staging.path().join("context");
        let storage = staging.path().join("podman-storage");
        let runroot = staging.path().join("podman-runroot");
        fs::create_dir_all(&storage)?;
        fs::create_dir_all(&runroot)?;
        let source = materialize_source_bundle(archive, &request.source_bundle_sha256, &context)?;
        let policy = inspect_build_source(&source.files, &request.dockerfile);
        if !policy.passed {
            return Err(RecipeBuildError::Policy(policy));
        }
        let tag = format!("localhost/vonk/recipe-build-{}", request.build_id);
        // Ubuntu 24.04's supported Podman 4.9 build command does not accept
        // the `--cpus` or `--pids-limit` aliases. Express the same boundaries
        // with the portable CFS quota and nproc ulimit forms.
        let cpu_period = 100_000_u64;
        let cpu_quota = u64::from(request.limits.cpu_cores) * cpu_period;
        let mut arguments = vec![
            "--root".to_owned(),
            storage.display().to_string(),
            "--runroot".to_owned(),
            runroot.display().to_string(),
            "--storage-opt".to_owned(),
            "overlay.ignore_chown_errors=true".to_owned(),
            "--storage-opt".to_owned(),
            "overlay.mount_program=/usr/bin/fuse-overlayfs".to_owned(),
            "build".to_owned(),
            "--no-cache".to_owned(),
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
        ];
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
            request.limits.temporary_bytes,
            request.limits.output_bytes,
        )?;
        if !output.success {
            return Err(RecipeBuildError::Evidence);
        }
        let inspected = self.runner.run(
            Program::Podman,
            &[
                "--root".to_owned(),
                storage.display().to_string(),
                "--runroot".to_owned(),
                runroot.display().to_string(),
                "--storage-opt".to_owned(),
                "overlay.ignore_chown_errors=true".to_owned(),
                "--storage-opt".to_owned(),
                "overlay.mount_program=/usr/bin/fuse-overlayfs".to_owned(),
                "image".to_owned(),
                "inspect".to_owned(),
                "--format".to_owned(),
                "{{.Os}}\t{{.Architecture}}\t{{index .Config.Labels \"ai.vonkforge.runtime-interface\"}}\t{{.Config.User}}".to_owned(),
                tag.clone(),
            ],
            Duration::from_secs(60),
        )?;
        inspect_image(&inspected.stdout)?;
        let build_root = self.data_root.join("builds");
        fs::create_dir_all(&build_root)?;
        let operation_root = build_root.join(operation_id.to_string());
        fs::create_dir(&operation_root)?;
        let layout = self.layout_path(operation_id);
        let digest_file = staging.path().join("image.digest");
        let saved = self.runner.run_bounded_directory_with_output_limit(
            Program::Podman,
            &[
                "--root".to_owned(),
                storage.display().to_string(),
                "--runroot".to_owned(),
                runroot.display().to_string(),
                "--storage-opt".to_owned(),
                "overlay.ignore_chown_errors=true".to_owned(),
                "--storage-opt".to_owned(),
                "overlay.mount_program=/usr/bin/fuse-overlayfs".to_owned(),
                "push".to_owned(),
                "--digestfile".to_owned(),
                digest_file.display().to_string(),
                tag.clone(),
                format!("oci-archive:{}", layout.display()),
            ],
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
        let _ = self.runner.run(
            Program::Podman,
            &[
                "--root".to_owned(),
                storage.display().to_string(),
                "--runroot".to_owned(),
                runroot.display().to_string(),
                "--storage-opt".to_owned(),
                "overlay.ignore_chown_errors=true".to_owned(),
                "--storage-opt".to_owned(),
                "overlay.mount_program=/usr/bin/fuse-overlayfs".to_owned(),
                "image".to_owned(),
                "rm".to_owned(),
                tag,
            ],
            Duration::from_secs(60),
        );
        Ok(RecipeBuildEvidence {
            build_input_sha256: request.build_input_sha256.clone(),
            image_bytes,
            image_digest,
            oci_layout_sha256,
            policy,
        })
    }
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
