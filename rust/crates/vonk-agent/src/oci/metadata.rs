//! Metadata for the oci boundary.

use super::*;

pub(super) fn metadata_stable(before: &fs::Metadata, after: &fs::Metadata) -> bool {
    before.dev() == after.dev()
        && before.ino() == after.ino()
        && before.len() == after.len()
        && timestamp_ns(before.mtime(), before.mtime_nsec())
            == timestamp_ns(after.mtime(), after.mtime_nsec())
        && timestamp_ns(before.ctime(), before.ctime_nsec())
            == timestamp_ns(after.ctime(), after.ctime_nsec())
}

pub(super) fn write_installation_metadata(
    data_root: &Path,
    installation: &Path,
    plan: &CompiledExecutionPlan,
) -> Result<(), OciError> {
    let models = installation.join("models");
    let unique_artifacts = unique_plan_artifacts(plan);
    let mut entries = Vec::with_capacity(unique_artifacts.len());
    for artifact in unique_artifacts {
        let path = models.join(&artifact.selection_id).join(&artifact.path);
        let (_, metadata) = open_trusted_model_file(
            &path,
            artifact.size_bytes,
            shared_store_inode(data_root, &artifact.sha256),
        )?;
        entries.push(installation_metadata_entry(artifact, &metadata));
    }
    sort_metadata_entries(&mut entries);
    atomic_write(
        installation,
        INSTALLATION_METADATA_FILE,
        &serde_json::to_vec(&InstallationMetadataReceipt {
            schema_version: INSTALLATION_METADATA_SCHEMA_VERSION,
            entries,
        })?,
    )?;
    Ok(())
}

pub(super) fn read_installation_metadata(
    installation: &Path,
) -> Result<Option<InstallationMetadataReceipt>, OciError> {
    let path = installation.join(INSTALLATION_METADATA_FILE);
    let metadata = match fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    if !trusted_receipt_metadata(&metadata) || metadata.len() > MAX_COMPILED_DOCUMENT_BYTES {
        return Ok(None);
    }
    let value = match read_regular_file(&path, MAX_COMPILED_DOCUMENT_BYTES) {
        Ok(value) => value,
        Err(OciError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(None);
        }
        Err(_) => return Ok(None),
    };
    let receipt = match serde_json::from_slice(&value) {
        Ok(receipt) => receipt,
        Err(_) => return Ok(None),
    };
    Ok(Some(receipt))
}

pub(super) fn receipt_matches_plan(
    receipt: &InstallationMetadataReceipt,
    plan: &CompiledExecutionPlan,
) -> bool {
    let unique_artifacts = unique_plan_artifacts(plan);
    if receipt.schema_version != INSTALLATION_METADATA_SCHEMA_VERSION
        || receipt.entries.len() != unique_artifacts.len()
    {
        return false;
    }
    let mut observed = BTreeMap::new();
    for entry in &receipt.entries {
        if observed
            .insert(
                (entry.selection_id.as_str(), entry.path.as_str()),
                (&entry.sha256, entry.size_bytes),
            )
            .is_some()
        {
            return false;
        }
    }
    unique_artifacts.iter().all(|artifact| {
        observed.get(&(artifact.selection_id.as_str(), artifact.path.as_str()))
            == Some(&(&artifact.sha256, artifact.size_bytes))
    })
}

pub(super) fn unique_plan_artifacts(
    plan: &CompiledExecutionPlan,
) -> Vec<&crate::workloads::CompiledModelArtifact> {
    let mut seen = BTreeSet::new();
    plan.artifacts
        .iter()
        .filter(|artifact| seen.insert((artifact.selection_id.as_str(), artifact.path.as_str())))
        .collect()
}

pub(super) fn installation_metadata_entry(
    artifact: &crate::workloads::CompiledModelArtifact,
    metadata: &fs::Metadata,
) -> InstallationMetadataEntry {
    InstallationMetadataEntry {
        selection_id: artifact.selection_id.clone(),
        path: artifact.path.clone(),
        sha256: artifact.sha256.clone(),
        size_bytes: artifact.size_bytes,
        dev: metadata.dev(),
        ino: metadata.ino(),
        mtime_ns: timestamp_ns(metadata.mtime(), metadata.mtime_nsec()),
        ctime_ns: timestamp_ns(metadata.ctime(), metadata.ctime_nsec()),
    }
}

