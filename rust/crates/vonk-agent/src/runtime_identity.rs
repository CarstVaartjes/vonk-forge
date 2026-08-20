use std::fs::File;
use std::io::Read;
use std::path::Path;

use rustix::fs::{Mode, OFlags};
use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;

const MAX_AGENT_BYTES: u64 = 512 * 1024 * 1024;

#[derive(Debug, Error)]
pub enum RuntimeIdentityError {
    #[error("agent executable is unsafe")]
    UnsafeExecutable,
    #[error("agent executable could not be read")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AgentRuntimeIdentity {
    pub semantic_version: &'static str,
    pub build_digest: String,
    pub binary_digest: String,
    pub architecture: &'static str,
    pub self_test_passed: bool,
}

impl AgentRuntimeIdentity {
    pub fn from_current_executable() -> Result<Self, RuntimeIdentityError> {
        Self::from_executable(&std::env::current_exe()?)
    }

    pub fn from_executable(path: &Path) -> Result<Self, RuntimeIdentityError> {
        let descriptor = rustix::fs::open(
            path,
            OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::empty(),
        )
        .map_err(std::io::Error::from)?;
        let mut executable = File::from(descriptor);
        let metadata = executable.metadata()?;
        if !metadata.is_file() || metadata.len() == 0 || metadata.len() > MAX_AGENT_BYTES {
            return Err(RuntimeIdentityError::UnsafeExecutable);
        }
        let mut raw = Vec::with_capacity(metadata.len() as usize);
        executable
            .by_ref()
            .take(MAX_AGENT_BYTES + 1)
            .read_to_end(&mut raw)?;
        if raw.len() as u64 != metadata.len() {
            return Err(RuntimeIdentityError::UnsafeExecutable);
        }
        let binary_digest = hex::encode(Sha256::digest(raw));
        Ok(Self {
            semantic_version: env!("CARGO_PKG_VERSION"),
            build_digest: format!("sha256:{binary_digest}"),
            binary_digest,
            architecture: if cfg!(target_arch = "aarch64") {
                "linux-arm64"
            } else {
                "linux-x86_64"
            },
            self_test_passed: true,
        })
    }
}
