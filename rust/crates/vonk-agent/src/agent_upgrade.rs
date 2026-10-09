//! Exact, controller-authorized agent package upgrades.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;

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
    #[error("agent upgrade transport failed")]
    Transport(#[from] reqwest::Error),
    #[error("agent upgrade storage failed")]
    Io(#[from] std::io::Error),
    #[error("agent upgrade authority is unavailable")]
    Controller(#[from] ClientError),
}

impl AgentUpgradeError {
    pub fn security_edge(&self) -> bool {
        matches!(self, Self::DownloadIdentityInvalid)
            || matches!(self, Self::Controller(error) if error.fatal())
            || matches!(self, Self::HelperRejectedWithCode { code, .. } if matches!(code,
                HelperErrorCode::GrantInvalid | HelperErrorCode::GrantNodeMismatch |
                HelperErrorCode::GrantUnauthorized | HelperErrorCode::PeerIdentityInvalid |
                HelperErrorCode::PackageVerificationFailed))
    }

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
        self.download(
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
        let body = canonical_json(&grant).map_err(|_| AgentUpgradeError::HelperResponseInvalid)?;
        let response = tokio::task::spawn_blocking(move || call_helper(&body))
            .await
            .map_err(|_| AgentUpgradeError::HelperResponseInvalid)??;
        // A real upgrade restarts this service from dpkg postinst before the helper
        // can answer. Reaching here is intentionally not treated as proof that the
        // new runtime is active; the controller completes only after a fresh claim
        // reports the exact target build and binary identities.
        installed_handoff(&response, &request_id)
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
        // The content digest owns this partial, so cancellation, response loss
        // and process death preserve compatible bytes for the next request.
        let temporary = self.incoming.join(format!(".{package_sha256}.partial"));
        if let Ok(metadata) = fs::symlink_metadata(&temporary)
            && (!metadata.is_file()
                || metadata.file_type().is_symlink()
                || metadata.nlink() != 1
                || metadata.len() > package_bytes
                || metadata.permissions().mode() & 0o077 != 0)
        {
            isolate_file(&temporary)?;
        }
        if fs::symlink_metadata(&temporary).is_ok_and(|metadata| metadata.len() == package_bytes)
            && verified_file(&temporary, package_bytes, package_sha256)?
        {
            fs::rename(&temporary, &destination)?;
            File::open(self.incoming)?.sync_all()?;
            return Ok(destination);
        }
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .mode(0o600)
            .open(&temporary)?;

        let result = async {
            let client = Client::builder()
                .https_only(true)
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(10))
                .timeout(Duration::from_secs(300))
                .build()?;
            let retained = file.metadata()?.len();
            let mut request = client.get(package_url);
            if retained > 0 {
                request = request.header(reqwest::header::RANGE, format!("bytes={retained}-"));
            }
            let mut response = request.send().await?;
            let resumed = retained > 0 && response.status() == reqwest::StatusCode::PARTIAL_CONTENT;
            if resumed {
                let expected = format!("bytes {retained}-{}/{package_bytes}", package_bytes - 1);
                if response
                    .headers()
                    .get(reqwest::header::CONTENT_RANGE)
                    .and_then(|value| value.to_str().ok())
                    != Some(expected.as_str())
                {
                    return Err(AgentUpgradeError::HelperResponseInvalid);
                }
            } else if response.status() == reqwest::StatusCode::OK {
                file.set_len(0)?;
            } else {
                return Err(AgentUpgradeError::HelperResponseInvalid);
            }
            let offset = if resumed { retained } else { 0 };
            if response.content_length() != Some(package_bytes - offset) {
                return Err(AgentUpgradeError::HelperResponseInvalid);
            }
            let mut digest = Sha256::new();
            file.seek(SeekFrom::Start(0))?;
            let mut buffer = [0_u8; 64 * 1024];
            let mut received = 0_u64;
            let deadline = std::time::Instant::now() + Duration::from_secs(300);
            while received < offset && std::time::Instant::now() < deadline {
                let count = file.read(&mut buffer)?;
                if count == 0 {
                    return Err(std::io::Error::other("partial package unavailable").into());
                }
                digest.update(&buffer[..count]);
                received += count as u64;
            }
            if received != offset {
                return Err(
                    std::io::Error::other("partial package observation budget elapsed").into(),
                );
            }
            file.seek(SeekFrom::Start(offset))?;
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
            if received != package_bytes {
                return Err(AgentUpgradeError::HelperResponseInvalid);
            }
            if hex::encode(digest.finalize()) != package_sha256 {
                return Err(AgentUpgradeError::DownloadIdentityInvalid);
            }
            file.sync_all()?;
            drop(file);
            fs::rename(&temporary, &destination)?;
            File::open(self.incoming)?.sync_all()?;
            Ok(destination.clone())
        }
        .await;
        if matches!(result, Err(AgentUpgradeError::DownloadIdentityInvalid)) {
            let _ = isolate_file(&temporary);
        }
        result
    }
}

fn ensure_private_directory(path: &Path) -> Result<(), AgentUpgradeError> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error.into()),
    }
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        isolate_file(path)?;
        fs::DirBuilder::new().mode(0o700).create(path)?;
    } else if metadata.permissions().mode() & 0o077 != 0 {
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
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
        isolate_file(path)?;
        return Ok(false);
    }
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    let deadline = std::time::Instant::now() + Duration::from_secs(300);
    let mut measured = 0_u64;
    while std::time::Instant::now() < deadline {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        measured = measured.saturating_add(count as u64);
        if measured > expected_bytes {
            isolate_file(path)?;
            return Ok(false);
        }
        digest.update(&buffer[..count]);
    }
    if measured != expected_bytes {
        return Err(std::io::Error::other("package measurement observation unavailable").into());
    }
    if hex::encode(digest.finalize()) != expected_digest {
        isolate_file(path)?;
        return Ok(false);
    }
    Ok(true)
}

