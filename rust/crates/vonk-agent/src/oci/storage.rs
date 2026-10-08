//! Storage for the oci boundary.

use super::*;

pub(super) fn sync_parent(parent: &Path) -> Result<(), OciError> {
    File::open(parent)?.sync_all()?;
    Ok(())
}

/// Release the kernel's page cache for a file this agent has finished with.
///
/// A materialized model is hundreds of gigabytes.  Where the device's memory is
/// the system's unified memory, the page cache holding those bytes is memory the
/// GPU cannot allocate from: after one 199 GB installation the workload's own
/// loader read about 950 MB of free device memory and refused the 1.27 GB
/// staging buffer its checkpoint needs, on a node that reported 126 GB
/// available.  The agent wrote those bytes, so releasing them is the agent's
/// job, and it is a hint: the file's content is unaffected and the next read
/// simply caches again.
pub(super) fn release_page_cache(path: &Path) -> Result<(), OciError> {
    if !path.is_file() {
        return Err(OciError::Artifact);
    }
    let file = File::open(path)?;
    rustix::fs::fadvise(&file, 0, None, rustix::fs::Advice::DontNeed)
        .map_err(std::io::Error::from)?;
    Ok(())
}

pub(super) fn spec_references_model(
    spec: &CompiledExecutionPlan,
    model_content_sha256: &str,
) -> bool {
    spec.artifacts
        .iter()
        .any(|artifact| artifact.model.content_sha256 == model_content_sha256)
}

pub(super) fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Sum of the lengths of the regular files below `directory`.
pub(super) fn tree_bytes(directory: &Path) -> Result<u64, OciError> {
    let mut total = 0_u64;
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let size = if file_type.is_dir() {
            tree_bytes(&entry.path())?
        } else if file_type.is_file() {
            entry.metadata()?.len()
        } else {
            return Err(OciError::Artifact);
        };
        total = total.checked_add(size).ok_or(OciError::Artifact)?;
    }
    Ok(total)
}

pub(super) fn visit_files(
    root: &Path,
    directory: &Path,
    files: &mut BTreeMap<String, String>,
    total: &mut u64,
) -> Result<(), OciError> {
    let mut entries = fs::read_dir(directory)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);
    for entry in entries {
        let metadata = entry.file_type()?;
        let path = entry.path();
        if metadata.is_symlink() {
            return Err(OciError::Artifact);
        }
        if metadata.is_dir() {
            visit_files(root, &path, files, total)?;
        } else if metadata.is_file() {
            let relative = path.strip_prefix(root).map_err(|_| OciError::Artifact)?;
            let name = relative
                .to_str()
                .ok_or(OciError::Artifact)?
                .replace('\\', "/");
            if name.contains("..") {
                return Err(OciError::Artifact);
            }
            let mut file = File::open(&path)?;
            let mut hasher = Sha256::new();
            let mut buffer = [0_u8; 64 * 1024];
            let expected_bytes = file.metadata()?.len();
            let mut remaining_bytes = expected_bytes;
            while remaining_bytes > 0 {
                let wave_bytes = remaining_bytes.min(buffer.len() as u64) as usize;
                let read = file.read(&mut buffer[..wave_bytes])?;
                if read == 0 {
                    return Err(OciError::Artifact);
                }
                remaining_bytes -= read as u64;
                hasher.update(&buffer[..read]);
                *total = total.checked_add(read as u64).ok_or(OciError::Artifact)?;
            }
            if file.metadata()?.len() != expected_bytes {
                return Err(OciError::Artifact);
            }
            files.insert(name, hex::encode(hasher.finalize()));
        } else {
            return Err(OciError::Artifact);
        }
    }
    Ok(())
}

pub(super) fn atomic_write(root: &Path, name: &str, value: &[u8]) -> Result<(), OciError> {
    let temporary: PathBuf = root.join(format!(".{name}.{}.tmp", std::process::id()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(value)?;
    file.sync_all()?;
    fs::rename(temporary, root.join(name))?;
    Ok(())
}

/// Prepare the agent-owned temporary mount boundary without traversing its
/// contents. The helper and workload create private directories below it;
/// only the authorized helper can remove them after proving the old container
/// absent. A retained start must never erase a live workload's temporary work.
pub(super) fn ensure_runtime_tmp(outputs: &Path) -> Result<(), OciError> {
    let output_metadata = fs::symlink_metadata(outputs)?;
    if output_metadata.file_type().is_symlink() || !output_metadata.is_dir() {
        return Err(OciError::Artifact);
    }
    let temporary = outputs.join("tmp");
    match fs::symlink_metadata(&temporary) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(OciError::Artifact);
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir(&temporary)?;
        }
        Err(error) => return Err(error.into()),
    }
    fs::set_permissions(&temporary, fs::Permissions::from_mode(0o700))?;
    let metadata = fs::symlink_metadata(&temporary)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() || metadata.mode() & 0o077 != 0 {
        return Err(OciError::Artifact);
    }
    Ok(())
}

pub(super) fn read_regular_file(path: &Path, maximum_bytes: u64) -> Result<Vec<u8>, OciError> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.file_type().is_file() || metadata.len() > maximum_bytes {
        return Err(OciError::Artifact);
    }
    let mut value = Vec::with_capacity(metadata.len() as usize);
    Read::by_ref(&mut file)
        .take(maximum_bytes.saturating_add(1))
        .read_to_end(&mut value)?;
    if value.len() as u64 > maximum_bytes {
        return Err(OciError::Artifact);
    }
    Ok(value)
}

pub(super) fn canonical_uuid(value: &str) -> bool {
    uuid::Uuid::parse_str(value).is_ok_and(|parsed| parsed.to_string() == value)
}
