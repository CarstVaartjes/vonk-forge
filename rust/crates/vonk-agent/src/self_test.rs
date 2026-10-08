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
    #[error("agent package helper upgrade activation is pending")]
    HelperUpgradePending,
    #[error(transparent)]
    Client(#[from] ClientError),
    #[error(transparent)]
    Identity(#[from] RuntimeIdentityError),
    #[error("agent filesystem observation is unavailable")]
    Io(#[from] std::io::Error),
}

pub fn run(
    config: &AgentConfig,
    executable: &Path,
    runtime_directory: &Path,
) -> Result<AgentRuntimeIdentity, SelfTestError> {
    verify_private_directory(&config.data_dir, "data")?;
    verify_private_directory(runtime_directory, "runtime")?;
    verify_no_helper_upgrade_pending(Path::new(HELPER_UPGRADE_PENDING))?;
    AgentHttpClient::from_config(config)?;
    Ok(PreparedRuntimeIdentity::from_executable(executable)?.mark_self_test_passed()?)
}

fn verify_no_helper_upgrade_pending(path: &Path) -> Result<(), SelfTestError> {
    observe_helper_upgrade_marker(
        || fs::symlink_metadata(path),
        || {
            std::thread::sleep(std::time::Duration::from_millis(100));
        },
    )
}

fn verify_helper_upgrade_marker_state(
    metadata: std::io::Result<fs::Metadata>,
) -> Result<(), SelfTestError> {
    match metadata {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Ok(_) => Err(SelfTestError::HelperUpgradePending),
        Err(error) => Err(SelfTestError::Io(error)),
    }
}

fn observe_helper_upgrade_marker(
    mut read: impl FnMut() -> std::io::Result<fs::Metadata>,
    mut backoff: impl FnMut(),
) -> Result<(), SelfTestError> {
    for _ in 0..2 {
        match verify_helper_upgrade_marker_state(read()) {
            Err(SelfTestError::Io(_) | SelfTestError::HelperUpgradePending) => backoff(),
            result => return result,
        }
    }
    verify_helper_upgrade_marker_state(read())
}

fn verify_private_directory(path: &Path, name: &'static str) -> Result<(), SelfTestError> {
    observe_private_directory(
        || inspect_private_directory(path, name),
        || std::thread::sleep(std::time::Duration::from_millis(100)),
    )
}

fn observe_private_directory(
    mut read: impl FnMut() -> Result<(), SelfTestError>,
    mut backoff: impl FnMut(),
) -> Result<(), SelfTestError> {
    for _ in 0..2 {
        match read() {
            Err(SelfTestError::Io(_) | SelfTestError::UnsafePath(_)) => backoff(),
            result => return result,
        }
    }
    read()
}

fn inspect_private_directory(path: &Path, name: &'static str) -> Result<(), SelfTestError> {
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
    use super::{SelfTestError, verify_no_helper_upgrade_pending};
    use std::{fs, io, os::unix::fs::symlink};

    #[test]
    fn private_directory_observation_repairs_without_changing_host_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("runtime");
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        let mut waits = 0;
        let result = super::observe_private_directory(
            || super::inspect_private_directory(&path, "runtime"),
            || {
                waits += 1;
                // The existing runtime owner publishes the corrected directory.
                fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            },
        );
        assert!(result.is_ok() && waits == 1);
        assert!(super::verify_private_directory(&path, "runtime").is_ok());
    }

    #[test]
    fn unavailable_private_directory_has_a_finite_budget_and_no_fresh_gate() {
        for unreadable in [true, false] {
            let mut reads = 0;
            let mut waits = 0;
            let result = super::observe_private_directory(
                || {
                    reads += 1;
                    if unreadable {
                        Err(SelfTestError::Io(io::Error::from(
                            io::ErrorKind::Interrupted,
                        )))
                    } else {
                        Err(SelfTestError::UnsafePath("runtime"))
                    }
                },
                || waits += 1,
            );
            assert!(result.is_err());
            assert_eq!((reads, waits), (3, 2));
            assert!(super::observe_private_directory(|| Ok(()), || panic!("no wait")).is_ok());
        }
    }

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
            assert!(verify_no_helper_upgrade_pending(marker).is_err());
            assert!(fs::symlink_metadata(marker).is_ok());
        }
        // The activation owner clearing its marker admits the next self-test.
        fs::remove_file(&link).unwrap();
        fs::remove_file(&regular).unwrap();
        fs::remove_dir(&directory).unwrap();
        for marker in [&regular, &directory, &link] {
            assert!(verify_no_helper_upgrade_pending(marker).is_ok());
        }
    }

    #[test]
    fn helper_activation_finishes_within_the_same_self_test_budget() {
        let temporary = tempfile::tempdir().unwrap();
        let marker = temporary.path().join("helper-upgrade.pending");
        fs::write(&marker, b"activation intent").unwrap();
        let mut waits = 0;
        let outcome = super::observe_helper_upgrade_marker(
            || fs::symlink_metadata(&marker),
            || {
                waits += 1;
                // Model the existing package activation owner, not an operator.
                fs::remove_file(&marker).unwrap();
            },
        );
        assert!(outcome.is_ok() && waits == 1);
        assert!(verify_no_helper_upgrade_pending(&marker).is_ok());
    }

    #[test]
    fn unreadable_marker_recovers_or_ends_then_a_fresh_self_test_is_admitted() {
        for repair in [true, false] {
            let mut calls = 0;
            let mut waits = 0;
            let outcome = super::observe_helper_upgrade_marker(
                || {
                    calls += 1;
                    Err(io::Error::new(
                        if repair && calls > 1 {
                            io::ErrorKind::NotFound
                        } else {
                            io::ErrorKind::Interrupted
                        },
                        "private host detail",
                    ))
                },
                || waits += 1,
            );
            assert_eq!(outcome.is_ok(), repair);
            assert!(calls <= 3 && waits <= 2);
            if let Err(error) = outcome {
                assert!(!error.to_string().contains("private host detail"));
            }
            assert!(
                super::observe_helper_upgrade_marker(
                    || Err(io::Error::from(io::ErrorKind::NotFound)),
                    || panic!("readable absence needs no backoff"),
                )
                .is_ok()
            );
        }
    }
}
