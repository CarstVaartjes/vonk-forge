//! Exact, controller-authorized agent package upgrades.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use reqwest::Client;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::{AgentClaim, AgentUpgradeRequest, canonical_json, parse_strict};

use crate::client::{AgentHttpClient, ClientError};

const HELPER_SOCKET: &str = "/run/vonk-forge-package-helper/package-helper.sock";
const MAX_HELPER_MESSAGE_BYTES: usize = 256 * 1024;

#[derive(Debug, Error)]
pub enum AgentUpgradeError {
    #[error("agent upgrade claim is invalid")]
    InvalidClaim,
    #[error("agent upgrade package identity is invalid")]
    DownloadIdentityInvalid,
    #[error("agent upgrade grant is invalid")]
    GrantInvalid,
    #[error("agent upgrade helper rejected the request")]
    HelperRejected,
    #[error("agent upgrade helper rejected the request: {code}")]
    HelperRejectedWithCode {
        code: String,
        exit_code: Option<i32>,
    },
    #[error("agent upgrade helper response is invalid")]
    HelperResponseInvalid,
    #[error("agent upgrade helper is unavailable")]
    HelperUnavailable(#[source] std::io::Error),
    #[error("agent upgrade did not restart the service")]
    RestartNotObserved,
    #[error("agent upgrade transport failed")]
    Transport(#[from] reqwest::Error),
    #[error("agent upgrade storage failed")]
    Io(#[from] std::io::Error),
    #[error("agent upgrade authority is unavailable")]
    Controller(#[from] ClientError),
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HelperResponse {
    schema_version: u8,
    request_id: Option<String>,
    status: String,
    evidence_sha256: Option<String>,
    #[serde(default)]
    error_code: Option<String>,
    #[serde(default)]
    exit_code: Option<i32>,
}

impl AgentUpgradeError {
    pub fn helper_diagnostics(&self) -> Option<(&str, Option<i32>)> {
        match self {
            Self::HelperRejectedWithCode { code, exit_code } => Some((code.as_str(), *exit_code)),
            _ => None,
        }
    }
}

pub struct AgentUpgradeExecutor<'a> {
    pub client: &'a AgentHttpClient,
    pub incoming: &'a Path,
}

impl AgentUpgradeExecutor<'_> {
    pub async fn execute(&self, claim: &AgentClaim) -> Result<(), AgentUpgradeError> {
        let request =
            AgentUpgradeRequest::parse(claim).map_err(|_| AgentUpgradeError::InvalidClaim)?;
        let package = self.download(&request).await?;
        let grant = self
            .client
            .agent_upgrade_grant(claim, &request.package_sha256, &request.package_signature)
            .await?;
        let request_id = grant
            .get("claims")
            .and_then(|claims| claims.get("request_id"))
            .and_then(serde_json::Value::as_str)
            .ok_or(AgentUpgradeError::GrantInvalid)?
            .to_owned();
        let body = canonical_json(&grant).map_err(|_| AgentUpgradeError::GrantInvalid)?;
        let response = tokio::task::spawn_blocking(move || call_helper(&body))
            .await
            .map_err(|_| AgentUpgradeError::HelperResponseInvalid)??;
        validate_helper_response(&response, &request_id, &request.package_sha256)?;
        // A real upgrade restarts this service from dpkg postinst before the helper
        // can answer. Reaching here is intentionally not treated as proof that the
        // new runtime is active; the controller completes only after a fresh claim
        // reports the exact target build and binary identities.
        let _ = fs::remove_file(package);
        Err(AgentUpgradeError::RestartNotObserved)
    }

    async fn download(&self, request: &AgentUpgradeRequest) -> Result<PathBuf, AgentUpgradeError> {
        ensure_private_directory(self.incoming)?;
        let destination = self
            .incoming
            .join(format!("{}.deb", request.package_sha256));
        if verified_file(&destination, request.package_bytes, &request.package_sha256)? {
            return Ok(destination);
        }
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| AgentUpgradeError::DownloadIdentityInvalid)?
            .as_nanos();
        let temporary = self.incoming.join(format!(
            ".{}.{}.{}.tmp",
            request.package_sha256,
            std::process::id(),
            nonce
        ));
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&temporary)?;
        let result = async {
            let client = Client::builder()
                .https_only(true)
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(10))
                .timeout(Duration::from_secs(300))
                .build()?;
            let mut response = client.get(&request.package_url).send().await?;
            if !response.status().is_success()
                || response.content_length() != Some(request.package_bytes)
            {
                return Err(AgentUpgradeError::DownloadIdentityInvalid);
            }
            let mut digest = Sha256::new();
            let mut received = 0_u64;
            while let Some(chunk) = response.chunk().await? {
                received = received
                    .checked_add(chunk.len() as u64)
                    .ok_or(AgentUpgradeError::DownloadIdentityInvalid)?;
                if received > request.package_bytes {
                    return Err(AgentUpgradeError::DownloadIdentityInvalid);
                }
                digest.update(&chunk);
                file.write_all(&chunk)?;
            }
            if received != request.package_bytes
                || hex::encode(digest.finalize()) != request.package_sha256
            {
                return Err(AgentUpgradeError::DownloadIdentityInvalid);
            }
            file.sync_all()?;
            drop(file);
            fs::rename(&temporary, &destination)?;
            File::open(self.incoming)?.sync_all()?;
            Ok(destination.clone())
        }
        .await;
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }
}

