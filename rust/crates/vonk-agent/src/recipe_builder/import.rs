//! Import.

use super::*;

pub(super) fn podman_import_diagnostic(
    output: &crate::process::ProcessOutput,
) -> PodmanImportDiagnostic {
    let logs = super::diagnostics::sanitized_process_logs(output);
    if logs.stdout.text.trim().is_empty() && logs.stderr.text.trim().is_empty() {
        return PodmanImportDiagnostic::NonzeroWithoutOutput;
    }
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

pub(crate) fn podman_build_diagnostic(
    output: &crate::process::ProcessOutput,
) -> PodmanBuildDiagnostic {
    let mut evidence = Vec::with_capacity(output.stdout.len() + output.stderr.len() + 1);
    evidence.extend_from_slice(&output.stdout);
    evidence.push(b'\n');
    evidence.extend_from_slice(&output.stderr);
    let evidence = String::from_utf8_lossy(&evidence).to_ascii_lowercase();
    // stdout is untrusted recipe/package progress. Podman also echoes the
    // entire RUN command on stderr; its words are not an observed host fault.
    // Only actual stderr cause lines may classify a platform denial.
    let stderr = String::from_utf8_lossy(&output.stderr).to_ascii_lowercase();
    let causes = if let Some((before, step)) = stderr.split_once("error: building at step ") {
        // A Dockerfile heredoc may put newlines inside the echoed command.
        // Keep preceding runtime errors and the cause after the closing quote.
        format!(
            "{before}\n{}",
            step.rsplit_once("\": ").map_or("", |(_, cause)| cause)
        )
    } else {
        stderr
    };
    let step_exited = causes
        .lines()
        .any(|line| line.starts_with("while running runtime: exit status "));
    let logs = super::diagnostics::sanitized_process_logs(output);
    if logs.stdout.text.trim().is_empty() && logs.stderr.text.trim().is_empty() {
        PodmanBuildDiagnostic::NonzeroWithoutOutput
    } else if evidence.contains("no space left on device")
        || evidence.contains("disk quota exceeded")
    {
        PodmanBuildDiagnostic::TemporaryStorageExhausted
    } else if causes.contains("insufficient uids or gids")
        || causes.contains("subuid")
        || causes.contains("subgid")
        || causes.contains("newuidmap")
        || causes.contains("newgidmap")
        || causes.contains("lchown")
    {
        PodmanBuildDiagnostic::SubordinateIdMappingUnavailable
    } else if causes.lines().any(|line| {
        line.contains("apparmor")
            && line.contains("denied")
            && (line.contains("userns") || line.contains("user namespace"))
    }) || causes.contains("user namespace is not enabled")
        || causes.contains("user namespaces are not enabled")
        || causes.contains("cannot clone: operation not permitted")
    {
        PodmanBuildDiagnostic::UserNamespaceDenied
    } else if (causes.contains("mount `proc`")
        || causes.contains("mount 'proc'")
        || causes.contains("mount \"proc\"")
        || causes.contains("mount proc to proc"))
        && (causes.contains("permission denied") || causes.contains("operation not permitted"))
    {
        PodmanBuildDiagnostic::ProcMountDenied
    } else if (!step_exited || causes.contains("crun:") || causes.contains("runc:"))
        && (causes.contains("permission denied") || causes.contains("operation not permitted"))
    {
        // A RUN process can report its own permission error. An explicit OCI
        // runtime refusal stays a platform denial even with a step exit status.
        PodmanBuildDiagnostic::PermissionDenied
    } else if evidence.contains("out of memory")
        || evidence.contains("memory limit")
        || evidence.contains("oom-kill")
        || evidence.contains("signal: killed")
    {
        PodmanBuildDiagnostic::MemoryLimitExceeded
    } else if causes.contains("fuse-overlayfs")
        || causes.contains("overlay mount")
        || causes.contains("error committing")
        || causes.contains("writing blob")
        || causes.contains("storage driver")
    {
        PodmanBuildDiagnostic::StorageDriverFailure
    } else if causes.contains("transient scope")
        || causes.contains("scope unit")
        || causes.contains("systemd-run")
        || causes.contains("failed with result")
    {
        PodmanBuildDiagnostic::SystemdScopeFailure
    } else if evidence.contains("previously applied) patch detected")
        || evidence.contains("hunk failed")
        || evidence.contains("hunks failed")
        || evidence.contains("hunk ignored")
        || evidence.contains("hunks ignored")
    {
        // `patch` names the drift itself; the build step it ran in is incidental.
        PodmanBuildDiagnostic::PatchRejected
    } else if evidence.contains("building at step") && evidence.contains("exit status") {
        PodmanBuildDiagnostic::BuildStepFailed
    } else {
        PodmanBuildDiagnostic::Unknown
    }
}

pub(super) fn podman_import_process_error(error: ProcessError) -> RecipeBuildError {
    let diagnostic = match error {
        // Import uses the filesystem free-space guard, not a directory-size
        // limit. Another disk consumer can clear this pressure before retry.
        ProcessError::StorageLimit => PodmanImportDiagnostic::TemporaryStorageExhausted,
        ProcessError::Timeout => PodmanImportDiagnostic::DeadlineExceeded,
        ProcessError::OutputLimit => PodmanImportDiagnostic::DiagnosticOutputLimitExceeded,
        ProcessError::Io(_) => PodmanImportDiagnostic::SubprocessUnavailable,
        ProcessError::Cancelled => {
            return RecipeBuildError::Process(ProcessError::Cancelled);
        }
    };
    RecipeBuildError::BaseImageImportProcess { diagnostic }
}

pub(super) fn recipe_base_image_error(error: BaseImageError) -> RecipeBuildError {
    match error {
        BaseImageError::Invalid => RecipeBuildError::BaseImageContent,
        BaseImageError::Limit => RecipeBuildError::OutputLimit,
        BaseImageError::ManifestTransfer
        | BaseImageError::ManifestProcess(_)
        | BaseImageError::ManifestEvidence => RecipeBuildError::BaseImageManifest,
        BaseImageError::BlobTransfer | BaseImageError::BlobEvidence => {
            RecipeBuildError::BaseImageBlob
        }
        BaseImageError::ArchiveEvidence => RecipeBuildError::BaseImageArchive,
        BaseImageError::Io(error) => RecipeBuildError::Io(error),
    }
}

pub(crate) fn make_owned_directories_removable(path: &Path) -> std::io::Result<()> {
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

/// A deterministic conflict between Podman's database and this build's paths.
/// Permission and integrity failures do not authorize discarding storage.
pub(super) fn podman_database_conflict(output: &crate::process::ProcessOutput) -> bool {
    !output.success
        && String::from_utf8_lossy(&output.stderr).contains("database configuration mismatch")
}
