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

#[derive(Debug, Error)]
pub enum SelfTestError {
    #[error("agent runtime path is unsafe: {0}")]
    UnsafePath(&'static str),
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
    if let Err(error) = verify_runtime_directories(&config.data_dir, runtime_directory) {
        eprintln!("vonk-agent: directory custody unavailable: {error}");
    }
    Ok(PreparedRuntimeIdentity::from_executable(executable)?.mark_self_test_passed()?)
}

pub fn verify_runtime_directories(data: &Path, runtime: &Path) -> Result<(), SelfTestError> {
    let data = verify_private_directory(data, "data");
    let runtime = verify_private_directory(runtime, "runtime");
    data.and(runtime)
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
mod tests;
