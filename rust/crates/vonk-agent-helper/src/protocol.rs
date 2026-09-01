use std::io::{Read, Write};

use ring::signature::{self, Ed25519KeyPair, KeyPair};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use uuid::{Uuid, Version};
use vonk_agent_protocol::{
    RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY, RecipeRunObservationOutcome,
    RecipeRunObservationReceipt, RecipeRunObservationReceiptClaims,
    RecipeRunObservationReceiptSignature, canonical_json, hex_sha256,
    recipe_run_observation_receipt_signing_bytes,
};

pub const MAX_MESSAGE_BYTES: usize = 256 * 1024;
pub const MAX_GRANT_LIFETIME_SECONDS: i64 = 300;
pub const AUTHORITY: &str = "vonk.host-maintenance-helper";
const GRANT_DOMAIN: &[u8] = b"VONK-HOST-MAINTENANCE-HELPER-GRANT-V1\0";
const ARTIFACT_DOMAIN: &[u8] = b"VONK-HOST-ARTIFACT-V1\0";

#[derive(Debug, Error)]
pub enum HelperError {
    #[error("helper message is invalid")]
    InvalidMessage,
    #[error("helper operation is invalid")]
    InvalidOperation,
    #[error("helper authorization is invalid")]
    InvalidAuthorization,
    #[error("helper peer is not authorized")]
    InvalidPeer,
    #[error("helper framing is invalid")]
    InvalidFrame,
    #[error("helper I/O failed")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum ManagedArea {
    Models,
    State,
    Workloads,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum RestartUnit {
    Agent,
    Helper,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum ContainerRuntimeAction {
    ImageImport,
    ImageInspect,
    RunInspect,
    Start,
    Stop,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "type", rename_all = "kebab-case", deny_unknown_fields)]
pub enum HostOperation {
    CreateManagedDirectory {
        area: ManagedArea,
        relative_path: String,
    },
    InstallVonkDeb {
        package_sha256: String,
        package_signature: String,
    },
    RestartVonkUnit {
        unit: RestartUnit,
    },
    ScheduleReboot {
        delay_seconds: u16,
    },
    ExecuteContainerRuntimeRequest {
        action: ContainerRuntimeAction,
        job_id: Uuid,
        operation_id: Uuid,
        attempt: u32,
        fence: Uuid,
        request_sha256: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        observation_identity_sha256: Option<String>,
    },
}

impl HostOperation {
    pub fn validate(&self) -> Result<(), HelperError> {
        let valid = match self {
            Self::CreateManagedDirectory { relative_path, .. } => {
                valid_relative_path(relative_path)
            }
            Self::InstallVonkDeb {
                package_sha256,
                package_signature,
            } => valid_digest(package_sha256) && valid_signature(package_signature),
            Self::RestartVonkUnit { .. } => true,
            Self::ScheduleReboot { delay_seconds } => (60..=3600).contains(delay_seconds),
            Self::ExecuteContainerRuntimeRequest {
                job_id,
                operation_id,
                attempt,
                fence,
                request_sha256,
                action,
                observation_identity_sha256,
                ..
            } => {
                job_id.get_version() == Some(Version::Random)
                    && operation_id.get_version() == Some(Version::Random)
                    && *attempt > 0
                    && fence.get_version() == Some(Version::Random)
                    && valid_digest(request_sha256)
                    && match observation_identity_sha256 {
                        None => true,
                        Some(digest) => {
                            *action == ContainerRuntimeAction::RunInspect && valid_digest(digest)
                        }
                    }
            }
        };
        if valid {
            Ok(())
        } else {
            Err(HelperError::InvalidOperation)
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GrantClaims {
    pub schema_version: u8,
    pub authority: String,
    pub request_id: Uuid,
    pub node_id: String,
    pub issued_at: i64,
    pub expires_at: i64,
    pub operation: HostOperation,
}

impl GrantClaims {
    fn validate(&self) -> Result<(), HelperError> {
        if self.schema_version != 1
            || self.authority != AUTHORITY
            || self.request_id.get_version() != Some(Version::Random)
            || !valid_node_id(&self.node_id)
            || self.issued_at <= 0
            || !(1..=MAX_GRANT_LIFETIME_SECONDS).contains(&(self.expires_at - self.issued_at))
        {
            return Err(HelperError::InvalidAuthorization);
        }
        self.operation.validate()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GrantSignature {
    pub algorithm: String,
    pub key_id: String,
    pub value: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SignedGrant {
    pub schema_version: u8,
    pub claims: GrantClaims,
    pub signature: GrantSignature,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PeerIdentity {
    pub uid: u32,
    pub primary_gid: u32,
    pub supplementary_gids: Vec<u32>,
}

pub struct GrantVerifier {
    public_key: [u8; 32],
    key_id: String,
    allowed_gid: u32,
}

impl GrantVerifier {
    pub fn new(public_key: &[u8], allowed_gid: u32) -> Result<Self, HelperError> {
        let public_key: [u8; 32] = public_key
            .try_into()
            .map_err(|_| HelperError::InvalidAuthorization)?;
        Ok(Self {
            key_id: hex_sha256(&public_key),
            public_key,
            allowed_gid,
        })
    }

    pub fn authorize(
        &self,
        grant: &SignedGrant,
        peer: &PeerIdentity,
        now: i64,
    ) -> Result<(), HelperError> {
        if peer.primary_gid != self.allowed_gid
            && !peer.supplementary_gids.contains(&self.allowed_gid)
        {
            return Err(HelperError::InvalidPeer);
        }
        grant.claims.validate()?;
        if grant.schema_version != 1
            || now < grant.claims.issued_at
            || now >= grant.claims.expires_at
            || grant.signature.algorithm != "ed25519"
            || grant.signature.key_id != self.key_id
            || !valid_signature(&grant.signature.value)
        {
            return Err(HelperError::InvalidAuthorization);
        }
        let signature_bytes =
            hex::decode(&grant.signature.value).map_err(|_| HelperError::InvalidAuthorization)?;
        signature::UnparsedPublicKey::new(&signature::ED25519, self.public_key)
            .verify(&canonical_signing_bytes(&grant.claims)?, &signature_bytes)
            .map_err(|_| HelperError::InvalidAuthorization)
    }
}

pub fn parse_request(raw: &[u8]) -> Result<SignedGrant, HelperError> {
    if raw.is_empty() || raw.len() > MAX_MESSAGE_BYTES {
        return Err(HelperError::InvalidMessage);
    }
    let request: SignedGrant =
        serde_json::from_slice(raw).map_err(|_| HelperError::InvalidMessage)?;
    let canonical = canonical_json(&request).map_err(|_| HelperError::InvalidMessage)?;
    if canonical != raw {
        return Err(HelperError::InvalidMessage);
    }
    request.claims.validate()?;
    Ok(request)
}

pub fn canonical_signing_bytes(claims: &GrantClaims) -> Result<Vec<u8>, HelperError> {
    claims.validate()?;
    let mut value = GRANT_DOMAIN.to_vec();
    value.extend(canonical_json(claims).map_err(|_| HelperError::InvalidMessage)?);
    Ok(value)
}

pub fn sign_observation_receipt(
    signer: &Ed25519KeyPair,
    node_id: &str,
    request_id: Uuid,
    request_sha256: &str,
    observation_identity_sha256: &str,
    outcome: RecipeRunObservationOutcome,
    observed_at: i64,
) -> Result<RecipeRunObservationReceipt, HelperError> {
    let claims = RecipeRunObservationReceiptClaims {
        schema_version: 1,
        authority: RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY.to_owned(),
        node_id: node_id.to_owned(),
        request_id,
        request_sha256: request_sha256.to_owned(),
        observation_identity_sha256: observation_identity_sha256.to_owned(),
        outcome,
        observed_at,
    };
    let signature = signer.sign(
        &recipe_run_observation_receipt_signing_bytes(&claims)
            .map_err(|_| HelperError::InvalidOperation)?,
    );
    Ok(RecipeRunObservationReceipt {
        schema_version: 1,
        claims,
        signature: RecipeRunObservationReceiptSignature {
            algorithm: "ed25519".to_owned(),
            key_id: hex_sha256(signer.public_key().as_ref()),
            value: hex::encode(signature.as_ref()),
        },
    })
}

pub fn artifact_signing_bytes(kind: &str, digest: &str) -> Result<Vec<u8>, HelperError> {
    if !matches!(kind, "agent" | "deb") || !valid_digest(digest) {
        return Err(HelperError::InvalidOperation);
    }
    let mut value = ARTIFACT_DOMAIN.to_vec();
    value.extend_from_slice(kind.as_bytes());
    value.push(0);
    value.extend(hex::decode(digest).map_err(|_| HelperError::InvalidOperation)?);
    Ok(value)
}

pub fn read_frame(reader: &mut impl Read) -> Result<Vec<u8>, HelperError> {
    let mut header = [0_u8; 4];
    reader.read_exact(&mut header)?;
    let length = u32::from_be_bytes(header) as usize;
    if !(1..=MAX_MESSAGE_BYTES).contains(&length) {
        return Err(HelperError::InvalidFrame);
    }
    let mut body = vec![0_u8; length];
    reader.read_exact(&mut body)?;
    Ok(body)
}

pub fn write_frame(writer: &mut impl Write, body: &[u8]) -> Result<(), HelperError> {
    if body.is_empty() || body.len() > MAX_MESSAGE_BYTES {
        return Err(HelperError::InvalidFrame);
    }
    writer.write_all(&(body.len() as u32).to_be_bytes())?;
    writer.write_all(body)?;
    writer.flush()?;
    Ok(())
}

fn valid_node_id(value: &str) -> bool {
    value.len() == 36
        && value.starts_with("spk_")
        && value[4..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_signature(value: &str) -> bool {
    value.len() == 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_relative_path(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 512
        && value.split('/').all(|component| {
            !component.is_empty()
                && component != "."
                && component != ".."
                && component.len() <= 128
                && component
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        })
}

#[cfg(test)]
mod receipt_tests {
    use super::sign_observation_receipt;
    use ring::signature::{Ed25519KeyPair, KeyPair, UnparsedPublicKey};
    use uuid::Uuid;
    use vonk_agent_protocol::{
        RecipeRunObservationOutcome, recipe_run_observation_receipt_signing_bytes,
    };

    #[test]
    fn signed_receipt_fixture_verifies_and_every_claim_is_covered() {
        let signer = Ed25519KeyPair::from_seed_unchecked(&[19; 32]).unwrap();
        let request_id = Uuid::parse_str("10000000-0000-4000-8000-000000000001").unwrap();
        let receipt = sign_observation_receipt(
            &signer,
            "spk_0123456789abcdef0123456789abcdef",
            request_id,
            &"a".repeat(64),
            &"b".repeat(64),
            RecipeRunObservationOutcome::NotRunning,
            1_788_000_000,
        )
        .unwrap();
        receipt.validate().unwrap();
        let signature = hex::decode(&receipt.signature.value).unwrap();
        UnparsedPublicKey::new(&ring::signature::ED25519, signer.public_key().as_ref())
            .verify(
                &recipe_run_observation_receipt_signing_bytes(&receipt.claims).unwrap(),
                &signature,
            )
            .unwrap();

        let mut changed = receipt;
        changed.claims.request_id = Uuid::new_v4();
        assert!(
            UnparsedPublicKey::new(&ring::signature::ED25519, signer.public_key().as_ref())
                .verify(
                    &recipe_run_observation_receipt_signing_bytes(&changed.claims).unwrap(),
                    &signature,
                )
                .is_err()
        );
    }
}
