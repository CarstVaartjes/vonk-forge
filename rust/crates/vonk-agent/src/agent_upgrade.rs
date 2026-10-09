//! Exact, controller-authorized agent package upgrades.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use reqwest::Client;
use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::generated::HelperErrorCode;
use vonk_agent_protocol::generated::HostHelperResponse as HelperResponse;
use vonk_agent_protocol::generated::HostHelperResponseStatus;
use vonk_agent_protocol::{AgentClaim, AgentUpgradeRequest, canonical_json, parse_strict};

use crate::client::{AgentHttpClient, ClientError};

const HELPER_SOCKET: &str = "/run/vonk-forge-package-helper/package-helper.sock";
/// Shared with the host-runtime frame and the privileged helper.
const MAX_HELPER_MESSAGE_BYTES: usize = vonk_agent_protocol::MAX_HELPER_FRAME_BYTES;

/// The by-design handoff after the signed helper accepted the package.
///
/// A real upgrade restarts this service from dpkg postinst before the helper
/// can answer, so an answered helper is not proof that the new runtime is
/// active.  The Controller completes the upgrade only from a later
/// authenticated claim that reports the exact target build and binary
/// identities.  The recorded body must name that awaiting state: reusing a
/// failure observation here makes a completed install read to an operator as a
/// failed upgrade.  The executor reports this same reason, so it is named once.
pub const UPGRADE_AWAITING_IDENTITY_REASON: &str =
    "agent upgrade installed the package; awaiting identity confirmation";

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
        code: HelperErrorCode,
        exit_code: Option<i32>,
        diagnostic: Option<String>,
    },
    #[error("agent upgrade helper response is invalid")]
    HelperResponseInvalid,
    #[error("agent upgrade helper is unavailable")]
    HelperUnavailable(#[source] std::io::Error),
    #[error("{}", UPGRADE_AWAITING_IDENTITY_REASON)]
    RestartNotObserved,
    #[error("agent upgrade transport failed")]
    Transport(#[from] reqwest::Error),
    #[error("agent upgrade storage failed")]
    Io(#[from] std::io::Error),
    #[error("agent upgrade authority is unavailable")]
    Controller(#[from] ClientError),
}

impl AgentUpgradeError {
    pub fn diagnostic(&self) -> Option<&str> {
        match self {
            Self::HelperRejectedWithCode { diagnostic, .. } => diagnostic.as_deref(),
            _ => None,
        }
    }

    pub fn helper_diagnostics(&self) -> Option<(HelperErrorCode, Option<i32>)> {
        match self {
            Self::HelperRejectedWithCode {
                code, exit_code, ..
            } => Some((*code, *exit_code)),
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
        self.download(
            &request.source_package_url,
            request.source_package_bytes.into(),
            &request.rollback.source.package_sha256,
        )
        .await?;
        let package = self
            .download(
                &request.package_url,
                request.package_bytes.into(),
                &request.package_sha256,
            )
            .await?;
        let grant = self
            .client
            .agent_upgrade_grant(claim, &request.package_sha256, &request.package_signature)
            .await?;
        let request_id = grant.claims.request_id.to_string();
        let body = canonical_json(&grant).map_err(|_| AgentUpgradeError::GrantInvalid)?;
        let response = tokio::task::spawn_blocking(move || call_helper(&body))
            .await
            .map_err(|_| AgentUpgradeError::HelperResponseInvalid)??;
        validate_helper_response(&response, &request_id)?;
        if response.status != HostHelperResponseStatus::PackageInstalled {
            return Err(AgentUpgradeError::HelperResponseInvalid);
        }
        // A real upgrade restarts this service from dpkg postinst before the helper
        // can answer. Reaching here is intentionally not treated as proof that the
        // new runtime is active; the controller completes only after a fresh claim
        // reports the exact target build and binary identities.
        let _ = fs::remove_file(package);
        Err(AgentUpgradeError::RestartNotObserved)
    }

    async fn download(
        &self,
        package_url: &str,
        package_bytes: u64,
        package_sha256: &str,
    ) -> Result<PathBuf, AgentUpgradeError> {
        ensure_private_directory(self.incoming)?;
        let destination = self.incoming.join(format!("{}.deb", package_sha256));
        if verified_file(&destination, package_bytes, package_sha256)? {
            return Ok(destination);
        }
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| AgentUpgradeError::DownloadIdentityInvalid)?
            .as_nanos();
        let temporary = self.incoming.join(format!(
            ".{}.{}.{}.tmp",
            package_sha256,
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
            let mut response = client.get(package_url).send().await?;
            if !response.status().is_success() || response.content_length() != Some(package_bytes) {
                return Err(AgentUpgradeError::DownloadIdentityInvalid);
            }
            let mut digest = Sha256::new();
            let mut received = 0_u64;
            while let Some(chunk) = response.chunk().await? {
                received = received
                    .checked_add(chunk.len() as u64)
                    .ok_or(AgentUpgradeError::DownloadIdentityInvalid)?;
                if received > package_bytes {
                    return Err(AgentUpgradeError::DownloadIdentityInvalid);
                }
                digest.update(&chunk);
                file.write_all(&chunk)?;
            }
            if received != package_bytes || hex::encode(digest.finalize()) != package_sha256 {
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

pub(crate) fn call_helper(body: &[u8]) -> Result<HelperResponse, AgentUpgradeError> {
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

pub(crate) fn validate_helper_response(
    response: &HelperResponse,
    expected_request_id: &str,
) -> Result<(), AgentUpgradeError> {
    if response.schema_version != 1
        || response.process_running.is_some()
        || response.installation_intent_nonce.is_some()
    {
        return Err(AgentUpgradeError::HelperResponseInvalid);
    }
    if response.status == HostHelperResponseStatus::Rejected {
        let reported = response.error_code.as_deref();
        let code = reported.and_then(crate::helper_codes::upgrade_rejection);
        if response
            .request_id
            .map(|id| id.to_string())
            .as_deref()
            .is_some_and(|value| value != expected_request_id)
            || response
                .exit_code
                .is_some_and(|value| !(0..=255).contains(&value))
            || (response.exit_code.is_some() && code != Some(HelperErrorCode::PackageInstallFailed))
            || (reported.is_some() && code.is_none())
        {
            return Err(AgentUpgradeError::HelperResponseInvalid);
        }
        return Err(match code {
            Some(code) => AgentUpgradeError::HelperRejectedWithCode {
                code,
                exit_code: response.exit_code.map(|code| code as i32),
                diagnostic: response
                    .diagnostic
                    .as_deref()
                    .map(crate::failure_evidence::sanitize_text),
            },
            None => AgentUpgradeError::HelperRejected,
        });
    }
    if response.request_id.map(|id| id.to_string()).as_deref() != Some(expected_request_id)
        || !matches!(
            response.status,
            HostHelperResponseStatus::PackageInstalled
                | HostHelperResponseStatus::PackageActivationConfirmed
        )
        || response.exit_code.is_some()
        || response.error_code.is_some()
    {
        return Err(AgentUpgradeError::HelperResponseInvalid);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        AgentUpgradeError, HelperErrorCode, HelperResponse, HostHelperResponseStatus,
        validate_helper_response,
    };
    use vonk_agent_protocol::{canonical_generated_json, parse_strict};

    fn response(status: HostHelperResponseStatus) -> HelperResponse {
        HelperResponse {
            diagnostic: None,
            process_logs: None,
            installation_intent_nonce: None,
            schema_version: 1,
            request_id: Some("10000000-0000-4000-8000-000000000001".parse().unwrap()),
            status,
            error_code: None,
            exit_code: None,
            process_running: None,
        }
    }

    #[test]
    fn preparation_observation_survives_helper_wire_and_fresh_completion() {
        let request_id = "10000000-0000-4000-8000-000000000001";
        let mut pending = response(HostHelperResponseStatus::Rejected);
        pending.error_code = Some(
            HelperErrorCode::PackagePreparationUnavailable
                .as_str()
                .to_owned(),
        );
        let body = canonical_generated_json(&pending).unwrap();
        let observed = parse_strict(&body).unwrap();
        let error = validate_helper_response(&observed, request_id).unwrap_err();
        // This retained observation reaches the executor's temporary-dependency
        // path; a malformed-response implementation loses the helper evidence.
        assert!(error.helper_diagnostics().is_some());
        validate_helper_response(
            &response(HostHelperResponseStatus::PackageInstalled),
            request_id,
        )
        .unwrap();
    }

    #[test]
    fn package_failure_detail_survives_wire_roundtrip_and_redacts_secrets() {
        let mut response = response(HostHelperResponseStatus::Rejected);
        response.error_code = Some("package_install_failed".into());
        response.exit_code = Some(1);
        response.diagnostic = Some("permission denied\npassword=do-not-expose".into());
        let response = parse_strict(&canonical_generated_json(&response).unwrap()).unwrap();
        let error = validate_helper_response(&response, "10000000-0000-4000-8000-000000000001")
            .unwrap_err();
        assert!(error.diagnostic().unwrap().contains("permission denied"));
        assert!(!error.diagnostic().unwrap().contains("do-not-expose"));
    }

    #[test]
    fn package_context_rejects_the_shared_inspection_outcome_field() {
        let mut response = response(HostHelperResponseStatus::PackageInstalled);
        response.process_running = Some(true);
        let mut response: HelperResponse =
            parse_strict(&canonical_generated_json(&response).unwrap()).unwrap();
        assert!(matches!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));
        response.status = HostHelperResponseStatus::Rejected;
        assert!(matches!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));
    }

    #[test]
    fn accepts_rejection_without_optional_diagnostics() {
        let response: HelperResponse =
            parse_strict(br#"{"request_id":null,"schema_version":1,"status":"rejected"}"#).unwrap();
        assert!(response.error_code.is_none());
        assert!(matches!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",),
            Err(AgentUpgradeError::HelperRejected)
        ));
    }

    #[test]
    fn accepts_stable_helper_rejection_diagnostics() {
        let mut response = response(HostHelperResponseStatus::Rejected);
        response.error_code = Some("operation_failed".to_owned());
        let error = validate_helper_response(&response, "10000000-0000-4000-8000-000000000001")
            .unwrap_err();
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
        let mut response = response(HostHelperResponseStatus::Rejected);
        response.error_code = Some("package_install_failed".to_owned());
        response.exit_code = Some(75);
        let error = validate_helper_response(&response, "10000000-0000-4000-8000-000000000001")
            .unwrap_err();
        assert_eq!(
            error.helper_diagnostics(),
            Some((HelperErrorCode::PackageInstallFailed, Some(75)))
        );
        assert_eq!(
            error.to_string(),
            "agent upgrade helper rejected the request: package_install_failed"
        );
    }

    #[test]
    fn rejects_unbounded_or_misbound_package_exit_diagnostics() {
        let mut response = response(HostHelperResponseStatus::Rejected);
        response.error_code = Some("package_install_failed".to_owned());
        response.exit_code = Some(256);
        assert!(matches!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));

        response.error_code = Some("operation_failed".to_owned());
        response.exit_code = Some(1);
        assert!(matches!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));
    }

    #[test]
    fn rejects_untrusted_helper_diagnostics() {
        let mut response = response(HostHelperResponseStatus::Rejected);
        response.error_code = Some("dpkg stderr: secret".to_owned());
        assert!(matches!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));
    }

    #[test]
    fn distinguishes_invalid_response_from_restart_not_observed() {
        let mut response = response(HostHelperResponseStatus::PackageInstalled);
        assert!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_ok()
        );

        response.request_id = Some("20000000-0000-4000-8000-000000000002".parse().unwrap());
        assert!(matches!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",),
            Err(AgentUpgradeError::HelperResponseInvalid)
        ));
        assert_eq!(
            AgentUpgradeError::RestartNotObserved.to_string(),
            "agent upgrade installed the package; awaiting identity confirmation"
        );
    }

    #[test]
    fn the_by_design_handoff_names_its_awaiting_state_not_a_failure() {
        // Wrong implementation: the successful helper-install handoff reused the
        // failure observation "agent upgrade did not restart the service", so the
        // Controller's operator surface recorded a completed install as a failed
        // upgrade.  The handoff must name the state the Controller waits in.
        let handoff = AgentUpgradeError::RestartNotObserved.to_string();
        assert!(
            handoff.contains("awaiting identity confirmation") && !handoff.contains("did not"),
            "the by-design install handoff must name its awaiting state, not a failure: {handoff:?}"
        );
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
                "agent upgrade installed the package; awaiting identity confirmation",
            ),
        ];
        for (error, expected) in diagnostics {
            assert_eq!(error.to_string(), expected);
        }
    }
}
