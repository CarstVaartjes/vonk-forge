//! Import.

use super::*;

pub(super) fn podman_import_diagnostic(
    output: &crate::process::ProcessOutput,
) -> PodmanImportDiagnostic {
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
    } else if (evidence.contains("apparmor") && evidence.contains("denied"))
        || evidence.contains("user namespace is not enabled")
        || evidence.contains("user namespaces are not enabled")
        || evidence.contains("cannot clone: operation not permitted")
    {
        PodmanBuildDiagnostic::UserNamespaceDenied
    } else if (evidence.contains("mount `proc`")
        || evidence.contains("mount 'proc'")
        || evidence.contains("mount \"proc\"")
        || evidence.contains("mount proc to proc"))
        && (evidence.contains("permission denied") || evidence.contains("operation not permitted"))
    {
        PodmanBuildDiagnostic::ProcMountDenied
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
        ProcessError::StorageLimit => PodmanImportDiagnostic::DeclaredStorageLimitExceeded,
        ProcessError::Timeout => PodmanImportDiagnostic::DeadlineExceeded,
        ProcessError::OutputLimit => PodmanImportDiagnostic::DiagnosticOutputLimitExceeded,
        ProcessError::Io(_) => PodmanImportDiagnostic::SubprocessUnavailable,
        ProcessError::Cancelled => {
            return RecipeBuildError::Process(ProcessError::Cancelled);
        }
    };
    RecipeBuildError::BaseImageImport {
        diagnostic,
        logs: None,
    }
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

pub(super) fn make_owned_directories_removable(path: &Path) -> std::io::Result<()> {
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
