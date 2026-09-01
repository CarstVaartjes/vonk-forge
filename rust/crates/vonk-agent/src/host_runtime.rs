//! Narrow client for controller-authorized host container-runtime operations.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use ring::signature;
use serde::Deserialize;
use thiserror::Error;
use vonk_agent_protocol::{
    AgentClaim, HostRuntimeAction, HostRuntimeRequest, RecipeRunInspectionBinding,
    RecipeRunObservationOutcome, RecipeRunObservationReceipt, canonical_json, hex_sha256,
    parse_strict, recipe_run_observation_receipt_signing_bytes,
};

use crate::client::{AgentHttpClient, ClientError};

const MAX_HELPER_MESSAGE_BYTES: usize = 256 * 1024;

#[derive(Debug, Error)]
pub enum HostRuntimeError {
    #[error("host runtime request storage is invalid")]
    Io(#[from] std::io::Error),
    #[error("host runtime authority is unavailable")]
    Controller(#[from] ClientError),
    #[error("host runtime helper response is invalid")]
    Protocol,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HelperResponse {
    schema_version: u8,
    request_id: Option<String>,
    status: String,
    evidence_sha256: Option<String>,
    #[serde(default)]
    exit_code: Option<i32>,
    #[serde(default)]
    error_code: Option<String>,
    #[serde(default)]
    observation_receipt: Option<RecipeRunObservationReceipt>,
}

pub struct HostRuntimeOutcome {
    pub exit_code: Option<i32>,
    pub stop_uncertain: bool,
}

pub struct RecipeRunInspectionOutcome {
    pub grant: serde_json::Value,
    pub observation_identity_sha256: String,
    pub receipt: RecipeRunObservationReceipt,
    pub process_running: bool,
}

pub struct HostRuntimeBoundary<'a> {
    pub client: &'a AgentHttpClient,
    pub request_root: &'a Path,
    pub helper_socket: &'a Path,
    pub observation_receipt_public_key: [u8; 32],
}

struct RequestFileCleanup(PathBuf);

impl Drop for RequestFileCleanup {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

impl HostRuntimeBoundary<'_> {
    pub async fn inspect_recipe_run(
        &self,
        binding: RecipeRunInspectionBinding,
        arguments: Vec<String>,
    ) -> Result<RecipeRunInspectionOutcome, HostRuntimeError> {
        binding.validate().map_err(|_| HostRuntimeError::Protocol)?;
        let attempt =
            u32::try_from(binding.run_generation).map_err(|_| HostRuntimeError::Protocol)?;
        let request = HostRuntimeRequest {
            schema_version: 1,
            action: HostRuntimeAction::RunInspect,
            job_id: binding.run_id,
            operation_id: uuid::Uuid::new_v4(),
            attempt,
            fence: uuid::Uuid::new_v4(),
            arguments,
            observation: Some(binding.clone()),
        };
        request.validate().map_err(|_| HostRuntimeError::Protocol)?;
        let body = canonical_json(&request).map_err(|_| HostRuntimeError::Protocol)?;
        let digest = hex_sha256(&body);
        let request_path = write_request(self.request_root, &digest, &body)?;
        let _request_cleanup = RequestFileCleanup(request_path);
        let authorization = self
            .client
            .recipe_run_inspection_grant(&binding, &request, &digest)
            .await?;
        let request_id = authorization
            .grant
            .get("claims")
            .and_then(|claims| claims.get("request_id"))
            .and_then(serde_json::Value::as_str)
            .and_then(|value| uuid::Uuid::parse_str(value).ok())
            .filter(|value| value.get_version() == Some(uuid::Version::Random))
            .ok_or(HostRuntimeError::Protocol)?
            .to_string();
        let grant_bytes =
            canonical_json(&authorization.grant).map_err(|_| HostRuntimeError::Protocol)?;
        let helper_socket = self.helper_socket.to_path_buf();
        let response = tokio::task::spawn_blocking(move || {
            call_helper(&helper_socket, &grant_bytes, Duration::from_secs(15))
        })
        .await
        .map_err(|_| HostRuntimeError::Protocol)??;
        if response.schema_version != 1 || response.request_id.as_deref() != Some(&request_id) {
            return Err(HostRuntimeError::Protocol);
        }
        let receipt = require_inspection_receipt(&response)?.clone();
        let issued_at = authorization
            .grant
            .get("claims")
            .and_then(|claims| claims.get("issued_at"))
            .and_then(serde_json::Value::as_i64)
            .ok_or(HostRuntimeError::Protocol)?;
        let expires_at = authorization
            .grant
            .get("claims")
            .and_then(|claims| claims.get("expires_at"))
            .and_then(serde_json::Value::as_i64)
            .ok_or(HostRuntimeError::Protocol)?;
        let received_at = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| HostRuntimeError::Protocol)?
            .as_secs() as i64;
        let process_running = verify_observation_receipt(
            &receipt,
            &self.observation_receipt_public_key,
            self.client.node_id(),
            &request_id,
            &digest,
            &authorization.observation_identity_sha256,
            issued_at,
            expires_at,
            received_at,
        )?;
        Ok(RecipeRunInspectionOutcome {
            grant: authorization.grant,
            observation_identity_sha256: authorization.observation_identity_sha256,
            receipt,
            process_running,
        })
    }

    pub async fn execute(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        let helper_timeout = match action {
            HostRuntimeAction::Start => arguments
                .iter()
                .find_map(|value| value.strip_prefix("VONK_JOB_TIMEOUT_SECONDS="))
                .and_then(|value| value.parse::<u64>().ok())
                .filter(|value| (1..=3600).contains(value))
                .map_or(Duration::from_secs(610), |value| {
                    // Leave room after the adapter deadline for bounded inspect/stop/remove and
                    // a truthful uncertainty response from the privileged helper.
                    Duration::from_secs(value + 120)
                }),
            HostRuntimeAction::Stop => arguments
                .get(1)
                .and_then(|value| value.parse::<u64>().ok())
                .filter(|value| (1..=600).contains(value))
                .map_or(Duration::from_secs(45), |value| {
                    Duration::from_secs(value + 45)
                }),
            _ => Duration::from_secs(610),
        };
        let request = HostRuntimeRequest {
            schema_version: 1,
            action,
            job_id: claim.job_id,
            operation_id: claim.operation_id,
            attempt: claim.attempt,
            fence: claim.fence,
            arguments,
            observation: None,
        };
        request.validate().map_err(|_| HostRuntimeError::Protocol)?;
        let body = canonical_json(&request).map_err(|_| HostRuntimeError::Protocol)?;
        let digest = hex_sha256(&body);
        let request_path = write_request(self.request_root, &digest, &body)?;
        // The attached helper call is deliberately run on a blocking worker so a cancellation
        // heartbeat can issue a concurrent STOP. If that cancellation drops this future, still
        // remove the signed request file; the helper already received its canonical body.
        let _request_cleanup = RequestFileCleanup(request_path);
        async {
            let grant = self
                .client
                .host_runtime_grant(claim, action, &digest)
                .await?;
            let request_id = grant
                .get("claims")
                .and_then(|claims| claims.get("request_id"))
                .and_then(serde_json::Value::as_str)
                .ok_or(HostRuntimeError::Protocol)?
                .to_owned();
            let grant = canonical_json(&grant).map_err(|_| HostRuntimeError::Protocol)?;
            let helper_socket = self.helper_socket.to_path_buf();
            let response = tokio::task::spawn_blocking(move || {
                call_helper(&helper_socket, &grant, helper_timeout)
            })
            .await
            .map_err(|_| HostRuntimeError::Protocol)??;
            let stop_uncertain = response.status == "container-runtime-stop-uncertain";
            if response.schema_version != 1
                || response.request_id.as_deref() != Some(request_id.as_str())
                || (!stop_uncertain && response.status != "container-runtime-request-executed")
                || response.error_code.is_some()
                || response
                    .evidence_sha256
                    .as_deref()
                    .is_none_or(|value| !lower_hex(value, 64))
                || response.observation_receipt.is_some()
            {
                return Err(HostRuntimeError::Protocol);
            }
            if response
                .exit_code
                .is_some_and(|code| !(0..=255).contains(&code))
            {
                return Err(HostRuntimeError::Protocol);
            }
            Ok(HostRuntimeOutcome {
                exit_code: response.exit_code,
                stop_uncertain,
            })
        }
        .await
    }
}

fn require_inspection_receipt(
    response: &HelperResponse,
) -> Result<&RecipeRunObservationReceipt, HostRuntimeError> {
    if response.status != "container-runtime-request-executed"
        || response.error_code.is_some()
        || response
            .evidence_sha256
            .as_deref()
            .is_none_or(|value| !lower_hex(value, 64))
        || response.exit_code.is_some()
    {
        return Err(HostRuntimeError::Protocol);
    }
    response
        .observation_receipt
        .as_ref()
        .ok_or(HostRuntimeError::Protocol)
}

#[allow(clippy::too_many_arguments)]
fn verify_observation_receipt(
    receipt: &RecipeRunObservationReceipt,
    public_key: &[u8; 32],
    node_id: &str,
    request_id: &str,
    request_sha256: &str,
    observation_identity_sha256: &str,
    issued_at: i64,
    expires_at: i64,
    received_at: i64,
) -> Result<bool, HostRuntimeError> {
    receipt.validate().map_err(|_| HostRuntimeError::Protocol)?;
    if receipt.claims.node_id != node_id
        || receipt.claims.request_id.to_string() != request_id
        || receipt.claims.request_sha256 != request_sha256
        || receipt.claims.observation_identity_sha256 != observation_identity_sha256
        || receipt.signature.key_id != hex_sha256(public_key)
        || receipt.claims.observed_at < issued_at
        || receipt.claims.observed_at >= expires_at
        || received_at > expires_at.saturating_add(5)
    {
        return Err(HostRuntimeError::Protocol);
    }
    let signature_bytes =
        hex::decode(&receipt.signature.value).map_err(|_| HostRuntimeError::Protocol)?;
    signature::UnparsedPublicKey::new(&signature::ED25519, public_key)
        .verify(
            &recipe_run_observation_receipt_signing_bytes(&receipt.claims)
                .map_err(|_| HostRuntimeError::Protocol)?,
            &signature_bytes,
        )
        .map_err(|_| HostRuntimeError::Protocol)?;
    Ok(receipt.claims.outcome == RecipeRunObservationOutcome::Running)
}

fn write_request(root: &Path, digest: &str, body: &[u8]) -> Result<PathBuf, HostRuntimeError> {
    match fs::create_dir(root) {
        Ok(()) => fs::set_permissions(root, fs::Permissions::from_mode(0o700))?,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error.into()),
    }
    let metadata = fs::symlink_metadata(root)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(HostRuntimeError::Protocol);
    }
    let destination = root.join(format!("{digest}.json"));
    match fs::symlink_metadata(&destination) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.permissions().mode() & 0o077 != 0
                || fs::read(&destination)? != body
            {
                return Err(HostRuntimeError::Protocol);
            }
            return Ok(destination);
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| HostRuntimeError::Protocol)?
        .as_nanos();
    let temporary = root.join(format!(".{digest}.{}.{nonce}.tmp", std::process::id()));
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(body)?;
    file.sync_all()?;
    match fs::hard_link(&temporary, &destination) {
        Ok(()) => fs::remove_file(&temporary)?,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            fs::remove_file(&temporary)?;
            let metadata = fs::symlink_metadata(&destination)?;
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.permissions().mode() & 0o077 != 0
                || fs::read(&destination)? != body
            {
                return Err(HostRuntimeError::Protocol);
            }
            return Ok(destination);
        }
        Err(error) => {
            let _ = fs::remove_file(&temporary);
            return Err(error.into());
        }
    }
    File::open(root)?.sync_all()?;
    Ok(destination)
}

