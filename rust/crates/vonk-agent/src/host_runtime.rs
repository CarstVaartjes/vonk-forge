//! Narrow client for controller-authorized host container-runtime operations.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use ring::signature;
use thiserror::Error;
use vonk_agent_protocol::generated::HostHelperResponse as HelperResponse;
use vonk_agent_protocol::{
    AgentClaim, HostRuntimeAction, HostRuntimeRequest, RecipeRunInspectionBinding,
    RecipeRunObservationOutcome, RecipeRunObservationReceipt, SignedHostHelperGrant,
    canonical_json, hex_sha256, parse_strict, recipe_run_observation_receipt_signing_bytes,
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
    #[error("host runtime helper rejected request: {code}")]
    HelperRejected {
        code: String,
        diagnostic: Option<String>,
    },
}

impl HostRuntimeError {
    /// Bounded, non-sensitive evidence for the admission receipt.
    pub fn preflight_code(&self) -> String {
        match self {
            Self::Io(_) => "helper_io_failed".to_owned(),
            Self::Controller(ClientError::Protocol) => "helper_grant_invalid".to_owned(),
            Self::Controller(ClientError::Controller(error))
                if matches!(error.status, 401 | 403) =>
            {
                "helper_grant_unauthorized".to_owned()
            }
            Self::Controller(_) => "helper_grant_unavailable".to_owned(),
            Self::Protocol => "helper_protocol_invalid".to_owned(),
            Self::HelperRejected { code, .. } if stable_runtime_error_code(code) => {
                format!("helper_{code}")
            }
            Self::HelperRejected { .. } => "helper_protocol_invalid".to_owned(),
        }
    }

    pub fn diagnostic(&self) -> Option<&str> {
        match self {
            Self::HelperRejected { diagnostic, .. } => diagnostic.as_deref(),
            _ => None,
        }
    }
}

pub struct HostRuntimeOutcome {
    pub exit_code: Option<i32>,
    pub stop_uncertain: bool,
}

pub struct RecipeRunInspectionOutcome {
    pub grant: SignedHostHelperGrant,
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
        let attempt = binding.run_generation;
        let request = HostRuntimeRequest {
            schema_version: 1,
            action: HostRuntimeAction::RunInspect,
            job_id: binding.run_id,
            operation_id: uuid::Uuid::new_v4(),
            attempt,
            fence: uuid::Uuid::new_v4(),
            arguments,
            observation: Some(binding.clone()),
            installation_id: None,
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
        let request_id = authorization.grant.claims.request_id.to_string();
        let grant_bytes =
            canonical_json(&authorization.grant).map_err(|_| HostRuntimeError::Protocol)?;
        let helper_socket = self.helper_socket.to_path_buf();
        let response = tokio::task::spawn_blocking(move || {
            call_helper(&helper_socket, &grant_bytes, Duration::from_secs(15))
        })
        .await
        .map_err(|_| HostRuntimeError::Protocol)??;
        require_bound_response(&response, &request_id)?;
        if response.error_code.is_some() {
            return Err(runtime_rejection(&response, HostRuntimeAction::RunInspect));
        }
        let receipt = require_inspection_receipt(&response)?.clone();
        let issued_at = authorization.grant.claims.issued_at;
        let expires_at = authorization.grant.claims.expires_at;
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
        self.execute_bound(claim, action, arguments, None).await
    }

    pub async fn cleanup_installation(
        &self,
        claim: &AgentClaim,
        installation_id: uuid::Uuid,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        self.execute_bound(
            claim,
            HostRuntimeAction::InstallationCleanup,
            Vec::new(),
            Some(installation_id),
        )
        .await
    }

    async fn execute_bound(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
        installation_id: Option<uuid::Uuid>,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        let helper_timeout = match action {
            HostRuntimeAction::RuntimePreflight => Duration::from_secs(14),
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
            installation_id,
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
                .host_runtime_grant(claim, action, &digest, installation_id)
                .await?;
            let request_id = grant.claims.request_id.to_string();
            let grant = canonical_json(&grant).map_err(|_| HostRuntimeError::Protocol)?;
            let helper_socket = self.helper_socket.to_path_buf();
            let response = tokio::task::spawn_blocking(move || {
                call_helper(&helper_socket, &grant, helper_timeout)
            })
            .await
            .map_err(|_| HostRuntimeError::Protocol)??;
            let stop_uncertain = response.status == "container-runtime-stop-uncertain";
            require_bound_response(&response, &request_id)?;
            if response.error_code.is_some() {
                return Err(runtime_rejection(&response, action));
            }
            if response.observation_receipt.is_some() {
                return Err(HostRuntimeError::Protocol);
            }
            if response.diagnostic.is_some()
                || !stop_uncertain && response.status != "container-runtime-request-executed"
            {
                return Err(HostRuntimeError::Protocol);
            }
            if response
                .evidence_sha256
                .as_deref()
                .is_none_or(|value| !lower_hex(value, 64))
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
                exit_code: response.exit_code.map(|code| code as i32),
                stop_uncertain,
            })
        }
        .await
    }
}

