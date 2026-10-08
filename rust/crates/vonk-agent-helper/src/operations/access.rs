//! Access.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    /// The agent cannot traverse private directories created by root or the
    /// workload UID. Reset only this authorized run's disposable tmp tree,
    /// after exact container absence, without following any path component or
    /// descendant symlink. Outputs and the installation cache are untouched.
    pub(super) fn reset_runtime_tmp_if_requested(
        &self,
        run_id: &str,
    ) -> Result<(), OperationError> {
        if uuid::Uuid::parse_str(run_id)
            .ok()
            .map(|value| value.to_string())
            .as_deref()
            != Some(run_id)
        {
            return Err(OperationError::InvalidOperation);
        }
        let flags = rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC;
        let root: std::os::fd::OwnedFd = OpenOptions::new()
            .read(true)
            .custom_flags(flags.bits() as i32)
            .open(&self.roots.agent_data)?
            .into();
        let metadata_root =
            rustix::fs::openat(&root, "run-metadata", flags, rustix::fs::Mode::empty())
                .map_err(errno_io)?;
        let metadata = rustix::fs::openat(&metadata_root, run_id, flags, rustix::fs::Mode::empty())
            .map_err(errno_io)?;
        let marker = match rustix::fs::openat(
            &metadata,
            "tmp-reset-required",
            // Inspect the descriptor before accepting its type. A malformed
            // FIFO must not block the helper while open waits for a writer.
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::NONBLOCK
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        ) {
            Ok(marker) => marker,
            Err(rustix::io::Errno::NOENT) => return Ok(()),
            Err(error) => return Err(errno_io(error).into()),
        };
        let marker_state = rustix::fs::fstat(&marker).map_err(errno_io)?;
        if rustix::fs::FileType::from_raw_mode(marker_state.st_mode)
            != rustix::fs::FileType::RegularFile
            || marker_state.st_mode & 0o777 != 0o600
            || marker_state.st_size != 0
            || marker_state.st_nlink != 1
            || self
                .runtime_request_owner_uid
                .is_some_and(|owner| marker_state.st_uid != owner)
        {
            return Err(OperationError::UnsafePath);
        }
        let device = rustix::fs::fstat(&root).map_err(errno_io)?.st_dev;
        let mut directory = Some(root);
        for component in ["runs", run_id, "outputs", "tmp"] {
            let Some(parent) = directory.take() else {
                break;
            };
            directory =
                match rustix::fs::openat(&parent, component, flags, rustix::fs::Mode::empty()) {
                    Ok(directory) => Some(directory),
                    Err(rustix::io::Errno::NOENT) => None,
                    Err(error) => return Err(errno_io(error).into()),
                };
            if let Some(directory) = &directory
                && rustix::fs::fstat(directory).map_err(errno_io)?.st_dev != device
            {
                return Err(OperationError::UnsafePath);
            }
        }
        if let Some(directory) = &directory {
            remove_directory_contents(directory, device)?;
            rustix::fs::fsync(directory).map_err(errno_io)?;
        }
        rustix::fs::unlinkat(
            &metadata,
            "tmp-reset-required",
            rustix::fs::AtFlags::empty(),
        )
        .map_err(errno_io)?;
        rustix::fs::fsync(&metadata).map_err(errno_io)?;
        Ok(())
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn prepare_runtime_access(
        &self,
        run: &ValidatedDockerRun,
    ) -> Result<(), OperationError> {
        for path in run.models.iter().chain(run.inputs.iter()) {
            // Reapplying setfacl changes ctime even when the named runtime
            // entry already grants precisely the intended read access. That
            // invalidates the agent's immutable installation receipt before
            // collective readiness. Inspect the whole selected tree and skip
            // the recursive write only when every ACL is already exact.
            // Installation and run inputs are owned by the unprivileged
            // agent, as checked while validating the signed Docker run.
            // The helper's own root-owned custody is a separate boundary.
            if runtime_read_tree_acl_ready(path, run.uid, self.runtime_request_owner_uid)? {
                continue;
            }
            let output = self
                .runner
                .run(
                    Path::new("/usr/bin/setfacl"),
                    &[
                        "-R".to_owned(),
                        "-m".to_owned(),
                        format!("u:{}:rX", run.uid),
                        path.display().to_string(),
                    ],
                )
                .map_err(|_| OperationError::CommandFailed)?;
            if !output.success {
                return Err(OperationError::CommandFailed);
            }
        }
        let cache_root = run.cache_root.as_path();
        let tmp_root = run.tmp_root.parent().ok_or(OperationError::UnsafePath)?;
        ensure_runtime_directory(cache_root)?;
        ensure_runtime_directory(&run.cache_home)?;
        ensure_runtime_directory(tmp_root)?;
        ensure_runtime_directory(&run.tmp_root)?;
        for (path, access) in [
            (run.outputs.as_path(), "rwx"),
            (cache_root, "rwx"),
            (run.cache_home.as_path(), "rwx"),
            (tmp_root, "rwx"),
            (run.tmp_root.as_path(), "rwx"),
            (run.runtime_contract.as_path(), "r"),
        ] {
            let output = self
                .runner
                .run(
                    Path::new("/usr/bin/setfacl"),
                    &[
                        "-m".to_owned(),
                        format!("u:{}:{access}", run.uid),
                        path.display().to_string(),
                    ],
                )
                .map_err(|_| OperationError::CommandFailed)?;
            if !output.success {
                return Err(OperationError::CommandFailed);
            }
        }
        Ok(())
    }
}