fn call_helper(
    socket: &Path,
    body: &[u8],
    read_timeout: Duration,
) -> Result<HelperResponse, HostRuntimeError> {
    if body.is_empty() || body.len() > MAX_HELPER_MESSAGE_BYTES {
        return Err(HostRuntimeError::Protocol);
    }
    let mut stream = UnixStream::connect(socket)?;
    stream.set_read_timeout(Some(read_timeout))?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    stream.write_all(&(body.len() as u32).to_be_bytes())?;
    stream.write_all(body)?;
    stream.flush()?;
    let mut prefix = [0_u8; 4];
    stream.read_exact(&mut prefix)?;
    let length = u32::from_be_bytes(prefix) as usize;
    if length == 0 || length > MAX_HELPER_MESSAGE_BYTES {
        return Err(HostRuntimeError::Protocol);
    }
    let mut response = vec![0_u8; length];
    stream.read_exact(&mut response)?;
    parse_strict(&response).map_err(|_| HostRuntimeError::Protocol)
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

#[cfg(test)]
mod tests {
    use super::{
        HelperResponse, require_inspection_receipt, verify_observation_receipt, write_request,
    };
    use ring::signature::{Ed25519KeyPair, KeyPair};
    use std::fs;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use uuid::Uuid;
    use vonk_agent_protocol::{
        RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY, RecipeRunObservationOutcome,
        RecipeRunObservationReceipt, RecipeRunObservationReceiptClaims,
        RecipeRunObservationReceiptSignature, hex_sha256,
        recipe_run_observation_receipt_signing_bytes,
    };

    fn signed_receipt(signer: &Ed25519KeyPair, request_id: Uuid) -> RecipeRunObservationReceipt {
        let claims = RecipeRunObservationReceiptClaims {
            schema_version: 1,
            authority: RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY.to_owned(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            request_id,
            request_sha256: "a".repeat(64),
            observation_identity_sha256: "b".repeat(64),
            outcome: RecipeRunObservationOutcome::Running,
            observed_at: 105,
        };
        RecipeRunObservationReceipt {
            schema_version: 1,
            signature: RecipeRunObservationReceiptSignature {
                algorithm: "ed25519".to_owned(),
                key_id: hex_sha256(signer.public_key().as_ref()),
                value: hex::encode(
                    signer
                        .sign(&recipe_run_observation_receipt_signing_bytes(&claims).unwrap())
                        .as_ref(),
                ),
            },
            claims,
        }
    }

    #[test]
    fn helper_receipt_signature_is_required_and_bound_to_exact_request() {
        let signer = Ed25519KeyPair::from_seed_unchecked(&[7; 32]).unwrap();
        let request_id = Uuid::new_v4();
        let receipt = signed_receipt(&signer, request_id);
        let public_key: [u8; 32] = signer.public_key().as_ref().try_into().unwrap();
        assert!(
            verify_observation_receipt(
                &receipt,
                &public_key,
                "spk_0123456789abcdef0123456789abcdef",
                &request_id.to_string(),
                &"a".repeat(64),
                &"b".repeat(64),
                100,
                110,
                106,
            )
            .unwrap()
        );

        for mismatch in ["request", "observation", "node", "replay"] {
            let mut changed = receipt.clone();
            let (node, request, observation, issued) = match mismatch {
                "request" => (
                    changed.claims.node_id.clone(),
                    "c".repeat(64),
                    "b".repeat(64),
                    100,
                ),
                "observation" => (
                    changed.claims.node_id.clone(),
                    "a".repeat(64),
                    "c".repeat(64),
                    100,
                ),
                "node" => (
                    "spk_11111111111111111111111111111111".to_owned(),
                    "a".repeat(64),
                    "b".repeat(64),
                    100,
                ),
                _ => {
                    changed.claims.observed_at = 99;
                    (
                        changed.claims.node_id.clone(),
                        "a".repeat(64),
                        "b".repeat(64),
                        100,
                    )
                }
            };
            assert!(
                verify_observation_receipt(
                    &changed,
                    &public_key,
                    &node,
                    &request_id.to_string(),
                    &request,
                    &observation,
                    issued,
                    110,
                    106,
                )
                .is_err()
            );
        }

        let mut forged_outcome = receipt;
        forged_outcome.claims.outcome = RecipeRunObservationOutcome::NotRunning;
        assert!(
            verify_observation_receipt(
                &forged_outcome,
                &public_key,
                "spk_0123456789abcdef0123456789abcdef",
                &request_id.to_string(),
                &"a".repeat(64),
                &"b".repeat(64),
                100,
                110,
                106,
            )
            .is_err()
        );
    }

    #[test]
    fn helper_success_without_an_execution_receipt_is_rejected() {
        let response: HelperResponse = serde_json::from_value(serde_json::json!({
            "schema_version": 1,
            "request_id": Uuid::new_v4().to_string(),
            "status": "container-runtime-request-executed",
            "evidence_sha256": "a".repeat(64)
        }))
        .unwrap();
        assert!(require_inspection_receipt(&response).is_err());
    }

    #[test]
    fn request_is_owner_only_atomic_and_idempotent() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("requests");
        let path = write_request(&root, &"a".repeat(64), b"{}").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"{}");
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(write_request(&root, &"a".repeat(64), b"{}").unwrap(), path);
        assert!(write_request(&root, &"a".repeat(64), b"[]").is_err());
    }

    #[test]
    fn request_root_may_not_be_a_symlink() {
        let temp = tempfile::tempdir().unwrap();
        let target = temp.path().join("target");
        fs::create_dir(&target).unwrap();
        let link = temp.path().join("link");
        symlink(&target, &link).unwrap();
        assert!(write_request(&link, &"a".repeat(64), b"{}").is_err());
    }
}