/// Bind the helper's reply to the request this agent sent before anything in it
/// is trusted.
///
/// The helper attaches the request identity only once it has authorized the
/// grant, so a rejection raised by an earlier check arrives with
/// `request_id: null`. That is the normal shape of the helper saying which check
/// refused -- `grant_node_mismatch` and `grant_unauthorized` cannot be reported
/// any other way -- and demanding an identity that the helper deliberately
/// withholds reported each of them as a malformed reply instead. The code set
/// keeps acceptance closed to the rejections the helper can only raise before it
/// trusts the grant, so an unbound reply can never be mistaken for a bound one.
fn require_bound_response(
    response: &HelperResponse,
    request_id: &str,
) -> Result<(), HostRuntimeError> {
    if response.schema_version != 1 {
        return Err(HostRuntimeError::Protocol);
    }
    if response.request_id.map(|id| id.to_string()).as_deref() == Some(request_id) {
        return Ok(());
    }
    if response.request_id.is_none()
        && response
            .error_code
            .as_deref()
            .is_some_and(unbound_rejection_is_expected)
    {
        return Ok(());
    }
    Err(HostRuntimeError::Protocol)
}

/// The codes the helper can only raise before it has trusted the grant, and so
/// the only rejections that legitimately arrive without the request identity.
/// Every other code is produced after the helper knows the request, so an
/// unbound reply claiming one is a reply this agent cannot account for.
fn unbound_rejection_is_expected(code: &str) -> bool {
    matches!(
        code,
        "grant_invalid"
            | "grant_node_mismatch"
            | "grant_unauthorized"
            | "peer_identity_invalid"
            | "request_invalid"
    )
}

fn runtime_rejection(response: &HelperResponse, action: HostRuntimeAction) -> HostRuntimeError {
    let Some(code) = response.error_code.as_deref() else {
        return HostRuntimeError::Protocol;
    };
    if response.status != "rejected"
        || response.evidence_sha256.is_some()
        || response.exit_code.is_some()
        || response.observation_receipt.is_some()
        || !stable_runtime_error_code(code)
        || response.diagnostic.is_some()
            && (action != HostRuntimeAction::RunInspect || code != "runtime_process_exited")
    {
        return HostRuntimeError::Protocol;
    }
    HostRuntimeError::HelperRejected {
        code: code.to_owned(),
        diagnostic: response
            .diagnostic
            .as_deref()
            .map(crate::failure_evidence::sanitize_text),
    }
}

