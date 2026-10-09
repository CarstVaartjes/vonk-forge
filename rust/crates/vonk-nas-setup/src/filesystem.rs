//! Filesystem.

use super::*;

pub(super) fn ensure_safe_output_root(output: &Path) -> Result<PathBuf, SetupError> {
    if output.as_os_str().is_empty() {
        return Err(SetupError::UnsafeDestination("path is empty".to_owned()));
    }
    let output = canonicalize_selected_path(output)?;
    match fs::symlink_metadata(&output) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
        Ok(_) => {
            return Err(SetupError::UnsafeDestination(
                "output root is not a real directory".to_owned(),
            ));
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            fs::create_dir_all(&output)?;
            set_directory_mode(&output)?;
        }
        Err(error) => return Err(error.into()),
    }
    let output = fs::canonicalize(output)?;
    match fs::symlink_metadata(&output) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => Ok(output),
        _ => Err(SetupError::UnsafeDestination(
            "output root is not a real directory".to_owned(),
        )),
    }
}

pub(super) fn canonicalize_selected_path(path: &Path) -> Result<PathBuf, SetupError> {
    let requested = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut absolute = PathBuf::new();
    for component in requested.components() {
        match component {
            Component::ParentDir => {
                return Err(SetupError::UnsafeDestination(
                    "parent traversal is not allowed".to_owned(),
                ));
            }
            Component::CurDir => {}
            _ => absolute.push(component.as_os_str()),
        }
    }

    match fs::symlink_metadata(&absolute) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            return Err(SetupError::UnsafeDestination(format!(
                "{} is a symbolic link",
                absolute.display()
            )));
        }
        Ok(_) => return fs::canonicalize(absolute).map_err(SetupError::from),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }

    let mut ancestor = absolute.as_path();
    let mut missing = Vec::new();
    let mut remaining = absolute.components().count();
    while remaining > 0 {
        remaining -= 1;
        let name = ancestor.file_name().ok_or_else(|| {
            SetupError::UnsafeDestination("path has no existing ancestor".to_owned())
        })?;
        missing.push(name.to_os_string());
        ancestor = ancestor.parent().ok_or_else(|| {
            SetupError::UnsafeDestination("path has no existing ancestor".to_owned())
        })?;
        match fs::symlink_metadata(ancestor) {
            Ok(_) => {
                let mut canonical = fs::canonicalize(ancestor)?;
                if !fs::metadata(&canonical)?.is_dir() {
                    return Err(SetupError::UnsafeDestination(format!(
                        "{} is not a directory",
                        ancestor.display()
                    )));
                }
                for component in missing.iter().rev() {
                    canonical.push(component);
                }
                return Ok(canonical);
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    Err(SetupError::UnsafeDestination(
        "path has no existing ancestor".to_owned(),
    ))
}

pub(super) fn validate_existing_bundle(
    bundle: &Path,
    _payload: &CanonicalTemplatePayload,
) -> Result<(), SetupError> {
    let bundle = canonicalize_selected_path(bundle)?;
    require_real_directory(&bundle)?;
    // Only consumed paths are authority. Unrelated entries, including symlinks,
    // are never traversed or modified. Empty owned staging directories may be
    // retired; nonempty entries are isolated by their unique staging names.
    if let Ok(entries) = fs::read_dir(&bundle) {
        for entry in entries.flatten() {
            if is_stale_staging_directory_name(&entry.file_name())
                && entry.file_type().is_ok_and(|kind| kind.is_dir())
            {
                let _ = fs::remove_dir(entry.path());
            }
        }
    }
    Ok(())
}

pub(super) fn is_stale_staging_directory_name(name: &std::ffi::OsStr) -> bool {
    let Some(name) = name.to_str() else {
        return false;
    };
    let Some(remainder) = name.strip_prefix(".vonk-forge.setup-") else {
        return false;
    };
    let Some((pid, sequence)) = remainder.split_once('-') else {
        return false;
    };
    !pid.is_empty()
        && !sequence.is_empty()
        && pid.bytes().all(|byte| byte.is_ascii_digit())
        && sequence.bytes().all(|byte| byte.is_ascii_digit())
}

pub(super) fn require_real_directory(path: &Path) -> Result<(), SetupError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => Ok(()),
        _ => Err(SetupError::MissingBundle),
    }
}

pub(super) fn create_staging_directory(parent: &Path) -> Result<PathBuf, SetupError> {
    let temporary = tempfile::Builder::new()
        .prefix(".vonk-forge.setup-")
        .tempdir_in(parent)?;
    set_directory_mode(temporary.path())?;
    Ok(temporary.keep())
}

