//! Storage.

use super::*;

pub(super) fn valid_runtime_cache_mount(source: &Path, roots: &ManagedRoots) -> bool {
    let Ok(relative) = source.strip_prefix(roots.agent_data.join("installations")) else {
        return false;
    };
    let components = relative.components().collect::<Vec<_>>();
    components.len() == 2
        && components[1].as_os_str() == "runtime-cache"
        && components[0]
            .as_os_str()
            .to_str()
            .is_some_and(valid_artifact_id)
}

pub(super) fn errno_io(error: rustix::io::Errno) -> std::io::Error {
    std::io::Error::from_raw_os_error(error.raw_os_error())
}

pub(super) fn retryable_reconciliation_storage_io(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::Interrupted
            | std::io::ErrorKind::WouldBlock
            | std::io::ErrorKind::TimedOut
    ) || error.raw_os_error().is_some_and(|code| {
        code == rustix::io::Errno::IO.raw_os_error()
            || code == rustix::io::Errno::NOSPC.raw_os_error()
    })
}

pub(super) fn remove_directory_contents(
    directory: &impl std::os::fd::AsFd,
    expected_device: u64,
) -> Result<(), OperationError> {
    let mut buffer = [MaybeUninit::uninit(); 8192];
    let mut entries = Vec::new();
    {
        let mut directory_entries = rustix::fs::RawDir::new(directory, &mut buffer);
        while let Some(entry) = directory_entries.next() {
            let entry = entry.map_err(errno_io)?;
            let name = entry.file_name();
            if name.to_bytes() == b"." || name.to_bytes() == b".." {
                continue;
            }
            entries.push(CString::new(name.to_bytes()).map_err(|_| OperationError::UnsafePath)?);
        }
    }
    for name in entries {
        remove_directory_entry(directory, &name, expected_device)?;
    }
    Ok(())
}

pub(super) fn remove_directory_entry(
    parent: &impl std::os::fd::AsFd,
    name: &CStr,
    expected_device: u64,
) -> Result<(), OperationError> {
    let metadata = rustix::fs::statat(parent, name, rustix::fs::AtFlags::SYMLINK_NOFOLLOW)
        .map_err(errno_io)?;
    if rustix::fs::FileType::from_raw_mode(metadata.st_mode) == rustix::fs::FileType::Directory {
        if metadata.st_dev != expected_device {
            return Err(OperationError::UnsafePath);
        }
        let child = rustix::fs::openat(
            parent,
            name,
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::DIRECTORY
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )
        .map_err(errno_io)?;
        let opened = rustix::fs::fstat(&child).map_err(errno_io)?;
        if opened.st_dev != metadata.st_dev || opened.st_ino != metadata.st_ino {
            return Err(OperationError::UnsafePath);
        }
        remove_directory_contents(&child, expected_device)?;
        rustix::fs::unlinkat(parent, name, rustix::fs::AtFlags::REMOVEDIR).map_err(errno_io)?;
    } else {
        rustix::fs::unlinkat(parent, name, rustix::fs::AtFlags::empty()).map_err(errno_io)?;
    }
    Ok(())
}

pub(super) fn require_safe_directory(
    path: &Path,
    required_owner_uid: Option<u32>,
) -> Result<(), OperationError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.mode() & 0o022 != 0
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
    {
        return Err(OperationError::UnsafePath);
    }
    Ok(())
}

pub(super) fn require_runtime_directory(
    path: &Path,
    required_owner_uid: Option<u32>,
    runtime_uid: u32,
) -> Result<(), OperationError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.mode() & 0o002 != 0
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
        || metadata.mode() & 0o020 != 0 && !exact_runtime_acl(path, runtime_uid)
    {
        return Err(OperationError::UnsafePath);
    }
    Ok(())
}

pub(super) fn ensure_private_directory(
    path: &Path,
    required_owner_uid: Option<u32>,
) -> Result<(), OperationError> {
    match fs::symlink_metadata(path) {
        Ok(_) => require_exact_directory(path, required_owner_uid, 0o700).map(|_| ()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir(path)?;
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
            require_exact_directory(path, required_owner_uid, 0o700).map(|_| ())
        }
        Err(error) => Err(error.into()),
    }
}

pub(super) fn ensure_runtime_directory(path: &Path) -> Result<(), OperationError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(OperationError::UnsafePath);
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir_all(path)?;
        }
        Err(error) => return Err(error.into()),
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() || metadata.mode() & 0o077 != 0 {
        return Err(OperationError::UnsafePath);
    }
    Ok(())
}