pub(super) fn metadata_matches_receipt(
    metadata: &fs::Metadata,
    receipt: &InstallationMetadataEntry,
) -> bool {
    metadata.dev() == receipt.dev
        && metadata.ino() == receipt.ino
        && metadata.len() == receipt.size_bytes
        && timestamp_ns(metadata.mtime(), metadata.mtime_nsec()) == receipt.mtime_ns
        // Every link another installation adds or drops moves the change time
        // of an inode they share, so it says nothing about a shared object. A
        // private inode keeps the exact change-time binding.
        && (metadata.nlink() > 1
            || timestamp_ns(metadata.ctime(), metadata.ctime_nsec()) == receipt.ctime_ns)
}

/// `(device, inode)` of the shared store object an installation's model file
/// may be a hard link of.
pub(super) type SharedInode = (u64, u64);

pub(super) fn store_object_path(data_root: &Path, sha256: &str) -> PathBuf {
    data_root.join("distribution").join("models").join(sha256)
}

/// The inode of the store object named `sha256`, when the agent owns a regular
/// file there. An installation's model file with more than one link is trusted
/// only when it is exactly this inode; a private copy keeps a single link.
pub(super) fn shared_store_inode(data_root: &Path, sha256: &str) -> Option<SharedInode> {
    if !lower_hex(sha256, 64) {
        return None;
    }
    let metadata = fs::symlink_metadata(store_object_path(data_root, sha256)).ok()?;
    (metadata.file_type().is_file() && metadata.uid() == rustix::process::geteuid().as_raw())
        .then(|| (metadata.dev(), metadata.ino()))
}

pub(super) fn trusted_model_shape(
    metadata: &fs::Metadata,
    expected_bytes: u64,
    shared: Option<SharedInode>,
) -> bool {
    metadata.file_type().is_file()
        && !metadata.file_type().is_symlink()
        && (metadata.nlink() == 1 || shared == Some((metadata.dev(), metadata.ino())))
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && metadata.len() == expected_bytes
}

pub(super) fn trusted_model_file(
    file: &File,
    metadata: &fs::Metadata,
    expected_bytes: u64,
    shared: Option<SharedInode>,
) -> bool {
    if !trusted_model_shape(metadata, expected_bytes, shared) {
        return false;
    }
    match metadata.mode() & 0o777 {
        0o600 => true,
        0o640 => exact_runtime_file_acl(file),
        _ => false,
    }
}

pub(crate) fn exact_runtime_file_acl(file: &impl std::os::fd::AsFd) -> bool {
    const ACL_VERSION: u32 = 0x0002;
    const USER_OBJ: u16 = 0x0001;
    const USER: u16 = 0x0002;
    const GROUP_OBJ: u16 = 0x0004;
    const MASK: u16 = 0x0010;
    const OTHER: u16 = 0x0020;
    let mut value = [0_u8; 4 + 5 * 8];
    let Ok(length) = rustix::fs::fgetxattr(file, "system.posix_acl_access", &mut value) else {
        return false;
    };
    if length != value.len() || u32::from_le_bytes(value[..4].try_into().unwrap()) != ACL_VERSION {
        return false;
    }
    let mut user_object = false;
    let mut runtime_user = false;
    let mut group_object = false;
    let mut mask = false;
    let mut other = false;
    for entry in value[4..].as_chunks::<8>().0.iter() {
        let [tag, permissions, low, high] = entry.as_chunks::<2>().0 else {
            unreachable!("an eight-byte entry yields four two-byte fields")
        };
        let tag = u16::from_le_bytes(*tag);
        let permissions = u16::from_le_bytes(*permissions);
        let identifier = u32::from_le_bytes([low[0], low[1], high[0], high[1]]);
        match tag {
            USER_OBJ if identifier == u32::MAX => user_object = permissions == 0o6,
            USER if identifier == TRUSTED_RUNTIME_UID => runtime_user = permissions == 0o4,
            GROUP_OBJ if identifier == u32::MAX => group_object = permissions == 0,
            MASK if identifier == u32::MAX => mask = permissions == 0o4,
            OTHER if identifier == u32::MAX => other = permissions == 0,
            _ => return false,
        }
    }
    user_object && runtime_user && group_object && mask && other
}

