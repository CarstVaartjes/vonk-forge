//! Storage for the oci boundary.

use super::*;

pub(super) fn sync_parent(parent: &Path) -> Result<(), OciError> {
    File::open(parent)?.sync_all()?;
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

/// Sum of the lengths of the regular files below `directory`; strict, for the
/// install receipt: anything unreadable or not a file or directory refuses.
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

/// What could be measured of an installation directory.
#[derive(Debug, Default)]
pub(crate) struct TreeSize {
    /// Logical bytes of everything readable; a symlink counts its own length.
    pub bytes: u64,
    /// Entries or subtrees that could not be read (not counted in `bytes`).
    pub unreadable: u64,
    /// The first such failure, for the diagnostic.
    pub first_error: Option<std::io::Error>,
}

/// An installation directory's logical size now, for inventory (metadata
/// only). Unlike the strict receipt measurement, it fails open: unreadable subtrees
/// (the helper's root-only runtime cache) are skipped and reported, and
/// symlinks are counted by their own length, never followed.
pub(crate) fn installation_tree_bytes(
    data_root: &Path,
    installation_id: &str,
) -> Result<TreeSize, OciError> {
    let root = managed_path(data_root, "installations", installation_id)?;
    let mut size = TreeSize::default();
    measure_tree(&root, &mut size, true)?;
    Ok(size)
}

fn measure_tree(directory: &Path, size: &mut TreeSize, top: bool) -> Result<(), OciError> {
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(error) if top => return Err(error.into()),
        Err(error) => {
            size.skip(error);
            return Ok(());
        }
    };
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                size.skip(error);
                continue;
            }
        };
        match entry.file_type() {
            Ok(file_type) if file_type.is_dir() => measure_tree(&entry.path(), size, false)?,
            Ok(_) => match fs::symlink_metadata(entry.path()) {
                Ok(metadata) => size.bytes = size.bytes.saturating_add(metadata.len()),
                Err(error) => size.skip(error),
            },
            Err(error) => size.skip(error),
        }
    }
    Ok(())
}

impl TreeSize {
    fn skip(&mut self, error: std::io::Error) {
        // Removed during the scan: nothing to count, nothing to report.
        if error.kind() == std::io::ErrorKind::NotFound {
            return;
        }
        self.unreadable += 1;
        self.first_error.get_or_insert(error);
    }
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
    let temporary = root.join(format!(".{name}.{}.tmp", uuid::Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    let written = (|| {
        file.write_all(value)?;
        file.sync_all()?;
        fs::rename(&temporary, root.join(name))?;
        File::open(root)?.sync_all()?;
        Ok(())
    })();
    // Only this invocation's exclusive temporary file may be unlinked. A stale
    // partial never owns the next invocation's publication path.
    if written.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    written
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

pub(crate) fn read_regular_file(path: &Path, maximum_bytes: u64) -> Result<Vec<u8>, OciError> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32)
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
