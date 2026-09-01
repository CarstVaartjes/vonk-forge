use std::{fs, os::unix::fs::MetadataExt, path::Path};

use thiserror::Error;

use crate::{
    client::{AgentHttpClient, ClientError},
    config::AgentConfig,
    runtime_identity::{AgentRuntimeIdentity, RuntimeIdentityError},
};

const HELPER_UPGRADE_PENDING: &str = "/var/lib/vonk-forge/helper-upgrade.pending";

#[derive(Debug, Error)]
pub enum SelfTestError {
    #[error("agent runtime path is unsafe: {0}")]
    UnsafePath(&'static str),
    #[error("agent package helper upgrade activation is pending")]
    HelperUpgradePending,
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
    run_with_observation_receipt_key(config, executable, runtime_directory, None)
}

/// Test seam for exercising the complete direct self-test contract without
/// weakening production's root-owned key-custody check.
#[doc(hidden)]
pub fn run_with_observation_receipt_key(
    config: &AgentConfig,
    executable: &Path,
    runtime_directory: &Path,
    observation_receipt_public_key: Option<[u8; 32]>,
) -> Result<AgentRuntimeIdentity, SelfTestError> {
    verify_private_directory(&config.data_dir, "data")?;
    verify_private_directory(runtime_directory, "runtime")?;
    verify_no_helper_upgrade_pending(Path::new(HELPER_UPGRADE_PENDING))?;
    AgentHttpClient::from_config(config)?;
    let identity = AgentRuntimeIdentity::from_executable(executable)?;
    let identity = match observation_receipt_public_key {
        Some(key) => identity.with_observation_receipt_public_key_bytes(key),
        None => identity.with_observation_receipt_public_key(Path::new(
            crate::runtime_identity::OBSERVATION_RECEIPT_PUBLIC_KEY_PATH,
        ))?,
    };
    Ok(identity.mark_self_test_passed())
}

fn verify_no_helper_upgrade_pending(path: &Path) -> Result<(), SelfTestError> {
    verify_helper_upgrade_marker_state(fs::symlink_metadata(path))
}

fn verify_helper_upgrade_marker_state(
    metadata: std::io::Result<fs::Metadata>,
) -> Result<(), SelfTestError> {
    match metadata {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Ok(_) | Err(_) => Err(SelfTestError::HelperUpgradePending),
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
    use super::{
        SelfTestError, verify_helper_upgrade_marker_state, verify_no_helper_upgrade_pending,
    };
    use std::{fs, io, os::unix::fs::symlink};

    #[test]
    fn absent_helper_upgrade_marker_is_normal() {
        let temporary = tempfile::tempdir().unwrap();
        let marker = temporary.path().join("helper-upgrade.pending");
        assert!(verify_no_helper_upgrade_pending(&marker).is_ok());
    }

    #[test]
    fn every_existing_marker_type_blocks_self_test() {
        let temporary = tempfile::tempdir().unwrap();
        let regular = temporary.path().join("regular");
        fs::write(&regular, b"pending\n").unwrap();
        let directory = temporary.path().join("directory");
        fs::create_dir(&directory).unwrap();
        let link = temporary.path().join("link");
        symlink(&regular, &link).unwrap();

        for marker in [&regular, &directory, &link] {
            assert!(matches!(
                verify_no_helper_upgrade_pending(marker),
                Err(SelfTestError::HelperUpgradePending)
            ));
        }
    }

    #[test]
    fn unreadable_marker_state_fails_closed_without_exposing_io_detail() {
        let result = verify_helper_upgrade_marker_state(Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "private host detail",
        )));
        let error = result.unwrap_err();
        assert!(matches!(&error, SelfTestError::HelperUpgradePending));
        assert_eq!(
            error.to_string(),
            "agent package helper upgrade activation is pending"
        );
        assert!(!error.to_string().contains("private host detail"));
    }
}
