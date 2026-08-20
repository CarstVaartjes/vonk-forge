use std::{fs, os::unix::fs::MetadataExt, path::Path};

use thiserror::Error;

use crate::{
    client::{AgentHttpClient, ClientError},
    config::AgentConfig,
    runtime_identity::{AgentRuntimeIdentity, RuntimeIdentityError},
};

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
    AgentHttpClient::from_config(config)?;
    Ok(AgentRuntimeIdentity::from_executable(executable)?.mark_self_test_passed())
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
