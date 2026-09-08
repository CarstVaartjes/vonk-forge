use std::fs::File;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;

use rustix::fs::{Mode, OFlags};
use sha2::{Digest, Sha256};
use thiserror::Error;

pub use vonk_agent_protocol::generated::AgentRuntimeIdentity;
use vonk_agent_protocol::generated::AgentRuntimeIdentityArchitecture;
use vonk_agent_protocol::canonical_generated_json;

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
    #[error("agent runtime identity is invalid")]
    InvalidIdentity,
}

#[used]
static BUILD_DIGEST_MARKER: &str =
    concat!("VONK_AGENT_BUILD_DIGEST=", env!("VONK_AGENT_BUILD_DIGEST"));
#[used]
static SEMANTIC_VERSION_MARKER: &str = concat!(
    "VONK_AGENT_SEMANTIC_VERSION=",
    env!("VONK_AGENT_SEMANTIC_VERSION")
);

/// Identity material collected before the local self-test succeeds.
///
/// This deliberately is not serializable: only the generated canonical
/// `AgentRuntimeIdentity` may cross the Controller transport boundary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreparedRuntimeIdentity {
    pub semantic_version: String,
    pub build_digest: String,
    pub binary_digest: String,
    pub architecture: AgentRuntimeIdentityArchitecture,
    observation_receipt_public_key: Option<[u8; 32]>,
}

impl PreparedRuntimeIdentity {
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
                AgentRuntimeIdentityArchitecture::LinuxArm64
            } else {
                AgentRuntimeIdentityArchitecture::LinuxAmd64
            },
            observation_receipt_public_key: None,
        })
    }

    pub fn with_observation_receipt_public_key(
        mut self,
        path: &Path,
    ) -> Result<Self, RuntimeIdentityError> {
        self.observation_receipt_public_key = Some(load_observation_public_key(path)?);
        Ok(self)
    }

    pub(crate) fn with_observation_receipt_public_key_bytes(mut self, key: [u8; 32]) -> Self {
        self.observation_receipt_public_key = Some(key);
        self
    }

    pub fn mark_self_test_passed(self) -> Result<AgentRuntimeIdentity, RuntimeIdentityError> {
        let observation_receipt_public_key = self
            .observation_receipt_public_key
            .ok_or(RuntimeIdentityError::UnsafeObservationReceiptKey)?;
        let identity = AgentRuntimeIdentity {
            semantic_version: self.semantic_version,
            build_digest: self.build_digest,
            binary_digest: self.binary_digest,
            architecture: self.architecture,
            self_test_passed: true,
            package_activation: None,
            observation_receipt_public_key: hex::encode(observation_receipt_public_key),
        };
        let document = canonical_generated_json(&identity)
            .map_err(|_| RuntimeIdentityError::InvalidIdentity)?;
        serde_json::from_slice(&document).map_err(|_| RuntimeIdentityError::InvalidIdentity)
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
