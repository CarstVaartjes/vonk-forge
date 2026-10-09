use std::{fs, os::unix::fs::MetadataExt, path::Path};

use thiserror::Error;

use crate::{
    client::{AgentHttpClient, ClientError},
    config::AgentConfig,
    runtime_identity::{AgentRuntimeIdentity, PreparedRuntimeIdentity, RuntimeIdentityError},
};

const HELPER_UPGRADE_PENDING: &str = "/var/lib/vonk-forge/helper-upgrade.pending";

#[derive(Debug, Error)]
pub enum SelfTestError {
    #[error("agent runtime path is unsafe: {0}")]
    UnsafePath(&'static str),
    #[error(transparent)]
    Client(#[from] ClientError),
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
    verify_runtime_directories(&config.data_dir, runtime_directory)?;
    // Upgrade markers are disposable bookkeeping, not executable identity.
    // Runtime identity and the Controller activation receipt remain the proof;
    // observation/recovery must stay available even with a stale marker.
    observe_helper_upgrade_pending(Path::new(HELPER_UPGRADE_PENDING));
    AgentHttpClient::from_config(config)?;
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

fn verify_runtime_directories(data: &Path, runtime: &Path) -> Result<(), SelfTestError> {
    verify_private_directory(data, "data")?;
    verify_private_directory(runtime, "runtime")
}

fn verify_private_directory(path: &Path, name: &'static str) -> Result<(), SelfTestError> {
    let metadata = fs::symlink_metadata(path)?;
    let effective_uid = rustix::process::geteuid().as_raw();
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != effective_uid && effective_uid != 0
        || metadata.mode() & 0o077 != 0
    {
        return Err(SelfTestError::UnsafePath(name));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};

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