pub(super) fn create_secure_directory(path: &Path) -> Result<(), SetupError> {
    fs::create_dir(path)?;
    set_directory_mode(path)?;
    Ok(())
}

pub(super) fn ensure_secure_directory(path: &Path) -> Result<(), SetupError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {
            set_directory_mode(path)?;
            Ok(())
        }
        Ok(_) => {
            let parent = path.parent().expect("owned directory has parent");
            let retired = create_staging_directory(parent)?;
            fs::rename(path, retired.join("preserved"))?;
            create_secure_directory(path)?;
            sync_directory(parent)
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => create_secure_directory(path),
        Err(error) => Err(error.into()),
    }
}

/// Make the group-readable secrets 0640 with the payload's group, leaving the
/// owner alone. Idempotent: files that already match are not touched. Only
/// root (or a member of the group) may assign the group. Other callers keep
/// the files owner-only and report the existing OS authorization decision.
pub(super) fn apply_secret_group<R: BufRead, W: Write, S: SecretInput<R, W>>(
    payload: &CanonicalTemplatePayload,
    root: &Path,
    prompt: &mut PromptIo<R, W, S>,
) -> Result<(), SetupError> {
    let Some(group) = &payload.group_readable_secrets else {
        return Ok(());
    };
    let mut fixed = Vec::new();
    let mut pending = Vec::new();
    for file in &group.files {
        let path = root.join(file);
        match fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.is_file() => {}
            Ok(_) => {
                return Err(SetupError::UnsafeDestination(format!(
                    "{} is not a regular secret file",
                    path.display()
                )));
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(at_path(&path, error)),
        }
        match set_secret_group(&path, group.gid)? {
            GroupOutcome::Unchanged => {}
            GroupOutcome::Fixed => fixed.push(file.as_str()),
            GroupOutcome::NotPermitted => pending.push(file.as_str()),
        }
    }
    if !fixed.is_empty() {
        prompt.note(&format!(
            "Secret permissions: set group {} and mode 0640 on {} for the capability-free Tailscale containers.",
            group.gid,
            fixed.join(", ")
        ))?;
    }
    if !pending.is_empty() {
        prompt.note(&format!(
            "{}: group {}: {}",
            vonk_agent_protocol::generated::SecurityRefusalReason::PermissionDenied,
            group.gid,
            pending.join(", ")
        ))?;
    }
    Ok(())
}

pub(super) enum GroupOutcome {
    Unchanged,
    Fixed,
    NotPermitted,
}

#[cfg(unix)]
pub(super) fn set_secret_group(path: &Path, gid: u32) -> Result<GroupOutcome, SetupError> {
    use std::os::unix::fs::{MetadataExt, chown};
    let metadata = fs::metadata(path).map_err(|error| at_path(path, error))?;
    if metadata.gid() == gid && metadata.mode() & 0o777 == 0o640 {
        return Ok(GroupOutcome::Unchanged);
    }
    if metadata.gid() != gid {
        match chown(path, None, Some(gid)) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::PermissionDenied => {
                return Ok(GroupOutcome::NotPermitted);
            }
            Err(error) => return Err(at_path(path, error)),
        }
    }
    set_file_mode(path, 0o640)?;
    Ok(GroupOutcome::Fixed)
}

#[cfg(not(unix))]
pub(super) fn set_secret_group(_path: &Path, _gid: u32) -> Result<GroupOutcome, SetupError> {
    Ok(GroupOutcome::NotPermitted)
}

pub(super) fn write_secret_file(
    root: &Path,
    relative: &str,
    content: &[u8],
) -> Result<(), SetupError> {
    write_nested_file(root, relative, content, 0o600)
}

pub(super) fn write_nested_file(
    root: &Path,
    relative: &str,
    content: &[u8],
    mode: u32,
) -> Result<(), SetupError> {
    let path = Path::new(relative);
    ensure_nested_parent(root, path, relative)?;
    let target = root.join(path);
    write_new_file(&target, content, mode)?;
    sync_directory(target.parent().expect("secret has parent"))?;
    Ok(())
}

