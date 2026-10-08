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
    verify_private_directory(&config.data_dir, "data")?;
    verify_private_directory(runtime_directory, "runtime")?;
    // Activation is acknowledged separately against the installed package.
    // A marker is not evidence about this executable or helper authority.
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
    use super::observe_helper_upgrade_pending;
    use std::{fs, os::unix::fs::symlink};

    #[test]
    fn damaged_activation_marker_does_not_gate_observation_service() {
        let temporary = tempfile::tempdir().unwrap();
        let regular = temporary.path().join("regular");
        fs::write(&regular, b"damaged activation intent").unwrap();
        let directory = temporary.path().join("directory");
        fs::create_dir(&directory).unwrap();
        let link = temporary.path().join("link");
        symlink(&regular, &link).unwrap();
        for marker in [&regular, &directory, &link] {
            observe_helper_upgrade_pending(marker);
            // Observation does not activate or erase unverified package state.
            assert!(fs::symlink_metadata(marker).is_ok());
        }
        observe_helper_upgrade_pending(&temporary.path().join("absent"));
    }
}
