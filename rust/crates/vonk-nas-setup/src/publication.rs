//! Durable publication intent. A complete candidate precedes every visible
//! replacement; recovery replays it before any credential consumer validates.
use super::filesystem::require_real_directory;
use super::*;

const INTENT: &str = ".vonk-publication";
const VERIFIED: &str = ".vonk-verified-secrets";

pub(super) fn secret_paths(payload: &CanonicalTemplatePayload) -> Vec<&str> {
    let generated = generated_secrets(payload);
    let mut paths: Vec<_> = payload
        .secrets
        .iter()
        .map(|value| value.file.as_str())
        .collect();
    paths.extend(
        generated
            .random_text
            .iter()
            .map(|value| value.file.as_str()),
    );
    paths.extend(
        generated
            .ed25519_pkcs8_pem
            .iter()
            .map(|value| value.file.as_str()),
    );
    paths.extend(
        generated
            .postgres_urls
            .iter()
            .map(|value| value.file.as_str()),
    );
    if let Some(request) = &payload.step_ca_controller {
        paths.extend(step_ca_files(&request.files));
    }
    paths.sort_unstable();
    paths.dedup();
    paths
}

/// Do not follow a damaged intermediate directory or a secret symlink.
/// Unavailable stored bytes are an observation miss, never new authority.
pub(super) fn read_member(root: &Path, relative: &str) -> Option<String> {
    let metadata = fs::symlink_metadata(root).ok()?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return None;
    }
    let mut path = root.to_path_buf();
    for component in Path::new(relative).components() {
        let Component::Normal(component) = component else {
            return None;
        };
        path.push(component);
        let metadata = fs::symlink_metadata(&path).ok()?;
        if metadata.file_type().is_symlink() {
            return None;
        }
    }
    let metadata = fs::metadata(&path).ok()?;
    if !metadata.is_file() || metadata.len() > 256 * 1024 {
        return None;
    }
    fs::read_to_string(path).ok()
}

pub(super) fn unknown() -> SetupError {
    SetupError::Io(io::Error::new(
        io::ErrorKind::WouldBlock,
        vonk_agent_protocol::generated::WaitReason::ObservationUnavailable.to_string(),
    ))
}

pub(super) fn copy_candidate(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    target: &Path,
) -> Result<(), SetupError> {
    create_secure_directory(target)?;
    let active = bundle.join("secrets");
    let verified = bundle.join(VERIFIED);
    for path in secret_paths(payload) {
        if let Some(value) = read_member(&active, path).or_else(|| read_member(&verified, path)) {
            write_secret_file(target, path, value.as_bytes())?;
        }
    }
    Ok(())
}

pub(super) fn remember_verified(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
) -> Result<(), SetupError> {
    let staging = create_staging_directory(bundle)?;
    let snapshot = staging.join("secrets");
    copy_candidate(payload, bundle, &snapshot)?;
    let template = serde_json::to_vec(payload).map_err(|_| unknown())?;
    write_new_file(&snapshot.join(".template.json"), &template, 0o600)?;
    if let Ok(document) = fs::read(bundle.join(".env")) {
        write_new_file(&snapshot.join(".env"), &document, 0o600)?;
    }
    sync_tree(payload, &snapshot)?;
    let verified = bundle.join(VERIFIED);
    if fs::symlink_metadata(&verified).is_ok() {
        // Preserve the former generation until the new one is durable. It is
        // not consulted for authority and cannot gate a later publication.
        fs::rename(&verified, staging.join("previous"))?;
    }
    fs::rename(&snapshot, &verified)?;
    sync_directory(bundle)?;
    let _ = fs::remove_dir_all(&staging);
    Ok(())
}

pub(super) fn sync_tree(payload: &CanonicalTemplatePayload, root: &Path) -> Result<(), SetupError> {
    for relative in secret_paths(payload) {
        let mut parent = root.join(relative);
        parent.pop();
        // Payload paths are finite; flush each newly created ancestor before
        // the root becomes visible through the publication intent.
        for directory in parent.ancestors().take_while(|path| path.starts_with(root)) {
            if directory.is_dir() {
                sync_directory(directory)?;
            }
        }
    }
    sync_directory(root)
}

fn replay_complete<F: FnMut(&str)>(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    mut after_member: F,
) -> Result<(), SetupError> {
    let intent = bundle.join(INTENT);
    match fs::symlink_metadata(&intent) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
        Ok(_) => return Err(SetupError::MissingBundle),
        Err(error) => return Err(error.into()),
    }
    let source = intent.join("secrets");
    require_real_directory(&source)?;
    let environment = parse_environment(&intent.join(".env"))?;
    // Validate the complete group before replay, even if an earlier process
    // died between the certificate and key replacements.
    if let Some(request) = &payload.step_ca_controller {
        let files = step_ca_files(&request.files)
            .into_iter()
            .map(|path| {
                read_member(&source, path)
                    .map(|value| (path.to_owned(), value))
                    .ok_or(SetupError::MissingBundle)
            })
            .collect::<Result<Vec<_>, _>>()?;
        validate_upgrade_pki_material(request, &environment, &files)?;
    }
    let root = bundle.join("secrets");
    require_real_directory(&root)?;
    for relative in secret_paths(payload) {
        if let Some(content) = read_member(&source, relative) {
            filesystem::ensure_nested_parent(&root, Path::new(relative), relative)?;
            atomic_replace(&root.join(relative), content.as_bytes(), 0o600)?;
            after_member(relative);
        }
    }
    let document = render_owned_environment(&environment)?;
    atomic_replace(&bundle.join(".env"), document.as_bytes(), 0o600)?;
    remember_verified(payload, bundle)?;
    // Retirement is an atomic rename, not recursive deletion of the intent.
    // A death during disposal cannot leave a partial journal blocking requests.
    let retired = create_staging_directory(bundle)?;
    fs::rename(&intent, retired.join("published"))?;
    sync_directory(bundle)?;
    let _ = fs::remove_dir_all(&retired);
    Ok(())
}