fn stable_runtime_error_code(value: &str) -> bool {
    // Every code the privileged helper can name, not only the operation ones.
    // The helper's grant, peer and request rejections were absent, so the agent
    // reported each of them as an opaque protocol error even when the helper had
    // said which check refused -- which is how a live privileged start became
    // unattributable.
    matches!(
        value,
        "operation_failed"
            | "operation_invalid"
            | "operation_unsafe_path"
            | "operation_invalid_artifact"
            | "operation_command_failed"
            | "operation_stop_uncertain"
            | "operation_io"
            | "runtime_image_load_failed"
            | "runtime_image_inspect_failed"
            | "runtime_image_identity_invalid"
            | "runtime_image_receipt_failed"
            | "runtime_process_exited"
            | "runtime_run_missing"
            | "runtime_fabric_unavailable"
            | "runtime_fabric_firewall_rejected"
            | "grant_invalid"
            | "grant_node_mismatch"
            | "grant_unauthorized"
            | "peer_identity_invalid"
            | "request_invalid"
            | "request_replayed"
            | "request_ledger_failed"
    )
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
        HostRuntimeAction, RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY, RecipeRunObservationOutcome,
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
    fn runtime_rejection_binds_and_redacts_captured_process_logs() {
        let mut response: super::HelperResponse = vonk_agent_protocol::parse_strict(
            br#"{"schema_version":1,"request_id":null,"status":"rejected","evidence_sha256":null,"error_code":"runtime_process_exited","diagnostic":"ModuleNotFoundError: runtime module\nAPI_TOKEN=private-value\n"}"#,
        ).unwrap();
        let error = super::runtime_rejection(&response, HostRuntimeAction::RunInspect);
        assert!(error.diagnostic().unwrap().contains("ModuleNotFoundError"));
        assert!(!error.diagnostic().unwrap().contains("private-value"));
        assert!(matches!(
            super::runtime_rejection(&response, HostRuntimeAction::Start),
            super::HostRuntimeError::Protocol
        ));
        response.error_code = Some("operation_unsafe_path".into());
        assert!(matches!(
            super::runtime_rejection(&response, HostRuntimeAction::RunInspect),
            super::HostRuntimeError::Protocol
        ));
        // A rejection never carries the observation receipt that only an
        // executed run produces, whatever the action claimed it ran.
        response.error_code = Some("runtime_process_exited".into());
        response.observation_receipt = Some(signed_receipt(
            &Ed25519KeyPair::from_seed_unchecked(&[7; 32]).unwrap(),
            Uuid::new_v4(),
        ));
        assert!(matches!(
            super::runtime_rejection(&response, HostRuntimeAction::RunInspect),
            super::HostRuntimeError::Protocol
        ));
    }

    #[test]
    fn unbound_helper_rejection_names_the_check_that_refused() {
        // The helper attaches the request identity only after it authorizes the
        // grant, so these refusals cannot echo it. Requiring the identity anyway
        // reported each as `helper_protocol_invalid` -- the same label a corrupt
        // reply gets -- which is how a live privileged start became
        // unattributable with no diagnostic to read.
        let request_id = "10000000-0000-4000-8000-000000000001";
        for (code, expected) in [
            ("grant_node_mismatch", "helper_grant_node_mismatch"),
            ("grant_unauthorized", "helper_grant_unauthorized"),
            ("peer_identity_invalid", "helper_peer_identity_invalid"),
        ] {
            let response: super::HelperResponse = vonk_agent_protocol::parse_strict(
                format!(
                    r#"{{"schema_version":1,"request_id":null,"status":"rejected","evidence_sha256":null,"error_code":"{code}"}}"#
                )
                .as_bytes(),
            )
            .unwrap();
            super::require_bound_response(&response, request_id).unwrap();
            assert_eq!(
                super::runtime_rejection(&response, HostRuntimeAction::Start).preflight_code(),
                expected
            );
        }
    }

    #[test]
    fn a_reply_this_agent_cannot_bind_is_still_a_protocol_error() {
        let request_id = "10000000-0000-4000-8000-000000000001";
        let rejected_for_another_request: super::HelperResponse =
            vonk_agent_protocol::parse_strict(
                br#"{"schema_version":1,"request_id":"20000000-0000-4000-8000-000000000002","status":"rejected","evidence_sha256":null,"error_code":"grant_unauthorized"}"#,
            )
            .unwrap();
        assert!(super::require_bound_response(&rejected_for_another_request, request_id).is_err());

        // Every other code is produced only after the helper knows the request,
        // so an unbound reply claiming one cannot be accounted for.
        for code in [
            "request_replayed",
            "request_ledger_failed",
            "operation_failed",
            "runtime_process_exited",
        ] {
            let unbound: super::HelperResponse = vonk_agent_protocol::parse_strict(
                format!(
                    r#"{{"schema_version":1,"request_id":null,"status":"rejected","evidence_sha256":null,"error_code":"{code}"}}"#
                )
                .as_bytes(),
            )
            .unwrap();
            assert!(
                super::require_bound_response(&unbound, request_id).is_err(),
                "{code} must not be accepted without a request identity"
            );
        }

        let unbound_success: super::HelperResponse = vonk_agent_protocol::parse_strict(
            br#"{"schema_version":1,"request_id":null,"status":"container-runtime-request-executed","evidence_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
        )
        .unwrap();
        assert!(super::require_bound_response(&unbound_success, request_id).is_err());
    }

    #[test]
    fn preflight_reports_the_failed_boundary_without_exposing_error_details() {
        use super::HostRuntimeError;
        use crate::client::ClientError;
        let cases = [
            (
                HostRuntimeError::Controller(ClientError::Protocol),
                "helper_grant_invalid",
            ),
            (
                HostRuntimeError::Controller(ClientError::Controller(Box::new(
                    crate::client::ControllerError::from_status(403),
                ))),
                "helper_grant_unauthorized",
            ),
            (
                HostRuntimeError::Controller(ClientError::Retryable),
                "helper_grant_unavailable",
            ),
            (
                HostRuntimeError::Io(std::io::Error::other("private path or transport detail")),
                "helper_io_failed",
            ),
            (HostRuntimeError::Protocol, "helper_protocol_invalid"),
            (
                HostRuntimeError::HelperRejected {
                    code: "operation_unsafe_path".to_owned(),
                    diagnostic: None,
                },
                "helper_operation_unsafe_path",
            ),
            // The helper's own grant and request rejections name the refusing
            // check, so the operator must see them rather than a protocol error.
            (
                HostRuntimeError::HelperRejected {
                    code: "grant_unauthorized".to_owned(),
                    diagnostic: None,
                },
                "helper_grant_unauthorized",
            ),
            (
                HostRuntimeError::HelperRejected {
                    code: "grant_node_mismatch".to_owned(),
                    diagnostic: None,
                },
                "helper_grant_node_mismatch",
            ),
            (
                HostRuntimeError::HelperRejected {
                    code: "request_replayed".to_owned(),
                    diagnostic: None,
                },
                "helper_request_replayed",
            ),
            (
                HostRuntimeError::HelperRejected {
                    code: "untrusted response detail".to_owned(),
                    diagnostic: None,
                },
                "helper_protocol_invalid",
            ),
        ];
        for (error, expected) in cases {
            assert_eq!(error.preflight_code(), expected);
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
