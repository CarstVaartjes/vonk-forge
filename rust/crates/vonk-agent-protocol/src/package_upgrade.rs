//! Current, exact source-bound package rollback authority.
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PackageRollbackSource {
    pub package_sha256: String,
    pub package_signature: String,
    pub package_version: String,
    pub binary_sha256: String,
    pub helper_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PackageRollbackAuthority {
    pub source: PackageRollbackSource,
    pub attempt_nonce: String,
    pub activation_deadline: i64,
}

impl PackageRollbackAuthority {
    pub fn valid(&self) -> bool {
        let hex = |value: &str, length| {
            value.len() == length
                && value
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        };
        hex(&self.source.package_sha256, 64)
            && hex(&self.source.package_signature, 128)
            && hex(&self.source.binary_sha256, 64)
            && hex(&self.source.helper_sha256, 64)
            && hex(&self.attempt_nonce, 64)
            && self.activation_deadline > 0
            && self
                .source
                .package_version
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphanumeric)
            && self.source.package_version.len() <= 128
            && self
                .source
                .package_version
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b".+~:-".contains(&c))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PackageActivationPhase {
    Armed,
    ActivationFailed,
    Acknowledged,
    RollingBack,
    RolledBack,
    RollbackFailed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PackageActivationReceipt {
    pub schema_version: u8,
    pub node_id: String,
    pub source_package_sha256: String,
    pub source_version: String,
    pub source_binary_sha256: String,
    pub candidate_package_sha256: String,
    pub candidate_version: String,
    pub candidate_binary_sha256: String,
    pub attempt_nonce: String,
    pub phase: PackageActivationPhase,
    pub created_at: i64,
    pub updated_at: i64,
    pub outcome: String,
}