fn isolate_file(path: &Path) -> Result<(), AgentUpgradeError> {
    let isolated = path.with_extension(format!("unavailable-{}", uuid::Uuid::new_v4()));
    fs::rename(path, isolated)?;
    File::open(
        path.parent()
            .ok_or_else(|| std::io::Error::other("package parent unavailable"))?,
    )?
    .sync_all()?;
    Ok(())
}

pub(crate) fn installed_handoff(
    response: &HelperResponse,
    request_id: &str,
) -> Result<(), AgentUpgradeError> {
    validate_helper_response(response, request_id)?;
    if response.status != HostHelperResponseStatus::PackageInstalled {
        return Err(AgentUpgradeError::HelperResponseInvalid);
    }
    Ok(())
}

pub(crate) fn call_helper(body: &[u8]) -> Result<HelperResponse, AgentUpgradeError> {
    call_helper_until(
        Path::new(HELPER_SOCKET),
        body,
        std::time::Instant::now() + Duration::from_secs(150),
    )
}

pub(crate) fn call_helper_until(
    socket: &Path,
    body: &[u8],
    deadline: std::time::Instant,
) -> Result<HelperResponse, AgentUpgradeError> {
    if body.is_empty() || body.len() > MAX_HELPER_MESSAGE_BYTES {
        return Err(AgentUpgradeError::HelperResponseInvalid);
    }
    let mut stream = UnixStream::connect(socket).map_err(AgentUpgradeError::HelperUnavailable)?;
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
    read_until(&mut stream, &mut prefix, deadline)?;
    let length = u32::from_be_bytes(prefix) as usize;
    if length == 0 || length > MAX_HELPER_MESSAGE_BYTES {
        return Err(AgentUpgradeError::HelperResponseInvalid);
    }
    let mut response = vec![0_u8; length];
    read_until(&mut stream, &mut response, deadline)?;
    parse_strict(&response).map_err(|_| AgentUpgradeError::HelperResponseInvalid)
}