fn ensure_private_directory(path: &Path) -> Result<(), AgentUpgradeError> {
    match fs::create_dir(path) {
        Ok(()) => fs::set_permissions(path, fs::Permissions::from_mode(0o700))?,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error.into()),
    }
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(AgentUpgradeError::DownloadIdentityInvalid);
    }
    Ok(())
}

fn verified_file(
    path: &Path,
    expected_bytes: u64,
    expected_digest: &str,
) -> Result<bool, AgentUpgradeError> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error.into()),
    };
    if metadata.file_type().is_symlink()
        || !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.permissions().mode() & 0o077 != 0
        || metadata.len() != expected_bytes
    {
        return Err(AgentUpgradeError::DownloadIdentityInvalid);
    }
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    if hex::encode(digest.finalize()) != expected_digest {
        return Err(AgentUpgradeError::DownloadIdentityInvalid);
    }
    Ok(true)
}

fn call_helper(body: &[u8]) -> Result<HelperResponse, AgentUpgradeError> {
    if body.is_empty() || body.len() > MAX_HELPER_MESSAGE_BYTES {
        return Err(AgentUpgradeError::GrantInvalid);
    }
    let mut stream =
        UnixStream::connect(HELPER_SOCKET).map_err(AgentUpgradeError::HelperUnavailable)?;
    stream
        .set_read_timeout(Some(Duration::from_secs(150)))
        .map_err(AgentUpgradeError::HelperUnavailable)?;
    stream
        .set_write_timeout(Some(Duration::from_secs(10)))
        .map_err(AgentUpgradeError::HelperUnavailable)?;
    stream
        .write_all(&(body.len() as u32).to_be_bytes())
        .map_err(AgentUpgradeError::HelperUnavailable)?;
    stream
        .write_all(body)
        .map_err(AgentUpgradeError::HelperUnavailable)?;
    stream
        .flush()
        .map_err(AgentUpgradeError::HelperUnavailable)?;
    let mut prefix = [0_u8; 4];
    stream
        .read_exact(&mut prefix)
        .map_err(AgentUpgradeError::HelperUnavailable)?;
    let length = u32::from_be_bytes(prefix) as usize;
    if length == 0 || length > MAX_HELPER_MESSAGE_BYTES {
        return Err(AgentUpgradeError::HelperResponseInvalid);
    }
    let mut response = vec![0_u8; length];
    stream
        .read_exact(&mut response)
        .map_err(AgentUpgradeError::HelperUnavailable)?;
    parse_strict(&response).map_err(|_| AgentUpgradeError::HelperResponseInvalid)
}

fn validate_helper_response(
    response: &HelperResponse,
    expected_request_id: &str,
    expected_package_sha256: &str,
) -> Result<(), AgentUpgradeError> {
    if response.schema_version != 1 {
        return Err(AgentUpgradeError::HelperResponseInvalid);
    }
    if response.status == "rejected" {
        if response
            .request_id
            .as_deref()
            .is_some_and(|value| value != expected_request_id)
            || response.evidence_sha256.is_some()
            || response
                .exit_code
                .is_some_and(|value| !(0..=255).contains(&value))
            || (response.exit_code.is_some()
                && response.error_code.as_deref() != Some("package_install_failed"))
            || response
                .error_code
                .as_deref()
                .is_some_and(|value| !stable_helper_error_code(value))
        {
            return Err(AgentUpgradeError::HelperResponseInvalid);
        }
        return Err(match response.error_code.as_deref() {
            Some(error_code) => AgentUpgradeError::HelperRejectedWithCode {
                code: error_code.to_owned(),
                exit_code: response.exit_code,
            },
            None => AgentUpgradeError::HelperRejected,
        });
    }
    let expected_evidence_sha256 = hex::encode(Sha256::digest(expected_package_sha256.as_bytes()));
    if response.request_id.as_deref() != Some(expected_request_id)
        || response.status != "package-installed"
        || response.evidence_sha256.as_deref() != Some(expected_evidence_sha256.as_str())
        || response.exit_code.is_some()
        || response.error_code.is_some()
    {
        return Err(AgentUpgradeError::HelperResponseInvalid);
    }
    Ok(())
}

fn stable_helper_error_code(value: &str) -> bool {
    matches!(
        value,
        "request_invalid"
            | "peer_identity_invalid"
            | "grant_invalid"
            | "grant_node_mismatch"
            | "grant_unauthorized"
            | "request_replayed"
            | "request_ledger_failed"
            | "package_verification_failed"
            | "package_metadata_failed"
            | "package_custody_failed"
            | "package_install_failed"
            | "operation_failed"
            | "concurrency_limit"
    )
}

