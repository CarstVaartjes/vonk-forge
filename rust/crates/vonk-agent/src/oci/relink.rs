//! Private model copies from before installations shared files become links of
//! the shared store object.
//!
//! An installation made before model files were shared holds its own copy of
//! each file. Its receipt binds that copy's exact inode, size and times to the
//! file's digest, so a copy that still matches its receipt is the receipted
//! content: no byte is read or hashed again. The copy is replaced by a link of
//! the store object of that digest, or, when the store has none, becomes that
//! object, so the next installation of the same model links it too.

use super::*;

/// What one relink pass did.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Relinked {
    /// Private copies that now are the shared store object.
    pub files: u64,
    /// Bytes of the private copies replaced by a link of an existing object.
    pub freed_bytes: u64,
    /// The pass reached its deadline with installations left to look at.
    pub pending: bool,
}

/// Relink the receipt-matched private model copies of every installation that
/// no started run names (`started`), until `deadline`. Runs between claims, so
/// no install or start is writing an installation meanwhile.
pub fn relink_private_model_copies(
    data_root: &Path,
    started: &BTreeSet<String>,
    deadline: Instant,
) -> Relinked {
    let mut relinked = Relinked::default();
    let owner = rustix::process::geteuid().as_raw();
    let owned = |path: &Path| {
        fs::symlink_metadata(path).is_ok_and(|metadata| {
            metadata.is_dir() && !metadata.file_type().is_symlink() && metadata.uid() == owner
        })
    };
    let store = data_root.join("distribution/models");
    let installations = data_root.join("installations");
    if !owned(&store) || !owned(&installations) {
        return relinked;
    }
    let Ok(entries) = fs::read_dir(&installations) else {
        return relinked;
    };
    let mut names: Vec<String> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| canonical_uuid(name) && !started.contains(name))
        .collect();
    names.sort();
    for name in names {
        if Instant::now() >= deadline {
            relinked.pending = true;
            break;
        }
        let installation = installations.join(&name);
        if !owned(&installation) {
            continue;
        }
        if let Err(error) = relink_installation(data_root, &installation, &mut relinked) {
            crate::store_retention::report_unreadable_installation(data_root, &name, &error);
        }
    }
    if relinked.files > 0 {
        eprintln!(
            "vonk-agent: store retention relinked {} private model copies to the shared store, freeing {} bytes",
            relinked.files, relinked.freed_bytes
        );
    }
    relinked
}

/// Relink one installation's private copies and record each new link in its
/// receipt in one atomic write. A file already linked to its store object but
/// still receipted as the old copy (a pass ended between the two) is recorded
/// as well.
fn relink_installation(
    data_root: &Path,
    installation: &Path,
    relinked: &mut Relinked,
) -> Result<(), OciError> {
    let name = installation
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or(OciError::Artifact)?;
    let plan = match read_persisted_plan(data_root, name) {
        Ok(plan) => plan,
        // Still being prepared: it holds no receipted copy.
        Err(OciError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    // Without a receipt for this plan no file is proven to be its digest's bytes.
    let Some(mut receipt) = read_installation_metadata(installation)?
        .filter(|receipt| receipt_matches_plan(receipt, &plan))
    else {
        return Ok(());
    };
    let models = installation.join("models");
    let mut changed = false;
    for entry in &mut receipt.entries {
        if !lower_hex(&entry.sha256, 64) {
            continue;
        }
        let destination = models.join(&entry.selection_id).join(&entry.path);
        if !destination.starts_with(&models) {
            continue;
        }
        let Ok(metadata) = fs::symlink_metadata(&destination) else {
            continue;
        };
        let shared = shared_store_inode(data_root, &entry.sha256);
        if shared == Some((metadata.dev(), metadata.ino())) {
            if !metadata_matches_receipt(&metadata, entry) {
                rebind(entry, &metadata);
                changed = true;
            }
            continue;
        }
        if metadata.nlink() != 1 || !metadata_matches_receipt(&metadata, entry) {
            continue;
        }
        let Ok((_, copy)) = open_trusted_model_file(&destination, entry.size_bytes, None) else {
            continue;
        };
        if !metadata_matches_receipt(&copy, entry) {
            continue;
        }
        let object = store_object_path(data_root, &entry.sha256);
        let freed = if shared.is_some() {
            // An object of another size or shape is the distribution's to
            // isolate; the copy stays until it is a trusted object.
            if !link_store_object(&object, &destination, entry.size_bytes, shared)? {
                continue;
            }
            true
        } else {
            // The store has no object of this digest: the receipted copy
            // becomes it. A name that appeared meanwhile is not replaced.
            match fs::hard_link(&destination, &object) {
                Ok(()) => sync_parent(object.parent().ok_or(OciError::Artifact)?)?,
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error.into()),
            }
            false
        };
        rebind(entry, &fs::symlink_metadata(&destination)?);
        changed = true;
        relinked.files += 1;
        if freed {
            relinked.freed_bytes = relinked.freed_bytes.saturating_add(entry.size_bytes);
        }
    }
    if changed {
        sort_metadata_entries(&mut receipt.entries);
        atomic_write(
            installation,
            INSTALLATION_METADATA_FILE,
            &serde_json::to_vec(&receipt)?,
        )?;
        File::open(installation)?.sync_all()?;
    }
    Ok(())
}

/// Replace `destination` with a link of the trusted store object, as an
/// install does (verified inode, atomic rename). False, and nothing changed,
/// when the object is not a trusted object of that size.
fn link_store_object(
    object: &Path,
    destination: &Path,
    size_bytes: u64,
    shared: Option<SharedInode>,
) -> Result<bool, OciError> {
    let Ok((_, source)) = open_trusted_model_file(object, size_bytes, shared) else {
        return Ok(false);
    };
    let parent = destination.parent().ok_or(OciError::Artifact)?;
    let temporary = parent.join(format!(".{}.relink.partial", uuid::Uuid::new_v4()));
    fs::hard_link(object, &temporary)?;
    let replaced = fs::symlink_metadata(&temporary)
        .map_err(OciError::from)
        .and_then(|linked| {
            if linked.dev() != source.dev() || linked.ino() != source.ino() {
                return Err(OciError::Artifact);
            }
            fs::rename(&temporary, destination)?;
            sync_parent(parent)
        });
    if replaced.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    replaced.map(|()| true)
}

/// The receipt entry of the same file, now bound to `metadata`'s inode.
fn rebind(entry: &mut InstallationMetadataEntry, metadata: &fs::Metadata) {
    entry.dev = metadata.dev();
    entry.ino = metadata.ino();
    entry.mtime_ns = timestamp_ns(metadata.mtime(), metadata.mtime_nsec());
    entry.ctime_ns = timestamp_ns(metadata.ctime(), metadata.ctime_nsec());
}

#[cfg(test)]
mod tests;
