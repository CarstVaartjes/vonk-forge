use std::{
    fs,
    os::unix::fs::{DirBuilderExt, MetadataExt},
    path::Path,
};

use thiserror::Error;

use crate::{
    config::AgentConfig,
    runtime_identity::{AgentRuntimeIdentity, PreparedRuntimeIdentity, RuntimeIdentityError},
};

const HELPER_UPGRADE_PENDING: &str = "/var/lib/vonk-forge/helper-upgrade.pending";

#[derive(Debug, Error)]
pub enum SelfTestError {
    #[error("agent runtime path is unsafe: {0}")]
    UnsafePath(&'static str),
    #[error(transparent)]
    Identity(#[from] RuntimeIdentityError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

pub fn run(
    config: &AgentConfig,
    executable: &Path,
    runtime_directory: &Path,
) -> Result<AgentRuntimeIdentity, SelfTestError> {
    if let Err(error) = verify_runtime_directories(&config.data_dir, runtime_directory) {
        eprintln!("vonk-agent: directory custody unavailable: {error}");
    }
    // Upgrade markers are disposable bookkeeping, not executable identity.
    // Runtime identity and the Controller activation receipt remain the proof;
    // observation/recovery must stay available even with a stale marker.
    observe_helper_upgrade_pending(Path::new(HELPER_UPGRADE_PENDING));

    Ok(PreparedRuntimeIdentity::from_executable(executable)?.mark_self_test_passed()?)
}

fn observe_helper_upgrade_pending(path: &Path) {
    if !matches!(fs::symlink_metadata(path), Err(error) if error.kind() == std::io::ErrorKind::NotFound)
    {
        eprintln!(
            "vonk-agent: helper activation unconfirmed; package owner reconciliation pending"
        );
    }
}

pub fn verify_runtime_directories(data: &Path, runtime: &Path) -> Result<(), SelfTestError> {
    let data = verify_private_directory(data, "data");
    let runtime = verify_private_directory(runtime, "runtime");
    data.and(runtime)
}

fn verify_private_directory(path: &Path, name: &'static str) -> Result<(), SelfTestError> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error.into()),
    }
    let metadata = fs::symlink_metadata(path)?;
    let effective_uid = rustix::process::geteuid().as_raw();
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        fs::rename(
            path,
            path.with_extension(format!("unavailable-{}", uuid::Uuid::new_v4())),
        )?;
        fs::DirBuilder::new().mode(0o700).create(path)?;
        fs::set_permissions(path, std::os::unix::fs::PermissionsExt::from_mode(0o700))?;
    } else if metadata.uid() != effective_uid && effective_uid != 0 {
        // The process lacks custody, so observation continues without effects.
        return Err(SelfTestError::UnsafePath(name));
    } else if metadata.mode() & 0o077 != 0 {
        fs::set_permissions(path, std::os::unix::fs::PermissionsExt::from_mode(0o700))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};

    #[test]
    fn generated_directory_miss_repairs_and_unsafe_neighbor_is_retained() {
        let root = tempfile::tempdir().unwrap();
        let data = root.path().join("data");
        let runtime = root.path().join("runtime");
        verify_runtime_directories(&data, &runtime).unwrap();
        fs::remove_dir(&runtime).unwrap();
        let sentinel = root.path().join("sentinel");
        fs::write(&sentinel, b"keep").unwrap();
        symlink(&sentinel, &runtime).unwrap();
        verify_runtime_directories(&data, &runtime).unwrap();
        assert_eq!(fs::read(&sentinel).unwrap(), b"keep");
        verify_runtime_directories(&data, &runtime).unwrap();
        assert!(runtime.is_dir());
    }

    #[test]
    fn stale_markers_do_not_disable_verified_runtime_observation() {
        let temporary = tempfile::tempdir().unwrap();
        let data = temporary.path();
        let runtime = tempfile::tempdir().unwrap();
        fs::set_permissions(data, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(runtime.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let marker = data.join("helper-upgrade.pending");
        for kind in 0..3 {
            match kind {
                0 => fs::write(&marker, b"damaged journal").unwrap(),
                1 => fs::create_dir(&marker).unwrap(),
                _ => symlink(data.join("missing"), &marker).unwrap(),
            }
            observe_helper_upgrade_pending(&marker);
            verify_runtime_directories(data, runtime.path()).unwrap();
            assert!(fs::symlink_metadata(&marker).is_ok());
            if kind == 1 {
                fs::remove_dir(&marker).unwrap();
            } else {
                fs::remove_file(&marker).unwrap();
            }
            verify_runtime_directories(data, runtime.path()).unwrap();
        }
    }
}
