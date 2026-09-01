use std::fs::File;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;

use rustix::fs::{Mode, OFlags};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;

const MAX_AGENT_BYTES: u64 = 512 * 1024 * 1024;
pub const OBSERVATION_RECEIPT_PUBLIC_KEY_PATH: &str =
    "/etc/vonk-forge-agent/observation-receipt.pub";

#[derive(Debug, Error)]
pub enum RuntimeIdentityError {
    #[error("agent executable is unsafe")]
    UnsafeExecutable,
    #[error("agent executable could not be read")]
    Io(#[from] std::io::Error),
    #[error("observation receipt public key is unsafe")]
    UnsafeObservationReceiptKey,
}

#[used]
static BUILD_DIGEST_MARKER: &str =
    concat!("VONK_AGENT_BUILD_DIGEST=", env!("VONK_AGENT_BUILD_DIGEST"));
#[used]
static SEMANTIC_VERSION_MARKER: &str = concat!(
    "VONK_AGENT_SEMANTIC_VERSION=",
    env!("VONK_AGENT_SEMANTIC_VERSION")
);

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AgentRuntimeIdentity {
    pub semantic_version: String,
    pub build_digest: String,
    pub binary_digest: String,
    pub architecture: String,
    pub self_test_passed: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub observation_receipt_public_key: Option<String>,
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
            semantic_version: env!("VONK_AGENT_SEMANTIC_VERSION").to_owned(),
            build_digest: env!("VONK_AGENT_BUILD_DIGEST").to_owned(),
            binary_digest,
            architecture: if cfg!(target_arch = "aarch64") {
                "linux-arm64".to_owned()
            } else {
                "linux-amd64".to_owned()
            },
            self_test_passed: false,
            observation_receipt_public_key: None,
        })
    }

    pub fn with_observation_receipt_public_key(
        mut self,
        path: &Path,
    ) -> Result<Self, RuntimeIdentityError> {
        self.observation_receipt_public_key = Some(hex::encode(load_observation_public_key(path)?));
        Ok(self)
    }

    pub(crate) fn with_observation_receipt_public_key_bytes(mut self, key: [u8; 32]) -> Self {
        self.observation_receipt_public_key = Some(hex::encode(key));
        self
    }

    pub fn observation_receipt_public_key(&self) -> Result<[u8; 32], RuntimeIdentityError> {
        let encoded = self
            .observation_receipt_public_key
            .as_deref()
            .ok_or(RuntimeIdentityError::UnsafeObservationReceiptKey)?;
        let raw =
            hex::decode(encoded).map_err(|_| RuntimeIdentityError::UnsafeObservationReceiptKey)?;
        raw.try_into()
            .map_err(|_| RuntimeIdentityError::UnsafeObservationReceiptKey)
    }

    pub fn mark_self_test_passed(mut self) -> Self {
        self.self_test_passed = true;
        self
    }
}

pub(crate) fn load_observation_public_key(path: &Path) -> Result<[u8; 32], RuntimeIdentityError> {
    let descriptor = rustix::fs::open(
        path,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::empty(),
    )
    .map_err(std::io::Error::from)?;
    let mut file = File::from(descriptor);
    let metadata = file.metadata()?;
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.uid() != 0
        || metadata.permissions().mode() & 0o777 != 0o640
        || metadata.len() != 32
    {
        return Err(RuntimeIdentityError::UnsafeObservationReceiptKey);
    }
    let mut raw = Vec::with_capacity(32);
    file.by_ref().take(33).read_to_end(&mut raw)?;
    if raw.len() != 32 {
        return Err(RuntimeIdentityError::UnsafeObservationReceiptKey);
    }
    raw.try_into()
        .map_err(|_| RuntimeIdentityError::UnsafeObservationReceiptKey)
}