pub(super) fn publish(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    staging: &Path,
) -> Result<(), SetupError> {
    sync_tree(payload, &staging.join("secrets"))?;
    sync_directory(staging)?;
    fs::rename(staging, bundle.join(INTENT))?;
    sync_directory(bundle)?;
    replay(payload, bundle)
}

/// The OS owns lock lifetime; death releases it without stale busy bookkeeping.
pub(super) fn acquire_owner(root: &Path) -> Result<File, SetupError> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true);
    filesystem::set_open_mode(&mut options, 0o600);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32);
    }
    let path = root.join(".vonk-setup.lock");
    for attempt in 0..3 {
        if fs::symlink_metadata(&path)
            .is_ok_and(|metadata| !metadata.is_file() || metadata.file_type().is_symlink())
        {
            // A non-file cannot own a flock. Retain it without following it.
            let retired = create_staging_directory(root)?;
            fs::rename(&path, retired.join("preserved"))?;
            sync_directory(root)?;
        }
        match options.open(&path) {
            Ok(owner) => match rustix::fs::flock(
                &owner,
                rustix::fs::FlockOperation::NonBlockingLockExclusive,
            ) {
                Ok(()) => return Ok(owner),
                Err(rustix::io::Errno::WOULDBLOCK) => {}
                Err(error)
                    if error == rustix::io::Errno::ACCES || error == rustix::io::Errno::PERM =>
                {
                    return Err(io::Error::from_raw_os_error(error.raw_os_error()).into());
                }
                Err(_) => {}
            },
            Err(error) if error.kind() == io::ErrorKind::PermissionDenied => {
                return Err(error.into());
            }
            Err(_) => {}
        }
        if attempt < 2 {
            std::thread::sleep(std::time::Duration::from_millis(50 << attempt));
        }
    }
    Err(unknown())
}

pub(super) fn replay(payload: &CanonicalTemplatePayload, bundle: &Path) -> Result<(), SetupError> {
    match replay_complete(payload, bundle, |_| {}) {
        Ok(()) => Ok(()),
        Err(error @ SetupError::PermissionDenied { .. }) => Err(error),
        Err(SetupError::Io(error)) if error.kind() == io::ErrorKind::PermissionDenied => {
            Err(error.into())
        }
        Err(
            SetupError::MissingBundle
            | SetupError::InvalidSecretMaterial(_)
            | SetupError::UnsafeDestination(_)
            | SetupError::InvalidPayload(_),
        ) => {
            // Corrupt bookkeeping is not authority. Preserve it and let the
            // current request reconstruct from active/last verified material.
            let intent = bundle.join(INTENT);
            if fs::symlink_metadata(&intent).is_ok() {
                let retired = create_staging_directory(bundle)?;
                fs::rename(&intent, retired.join("retained-intent"))?;
                sync_directory(bundle)?;
                restore_verified(payload, bundle)?;
            }
            Err(unknown())
        }
        Err(error) => Err(error),
    }
}

#[cfg(test)]
#[path = "publication/tests/mod.rs"]
mod tests;

pub(super) fn newly_requested(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    path: &str,
) -> bool {
    let Some(document) = read_member(&bundle.join(VERIFIED), ".template.json") else {
        return false;
    };
    let Ok(previous) = parse_template_payload(document.as_bytes()) else {
        return false;
    };
    secret_paths(payload).contains(&path) && !secret_paths(&previous).contains(&path)
}

pub(super) fn newly_requested_environment(bundle: &Path, name: &str) -> bool {
    let Some(document) = read_member(&bundle.join(VERIFIED), ".template.json") else {
        return false;
    };
    let Ok(previous) = parse_template_payload(document.as_bytes()) else {
        return false;
    };
    !previous
        .required_values
        .iter()
        .any(|value| value.env == name)
}

/// A damaged publication intent may have exposed part of its group. Roll back
/// only to a complete cryptographically verified retained group, before normal
/// validation. An unrelated mismatch without an intent is never overridden.
fn restore_verified(payload: &CanonicalTemplatePayload, bundle: &Path) -> Result<(), SetupError> {
    let retained = bundle.join(VERIFIED);
    let environment = parse_environment(&retained.join(".env"))?;
    if let Some(request) = &payload.step_ca_controller {
        let files = step_ca_files(&request.files)
            .into_iter()
            .map(|path| {
                read_member(&retained, path)
                    .map(|value| (path.to_owned(), value))
                    .ok_or_else(unknown)
            })
            .collect::<Result<Vec<_>, _>>()?;
        validate_upgrade_pki_material(request, &environment, &files)?;
    }
    let staging = tempfile::Builder::new()
        .prefix(".vonk-forge.setup-")
        .tempdir_in(bundle)?;
    let candidate = staging.path().join("secrets");
    create_secure_directory(&candidate)?;
    for path in secret_paths(payload) {
        if let Some(value) = read_member(&retained, path) {
            write_secret_file(&candidate, path, value.as_bytes())?;
        }
    }
    write_new_file(
        &staging.path().join(".env"),
        render_owned_environment(&environment)?.as_bytes(),
        0o600,
    )?;
    sync_tree(payload, &candidate)?;
    sync_directory(staging.path())?;
    fs::rename(staging.path(), bundle.join(INTENT))?;
    sync_directory(bundle)?;
    replay_complete(payload, bundle, |_| {})
}