pub(super) fn ensure_nested_parent(
    root: &Path,
    path: &Path,
    relative: &str,
) -> Result<(), SetupError> {
    let parent = path.parent().unwrap_or_else(|| Path::new(""));
    let mut current = root.to_path_buf();
    for component in parent.components() {
        let Component::Normal(component) = component else {
            return Err(SetupError::InvalidPayload(format!(
                "invalid secret path {relative}"
            )));
        };
        current.push(component);
        match fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
            Ok(_) => {
                let retired = create_staging_directory(
                    current.parent().expect("nested directory has parent"),
                )?;
                fs::rename(&current, retired.join("preserved"))?;
                create_secure_directory(&current)?;
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                create_secure_directory(&current)?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

/// Earlier bundles carried rendered runtime configs here. They now ship in
/// the Controller image, so an upgrade drops the stale copies.
pub(super) fn remove_retired_runtime_configs(secret_root: &Path) -> Result<(), SetupError> {
    let retired = secret_root.join("runtime-configs");
    match fs::symlink_metadata(&retired) {
        Ok(metadata) if metadata.is_dir() => Ok(fs::remove_dir_all(&retired)?),
        Ok(_) => Ok(fs::remove_file(&retired)?),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

pub(super) fn write_new_file(path: &Path, content: &[u8], mode: u32) -> Result<(), SetupError> {
    // New files are produced inside private staging. A failed write never
    // exposes a truncated member, and response loss reuses the exact bytes.
    atomic_replace(path, content, mode)
}

pub(super) fn stage_replacement(
    path: &Path,
    content: &[u8],
    mode: u32,
) -> Result<PathBuf, SetupError> {
    let parent = path.parent().expect("file has parent");
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| SetupError::UnsafeDestination("replacement path is invalid".to_owned()))?;
    let mut temporary = tempfile::Builder::new()
        .prefix(&format!(".{file_name}.tmp-"))
        .tempfile_in(parent)?;
    set_file_mode(temporary.path(), mode)?;
    temporary.write_all(content)?;
    sync_file(temporary.as_file())?;
    let (_, path) = temporary
        .keep()
        .map_err(|error| SetupError::Io(error.error))?;
    Ok(path)
}

pub(super) fn atomic_replace(path: &Path, content: &[u8], mode: u32) -> Result<(), SetupError> {
    let mut last_error = publication::unknown();
    for attempt in 0..3 {
        // A lost write/sync response is observed before another replacement.
        // Never read through a symbolic link while reconciling generated state.
        if same_content(path, content) {
            match File::open(path)
                .map_err(SetupError::from)
                .and_then(|file| sync_file(&file))
                .and_then(|()| sync_directory(path.parent().expect("file has parent")))
            {
                Ok(()) => return Ok(()),
                Err(error) => last_error = error,
            }
        } else {
            match replace_once(path, content, mode) {
                Ok(()) => return Ok(()),
                Err(error @ SetupError::PermissionDenied { .. }) => return Err(error),
                Err(error) => last_error = error,
            }
        }
        if attempt < 2 {
            std::thread::sleep(std::time::Duration::from_millis(50 << attempt));
        }
    }
    Err(last_error)
}

fn replace_once(path: &Path, content: &[u8], mode: u32) -> Result<(), SetupError> {
    let parent = path.parent().expect("file has parent");
    if fs::symlink_metadata(path)
        .is_ok_and(|metadata| !metadata.is_file() || metadata.file_type().is_symlink())
    {
        let retired = create_staging_directory(parent)?;
        fs::rename(path, retired.join("preserved"))?;
        sync_directory(parent)?;
    }
    let temporary = stage_replacement(path, content, mode)?;
    keep_owner(path, &temporary);
    match fs::rename(&temporary, path) {
        Ok(()) => {
            sync_directory(parent)?;
            Ok(())
        }
        Err(error) => {
            let _ = fs::remove_file(temporary);
            Err(at_path(path, error))
        }
    }
}

/// A sudo run creates files as root; keep the replaced file's owner and group
/// so the bundle owner does not lose access. Best effort: a non-root caller
/// already creates the file as itself.
#[cfg(unix)]
pub(super) fn keep_owner(original: &Path, replacement: &Path) {
    use std::os::unix::fs::{MetadataExt, chown};
    if let Ok(metadata) = fs::metadata(original) {
        let _ = chown(replacement, Some(metadata.uid()), Some(metadata.gid()));
    }
}

#[cfg(not(unix))]
pub(super) fn keep_owner(_original: &Path, _replacement: &Path) {}

pub(super) fn sync_directory(path: &Path) -> Result<(), SetupError> {
    let directory = File::open(path)?;
    sync_file(&directory)
}

pub(super) fn sync_file(file: &File) -> Result<(), SetupError> {
    complete_sync(file.sync_all(), || {
        rustix::fs::fsync(file).map_err(|error| io::Error::from_raw_os_error(error.raw_os_error()))
    })
}

pub(super) fn complete_sync<F>(full_sync: io::Result<()>, posix_sync: F) -> Result<(), SetupError>
where
    F: FnOnce() -> io::Result<()>,
{
    match full_sync {
        Ok(()) => Ok(()),
        // std uses F_FULLFSYNC on macOS. SMB mounts can reject that stronger
        // operation with ENOTSUP while supporting POSIX fsync, which still
        // flushes file data and metadata to the NAS. Preserve the durability
        // boundary by falling back to fsync instead of skipping synchronization.
        Err(error) if full_sync_is_unsupported(&error) => posix_sync().map_err(Into::into),
        Err(error) => Err(error.into()),
    }
}

pub(super) fn full_sync_is_unsupported(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::Unsupported
        || (cfg!(target_os = "macos") && error.raw_os_error() == Some(45))
}

#[cfg(unix)]
pub(super) fn set_open_mode(options: &mut OpenOptions, mode: u32) {
    use std::os::unix::fs::OpenOptionsExt;
    options.mode(mode);
}

#[cfg(not(unix))]
pub(super) fn set_open_mode(_options: &mut OpenOptions, _mode: u32) {}

#[cfg(unix)]
pub(super) fn set_directory_mode(path: &Path) -> Result<(), SetupError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .map_err(|error| at_path(path, error))
}

#[cfg(not(unix))]
pub(super) fn set_directory_mode(_path: &Path) -> Result<(), SetupError> {
    Ok(())
}

#[cfg(unix)]
pub(super) fn set_file_mode(path: &Path, mode: u32) -> Result<(), SetupError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
        .map_err(|error| at_path(path, error))
}

#[cfg(not(unix))]
pub(super) fn set_file_mode(_path: &Path, _mode: u32) -> Result<(), SetupError> {
    Ok(())
}

fn same_content(path: &Path, content: &[u8]) -> bool {
    use std::io::Read;
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32);
    }
    let Ok(file) = options.open(path) else {
        return false;
    };
    if !file
        .metadata()
        .is_ok_and(|metadata| metadata.is_file() && metadata.len() == content.len() as u64)
    {
        return false;
    }
    let mut bytes = Vec::new();
    file.take(content.len().saturating_add(1) as u64)
        .read_to_end(&mut bytes)
        .is_ok()
        && bytes == content
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    #[test]
    fn old_staging_names_do_not_poison_new_publication() {
        let root = tempfile::tempdir().unwrap();
        for sequence in 0..32 {
            fs::create_dir(root.path().join(format!(
                ".vonk-forge.setup-{}-{sequence}",
                std::process::id()
            )))
            .unwrap();
            fs::write(
                root.path()
                    .join(format!(".compose.tmp-{}-{sequence}", std::process::id())),
                b"preserved",
            )
            .unwrap();
        }
        let staging = create_staging_directory(root.path()).unwrap();
        assert!(staging.is_dir());
        let target = root.path().join("compose");
        fs::write(&target, b"original").unwrap();
        for content in [b"first".as_slice(), b"second".as_slice()] {
            atomic_replace(&target, content, 0o600).unwrap();
            assert_eq!(fs::read(&target).unwrap(), content);
        }
        assert_eq!(
            fs::read(
                root.path()
                    .join(format!(".compose.tmp-{}-0", std::process::id()))
            )
            .unwrap(),
            b"preserved"
        );
    }

    #[test]
    fn unsupported_full_sync_falls_back_to_posix_fsync() {
        let fallback_called = Cell::new(false);
        complete_sync(
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "full sync is unavailable",
            )),
            || {
                fallback_called.set(true);
                Ok(())
            },
        )
        .expect("POSIX fsync preserves the durability boundary");
        assert!(fallback_called.get());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_enotsup_falls_back_even_when_rust_does_not_classify_it() {
        let fallback_called = Cell::new(false);
        complete_sync(Err(io::Error::from_raw_os_error(45)), || {
            fallback_called.set(true);
            Ok(())
        })
        .expect("macOS SMB ENOTSUP falls back to POSIX fsync");
        assert!(fallback_called.get());
    }

    #[test]
    fn full_sync_rejects_other_io_failures_without_fallback() {
        let fallback_called = Cell::new(false);
        complete_sync(
            Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "file cannot be synced",
            )),
            || {
                fallback_called.set(true);
                Ok(())
            },
        )
        .expect_err("unconfirmed durability is withheld");
        assert!(!fallback_called.get());
        complete_sync(Ok(()), || panic!("fallback must not run")).unwrap();
    }

    #[test]
    fn posix_fsync_failure_remains_fatal() {
        complete_sync(
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "full sync is unavailable",
            )),
            || Err(io::Error::other("POSIX fsync failed")),
        )
        .expect_err("unconfirmed durability is withheld");
        complete_sync(Ok(()), || panic!("fallback must not run")).unwrap();
    }
}
