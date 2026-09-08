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

impl PackageActivationReceipt {
    pub fn validate(&self) -> Result<(), String> {
        let hex = |value: &str| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        };
        let version = |value: &str| {
            value
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphanumeric)
                && value.len() <= 128
                && value
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b".+~:-".contains(&c))
        };
        if self.schema_version != 2
            || self.node_id.len() != 36
            || !self.node_id.starts_with("spk_")
            || !self.node_id[4..]
                .bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
            || !hex(&self.source_package_sha256)
            || !hex(&self.source_binary_sha256)
            || !hex(&self.candidate_package_sha256)
            || !hex(&self.candidate_binary_sha256)
            || !hex(&self.attempt_nonce)
            || !version(&self.source_version)
            || !version(&self.candidate_version)
            || self.created_at < 1
            || self.updated_at < self.created_at
            || self.outcome.is_empty()
            || self.outcome.len() > 128
            || !self
                .outcome
                .bytes()
                .all(|c| c.is_ascii_lowercase() || c == b'_')
        {
            return Err("invalid package activation receipt".into());
        }
        Ok(())
    }
}