pub(super) fn runtime_read_tree_acl_ready(
    path: &Path,
    runtime_uid: u32,
    required_owner_uid: Option<u32>,
) -> Result<bool, OperationError> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink()
        || !(metadata.is_file() || metadata.is_dir())
        || metadata.mode() & 0o022 != 0
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
    {
        return Err(OperationError::UnsafePath);
    }
    if metadata.is_dir() && xattr::get(path, "system.posix_acl_default")?.is_some() {
        return Err(OperationError::InvalidArtifact);
    }
    let mut ready = exact_runtime_read_acl(path, &metadata, runtime_uid)?;
    if metadata.is_dir() {
        for entry in fs::read_dir(path)? {
            ready &= runtime_read_tree_acl_ready(&entry?.path(), runtime_uid, required_owner_uid)?;
        }
    }
    Ok(ready)
}

pub(super) fn exact_runtime_read_acl(
    path: &Path,
    metadata: &fs::Metadata,
    runtime_uid: u32,
) -> Result<bool, OperationError> {
    let Some(value) = xattr::get(path, "system.posix_acl_access")? else {
        return Ok(false);
    };
    if value.len() != 44 || u32::from_le_bytes(value[..4].try_into().unwrap()) != 2 {
        return Err(OperationError::InvalidArtifact);
    }
    let mut user_object = None;
    let mut runtime_user = None;
    let mut group_object = None;
    let mut mask = None;
    let mut other = None;
    for entry in value[4..].as_chunks::<8>().0.iter() {
        let (tag, permissions, identifier) = acl_entry(entry);
        match tag {
            0x0001 if identifier == u32::MAX && user_object.replace(permissions).is_none() => {}
            0x0002 if identifier == runtime_uid && runtime_user.replace(permissions).is_none() => {}
            0x0004 if identifier == u32::MAX && group_object.replace(permissions).is_none() => {}
            0x0010 if identifier == u32::MAX && mask.replace(permissions).is_none() => {}
            0x0020 if identifier == u32::MAX && other.replace(permissions).is_none() => {}
            _ => return Err(OperationError::InvalidArtifact),
        }
    }
    let (Some(user_object), Some(runtime_user), Some(group_object), Some(mask), Some(other)) =
        (user_object, runtime_user, group_object, mask, other)
    else {
        return Err(OperationError::InvalidArtifact);
    };
    let expected_runtime = if metadata.is_dir() || (user_object | group_object | other) & 0o1 != 0 {
        0o5
    } else {
        0o4
    };
    if user_object > 0o7
        || group_object & 0o2 != 0
        || other & 0o2 != 0
        || runtime_user != expected_runtime
        || mask != (group_object | runtime_user)
        || metadata.mode() & 0o777
            != (u32::from(user_object) << 6 | u32::from(mask) << 3 | u32::from(other))
    {
        return Err(OperationError::InvalidArtifact);
    }
    Ok(true)
}

pub(super) fn exact_runtime_acl(path: &Path, runtime_uid: u32) -> bool {
    const ACL_VERSION: u32 = 0x0002;
    const USER_OBJ: u16 = 0x0001;
    const USER: u16 = 0x0002;
    const GROUP_OBJ: u16 = 0x0004;
    const MASK: u16 = 0x0010;
    const OTHER: u16 = 0x0020;
    let Ok(Some(value)) = xattr::get(path, "system.posix_acl_access") else {
        return false;
    };
    if value.len() < 4 || u32::from_le_bytes(value[..4].try_into().unwrap()) != ACL_VERSION {
        return false;
    }
    let entries = &value[4..];
    if entries.len() % 8 != 0 || entries.len() / 8 != 5 {
        return false;
    }
    let mut user_object = false;
    let mut runtime_user = false;
    let mut group_object = false;
    let mut mask = false;
    let mut other = false;
    for entry in entries.as_chunks::<8>().0.iter() {
        let (tag, permissions, identifier) = acl_entry(entry);
        match tag {
            USER_OBJ => user_object = permissions == 0o7,
            USER if identifier == runtime_uid => runtime_user = permissions == 0o7,
            GROUP_OBJ => group_object = permissions == 0,
            MASK => mask = permissions == 0o7,
            OTHER => other = permissions == 0,
            _ => return false,
        }
    }
    user_object && runtime_user && group_object && mask && other
}

/// Split one eight-byte POSIX ACL entry into its tag, permissions, and id.
pub(super) fn acl_entry(entry: &[u8; 8]) -> (u16, u16, u32) {
    let [tag, permissions, low, high] = entry.as_chunks::<2>().0 else {
        unreachable!("an eight-byte entry yields four two-byte fields")
    };
    (
        u16::from_le_bytes(*tag),
        u16::from_le_bytes(*permissions),
        u32::from_le_bytes([low[0], low[1], high[0], high[1]]),
    )
}

#[cfg(test)]
mod tests;