fn read_until(
    stream: &mut UnixStream,
    bytes: &mut [u8],
    deadline: std::time::Instant,
) -> Result<(), AgentUpgradeError> {
    let mut received = 0;
    while received < bytes.len() && std::time::Instant::now() < deadline {
        stream.set_read_timeout(Some(
            deadline
                .saturating_duration_since(std::time::Instant::now())
                .max(Duration::from_nanos(1)),
        ))?;
        let count = stream.read(&mut bytes[received..])?;
        if count == 0 {
            return Err(std::io::Error::from(std::io::ErrorKind::UnexpectedEof).into());
        }
        received += count;
    }
    if received != bytes.len() {
        return Err(std::io::Error::from(std::io::ErrorKind::TimedOut).into());
    }
    Ok(())
}

pub(crate) fn validate_helper_response(
    response: &HelperResponse,
    expected_request_id: &str,
) -> Result<(), AgentUpgradeError> {
    if response.schema_version != 1 || response.process_running.is_some() {
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
    use super::*;
    use vonk_agent_protocol::{canonical_generated_json, parse_strict};

    fn response(status: HostHelperResponseStatus) -> HelperResponse {
        HelperResponse {
            diagnostic: None,
            process_logs: None,
            schema_version: 1,
            request_id: Some("10000000-0000-4000-8000-000000000001".parse().unwrap()),
            status,
            error_code: None,
            exit_code: None,
            process_running: None,
        }
    }

    #[test]
    fn package_failure_detail_survives_wire_roundtrip_and_redacts_secrets() {
        let mut response = response(HostHelperResponseStatus::Rejected);
        response.error_code = Some(HelperErrorCode::PackageInstallFailed.as_str().into());
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
        assert!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err()
        );
        response.status = HostHelperResponseStatus::Rejected;
        assert!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err()
        );
    }

    #[test]
    fn accepts_rejection_without_optional_diagnostics() {
        let response: HelperResponse =
            parse_strict(br#"{"request_id":null,"schema_version":1,"status":"rejected"}"#).unwrap();
        assert!(response.error_code.is_none());
        assert!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err()
        );
    }

    #[test]
    fn accepts_bounded_package_install_diagnostics() {
        let mut response = response(HostHelperResponseStatus::Rejected);
        response.error_code = Some(HelperErrorCode::PackageInstallFailed.as_str().to_owned());
        response.exit_code = Some(75);
        let error = validate_helper_response(&response, "10000000-0000-4000-8000-000000000001")
            .unwrap_err();
        assert_eq!(
            error.helper_diagnostics(),
            Some((HelperErrorCode::PackageInstallFailed, Some(75)))
        );
    }

    #[test]
    fn rejects_unbounded_or_misbound_package_exit_diagnostics() {
        let mut response = response(HostHelperResponseStatus::Rejected);
        response.error_code = Some(HelperErrorCode::PackageInstallFailed.as_str().to_owned());
        response.exit_code = Some(256);
        assert!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err()
        );

        response.error_code = Some(HelperErrorCode::OperationFailed.as_str().to_owned());
        response.exit_code = Some(1);
        assert!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err()
        );
    }

    #[test]
    fn rejects_untrusted_helper_diagnostics() {
        let mut response = response(HostHelperResponseStatus::Rejected);
        response.error_code = Some("dpkg stderr: secret".to_owned());
        assert!(
            validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err()
        );
    }

    #[test]
    fn damaged_retained_package_is_a_miss_and_fresh_verified_content_is_reused() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("package.deb");
        let bytes = b"signed package contents";
        let digest = hex::encode(Sha256::digest(bytes));
        for damaged in [
            b"truncated".as_slice(),
            b"wrong package contents!".as_slice(),
        ] {
            fs::write(&path, damaged).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
            assert!(!verified_file(&path, bytes.len() as u64, &digest).unwrap());
            assert!(!path.exists());
            fs::write(&path, bytes).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
            assert!(verified_file(&path, bytes.len() as u64, &digest).unwrap());
            assert_eq!(fs::read(&path).unwrap(), bytes);
        }
    }
}