#[cfg(test)]
mod tests {
    use super::{AgentUpgradeError, HelperResponse, validate_helper_response};
    use sha2::{Digest, Sha256};
    use vonk_agent_protocol::parse_strict;

    const PACKAGE_SHA256: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn package_evidence_sha256() -> String {
        hex::encode(Sha256::digest(PACKAGE_SHA256.as_bytes()))
    }

    fn response(status: &str) -> HelperResponse {
        HelperResponse {
            schema_version: 1,
            request_id: Some("request-1".to_owned()),
            status: status.to_owned(),
            evidence_sha256: None,
            error_code: None,
            exit_code: None,
        }
    }

    #[test]
    fn accepts_old_helper_rejection_without_optional_diagnostics() {
        let response: HelperResponse = parse_strict(
            br#"{"evidence_sha256":null,"request_id":null,"schema_version":1,"status":"rejected"}"#,
        )
        .unwrap();
        assert!(response.error_code.is_none());
        assert!(matches!(
            validate_helper_response(&response, "request-1", PACKAGE_SHA256),
            Err(AgentUpgradeError::HelperRejected)
        ));
    }

    #[test]
    fn accepts_stable_helper_rejection_diagnostics() {
        let mut response = response("rejected");
        response.error_code = Some("operation_failed".to_owned());
        let error = validate_helper_response(&response, "request-1", PACKAGE_SHA256).unwrap_err();
        assert!(matches!(
            &error,
            AgentUpgradeError::HelperRejectedWithCode { .. }
        ));
        assert_eq!(
            error.to_string(),
            "agent upgrade helper rejected the request: operation_failed"
        );
    }

    #[test]
    fn accepts_bounded_package_install_diagnostics() {
        let mut response = response("rejected");
        response.error_code = Some("package_install_failed".to_owned());
        response.exit_code = Some(75);
        let error = validate_helper_response(&response, "request-1", PACKAGE_SHA256).unwrap_err();
        assert_eq!(
            error.helper_diagnostics(),
            Some(("package_install_failed", Some(75)))
        );
        assert_eq!(
            error.to_string(),
            "agent upgrade helper rejected the request: package_install_failed"
        );
    }

    #[test]
    fn rejects_unbounded_or_misbound_package_exit_diagnostics() {
        let mut response = response("rejected");
        response.error_code = Some("package_install_failed".to_owned());
        response.exit_code = Some(256);
        assert!(matches!(
            validate_helper_response(&response, "request-1", PACKAGE_SHA256),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));

        response.error_code = Some("operation_failed".to_owned());
        response.exit_code = Some(1);
        assert!(matches!(
            validate_helper_response(&response, "request-1", PACKAGE_SHA256),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));
    }

    #[test]
    fn rejects_untrusted_helper_diagnostics() {
        let mut response = response("rejected");
        response.error_code = Some("dpkg stderr: secret".to_owned());
        assert!(matches!(
            validate_helper_response(&response, "request-1", PACKAGE_SHA256),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));
    }

    #[test]
    fn distinguishes_invalid_response_from_restart_not_observed() {
        let mut response = response("package-installed");
        response.evidence_sha256 = Some(package_evidence_sha256());
        assert!(validate_helper_response(&response, "request-1", PACKAGE_SHA256).is_ok());

        response.request_id = Some("different-request".to_owned());
        assert!(matches!(
            validate_helper_response(&response, "request-1", PACKAGE_SHA256),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));
        assert_eq!(
            AgentUpgradeError::RestartNotObserved.to_string(),
            "agent upgrade did not restart the service"
        );
    }

    #[test]
    fn rejects_success_evidence_for_a_different_package() {
        let mut response = response("package-installed");
        response.evidence_sha256 = Some(hex::encode(Sha256::digest(b"different-package")));
        assert!(matches!(
            validate_helper_response(&response, "request-1", PACKAGE_SHA256),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));
    }

    #[test]
    fn phase_diagnostics_are_stable_and_secret_free() {
        let diagnostics = [
            (
                AgentUpgradeError::InvalidClaim,
                "agent upgrade claim is invalid",
            ),
            (
                AgentUpgradeError::DownloadIdentityInvalid,
                "agent upgrade package identity is invalid",
            ),
            (
                AgentUpgradeError::GrantInvalid,
                "agent upgrade grant is invalid",
            ),
            (
                AgentUpgradeError::HelperRejected,
                "agent upgrade helper rejected the request",
            ),
            (
                AgentUpgradeError::HelperResponseInvalid,
                "agent upgrade helper response is invalid",
            ),
            (
                AgentUpgradeError::RestartNotObserved,
                "agent upgrade did not restart the service",
            ),
        ];
        for (error, expected) in diagnostics {
            assert_eq!(error.to_string(), expected);
        }
    }
}