pub(super) fn open_trusted_model_file(
    path: &Path,
    expected_bytes: u64,
    shared: Option<SharedInode>,
) -> Result<(File, fs::Metadata), OciError> {
    let path_metadata = fs::symlink_metadata(path)?;
    if !trusted_model_shape(&path_metadata, expected_bytes, shared) {
        return Err(OciError::Artifact);
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(path)?;
    let opened_metadata = file.metadata()?;
    if !trusted_model_file(&file, &opened_metadata, expected_bytes, shared)
        || opened_metadata.dev() != path_metadata.dev()
        || opened_metadata.ino() != path_metadata.ino()
    {
        return Err(OciError::Artifact);
    }
    Ok((file, opened_metadata))
}

pub(super) fn trusted_receipt_metadata(metadata: &fs::Metadata) -> bool {
    metadata.file_type().is_file()
        && !metadata.file_type().is_symlink()
        && metadata.nlink() == 1
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && metadata.mode() & 0o777 == 0o600
}

/// Nanoseconds since the epoch, saturating at the i64 range the receipt's
/// contract allows (year 2262).
pub(super) fn timestamp_ns(seconds: i64, nanoseconds: i64) -> i64 {
    seconds
        .saturating_mul(1_000_000_000)
        .saturating_add(nanoseconds)
}

/// The order a receipt lists its entries in: by selection, then path, then the
/// remaining identity fields.
pub(super) fn sort_metadata_entries(entries: &mut [InstallationMetadataEntry]) {
    entries.sort_by(|left, right| {
        let key = |entry: &InstallationMetadataEntry| {
            (
                entry.selection_id.clone(),
                entry.path.clone(),
                entry.sha256.clone(),
                entry.size_bytes,
                entry.dev,
                entry.ino,
                entry.mtime_ns,
                entry.ctime_ns,
            )
        };
        key(left).cmp(&key(right))
    });
}

pub(super) fn materialized_model_bytes(
    data_root: &Path,
    installation_id: &str,
    spec: &CompiledExecutionPlan,
) -> Result<u64, OciError> {
    let models = managed_path(data_root, "installations", installation_id)?.join("models");
    let metadata = match fs::symlink_metadata(&models) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(error) => return Err(error.into()),
    };
    if metadata.file_type().is_symlink() || !metadata.file_type().is_dir() {
        return Err(OciError::Artifact);
    }
    unique_plan_artifacts(spec)
        .into_iter()
        .try_fold(0_u64, |total, artifact| {
            let path = models.join(&artifact.selection_id).join(&artifact.path);
            let metadata = fs::symlink_metadata(path)?;
            if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
                return Err(OciError::Artifact);
            }
            total.checked_add(metadata.len()).ok_or(OciError::Artifact)
        })
}

/// Bytes of the plan's model files the shared store already holds in full, so
/// an installation links them instead of writing them. An estimate for the
/// capacity check only; the install itself verifies every object it uses.
pub(super) fn linkable_model_bytes(data_root: &Path, plan: &CompiledExecutionPlan) -> u64 {
    unique_plan_artifacts(plan)
        .into_iter()
        .filter(|artifact| {
            lower_hex(&artifact.sha256, 64)
                && fs::symlink_metadata(store_object_path(data_root, &artifact.sha256)).is_ok_and(
                    |metadata| {
                        metadata.file_type().is_file() && metadata.len() == artifact.size_bytes
                    },
                )
        })
        .map(|artifact| artifact.size_bytes)
        .fold(0_u64, u64::saturating_add)
}