pub(super) fn require_exact_directory(
    path: &Path,
    required_owner_uid: Option<u32>,
    expected_mode: u32,
) -> Result<(u32, u32), OperationError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.mode() & 0o777 != expected_mode
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
        || required_owner_uid == Some(0) && metadata.gid() != 0
    {
        return Err(OperationError::UnsafePath);
    }
    Ok((metadata.uid(), metadata.gid()))
}

pub(super) fn require_agent_artifact(
    metadata: &fs::Metadata,
    required_owner_uid: Option<u32>,
) -> Result<(), OperationError> {
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.len() == 0
        || metadata.len() > MAX_ARTIFACT_BYTES
        || metadata.mode() & 0o777 != 0o600
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
    {
        return Err(OperationError::InvalidArtifact);
    }
    Ok(())
}

pub(super) fn safe_custody_file(
    metadata: &fs::Metadata,
    required_owner_uid: Option<u32>,
    expected_bytes: u64,
) -> bool {
    metadata.is_file()
        && metadata.nlink() == 1
        && metadata.len() == expected_bytes
        && metadata.mode() & 0o777 == 0o600
        && required_owner_uid.is_none_or(|uid| metadata.uid() == uid)
        && (required_owner_uid != Some(0) || metadata.gid() == 0)
}

pub(super) fn artifact_identity(metadata: &fs::Metadata) -> ArtifactIdentity {
    ArtifactIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
        uid: metadata.uid(),
        gid: metadata.gid(),
        mode: metadata.mode(),
        links: metadata.nlink(),
        bytes: metadata.len(),
        modified_seconds: metadata.mtime(),
        modified_nanoseconds: metadata.mtime_nsec(),
        changed_seconds: metadata.ctime(),
        changed_nanoseconds: metadata.ctime_nsec(),
    }
}

pub(super) fn stable_identity(metadata: &fs::Metadata) -> (u64, u64, u64, i64, i64) {
    (
        metadata.dev(),
        metadata.ino(),
        metadata.len(),
        metadata.mtime(),
        metadata.ctime(),
    )
}

pub(super) fn sync_directory(path: &Path) -> Result<(), OperationError> {
    OpenOptions::new().read(true).open(path)?.sync_all()?;
    Ok(())
}

pub(super) fn read_helper_reconciliation_receipt(
    path: &Path,
    owner_uid: Option<u32>,
) -> Result<Option<InstallationReconciliationReceipt>, OperationError> {
    let mut file = match OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    let metadata = file.metadata()?;
    if !safe_custody_file(&metadata, owner_uid, metadata.len())
        || metadata.len() > MAX_INSTALLATION_RECONCILIATION_IDENTITY_BYTES
    {
        return Err(OperationError::InvalidArtifact);
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    Read::by_ref(&mut file)
        .take(MAX_INSTALLATION_RECONCILIATION_IDENTITY_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 != metadata.len() {
        return Err(OperationError::InvalidArtifact);
    }
    let receipt: InstallationReconciliationReceipt =
        parse_strict(&bytes).map_err(|_| OperationError::InvalidArtifact)?;
    receipt
        .identity
        .validate()
        .map_err(|_| OperationError::InvalidArtifact)?;
    if receipt.schema_version != INSTALLATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION
        || receipt.installation_inode == 0
        || canonical_json(&receipt).map_err(|_| OperationError::InvalidArtifact)? != bytes
    {
        return Err(OperationError::InvalidArtifact);
    }
    Ok(Some(receipt))
}

pub(super) fn write_helper_reconciliation_receipt(
    root: &Path,
    path: &Path,
    receipt: &InstallationReconciliationReceipt,
) -> Result<(), OperationError> {
    receipt
        .identity
        .validate()
        .map_err(|_| OperationError::InvalidOperation)?;
    let bytes = canonical_json(receipt).map_err(|_| OperationError::InvalidOperation)?;
    if receipt.schema_version != INSTALLATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION
        || receipt.installation_inode == 0
        || bytes.len() as u64 > MAX_INSTALLATION_RECONCILIATION_IDENTITY_BYTES
    {
        return Err(OperationError::InvalidOperation);
    }
    let temporary = root.join(format!(".receipt-{}.tmp", uuid::Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    fs::rename(&temporary, path)?;
    sync_directory(root)
}

impl ManagedRoots {
    pub fn under(data: &Path) -> Self {
        Self {
            data: data.to_path_buf(),
            incoming: data.join("incoming"),
            package_custody: data.join("helper/package-candidates"),
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

    pub fn with_package_custody(mut self, root: &Path) -> Self {
        self.package_custody = root.to_path_buf();
        self
    }
}
