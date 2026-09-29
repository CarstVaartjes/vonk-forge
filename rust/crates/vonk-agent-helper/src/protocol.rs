use std::io::{Read, Write};

use ring::signature;
use thiserror::Error;
use vonk_agent_protocol::{
    HOST_HELPER_AUTHORITY, RecipeRunInspectionRequest, canonical_json, hex_sha256,
    host_helper_grant_signing_bytes,
};
pub use vonk_agent_protocol::{
    HostHelperContainerRuntimeAction as ContainerRuntimeAction,
    HostHelperGrantClaims as GrantClaims, HostHelperGrantSignature as GrantSignature,
    HostHelperOperation as HostOperation, SignedHostHelperGrant as SignedGrant,
};

/// The same frame ceiling the agent frames against; declared once in the wire
/// contract so the two sides cannot drift.
pub const MAX_MESSAGE_BYTES: usize = vonk_agent_protocol::MAX_HELPER_FRAME_BYTES;
pub const MAX_GRANT_LIFETIME_SECONDS: i64 = 300;
pub const AUTHORITY: &str = HOST_HELPER_AUTHORITY;
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

impl HelperError {
    /// Stable, bounded diagnostics for the response and service log.
    /// Underlying I/O text can contain paths or other host details.
    pub fn code(&self) -> &'static str {
        match self {
            Self::InvalidMessage => "helper.message_invalid",
            Self::InvalidOperation => "helper.operation_invalid",
            Self::InvalidAuthorization => "helper.authorization_invalid",
            Self::InvalidPeer => "helper.peer_invalid",
            Self::InvalidFrame => "helper.frame_invalid",
            Self::Io(_) => "helper.io_failed",
        }
    }

    pub fn safe_detail(&self) -> &'static str {
        match self {
            Self::InvalidMessage => "helper message is invalid",
            Self::InvalidOperation => "helper operation is invalid",
            Self::InvalidAuthorization => "helper authorization is invalid",
            Self::InvalidPeer => "helper peer is not authorized",
            Self::InvalidFrame => "helper framing is invalid",
            Self::Io(_) => "helper I/O failed",
        }
    }
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

    /// Only the agent group may talk to the helper at all.
    pub fn authorize_peer(&self, peer: &PeerIdentity) -> Result<(), HelperError> {
        if peer.primary_gid != self.allowed_gid
            && !peer.supplementary_gids.contains(&self.allowed_gid)
        {
            return Err(HelperError::InvalidPeer);
        }
        Ok(())
    }

    pub fn authorize(
        &self,
        grant: &SignedGrant,
        peer: &PeerIdentity,
        now: i64,
    ) -> Result<(), HelperError> {
        self.authorize_peer(peer)?;
        grant
            .claims
            .validate()
            .map_err(|_| HelperError::InvalidAuthorization)?;
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
    request
        .claims
        .validate()
        .map_err(|_| HelperError::InvalidAuthorization)?;
    Ok(request)
}

/// Parse the one ungranted frame: a read-only inspection of a managed run.
pub fn parse_inspection_request(raw: &[u8]) -> Result<RecipeRunInspectionRequest, HelperError> {
    if raw.is_empty() || raw.len() > MAX_MESSAGE_BYTES {
        return Err(HelperError::InvalidMessage);
    }
    let request: RecipeRunInspectionRequest =
        serde_json::from_slice(raw).map_err(|_| HelperError::InvalidMessage)?;
    if canonical_json(&request).map_err(|_| HelperError::InvalidMessage)? != raw
        || !valid_digest(&request.request_sha256)
    {
        return Err(HelperError::InvalidMessage);
    }
    Ok(request)
}

pub fn canonical_signing_bytes(claims: &GrantClaims) -> Result<Vec<u8>, HelperError> {
    host_helper_grant_signing_bytes(claims).map_err(|_| HelperError::InvalidAuthorization)
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
