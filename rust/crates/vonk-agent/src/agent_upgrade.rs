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
    #[error("agent upgrade request is invalid")]
    Protocol,
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
}

pub struct AgentUpgradeExecutor<'a> {
    pub client: &'a AgentHttpClient,
    pub incoming: &'a Path,
}

impl AgentUpgradeExecutor<'_> {
    pub async fn execute(&self, claim: &AgentClaim) -> Result<(), AgentUpgradeError> {
        let request = AgentUpgradeRequest::parse(claim).map_err(|_| AgentUpgradeError::Protocol)?;
        let package = self.download(&request).await?;
        let grant = self
            .client
            .agent_upgrade_grant(claim, &request.package_sha256, &request.package_signature)
            .await?;
        let request_id = grant
            .get("claims")
            .and_then(|claims| claims.get("request_id"))
            .and_then(serde_json::Value::as_str)
            .ok_or(AgentUpgradeError::Protocol)?
            .to_owned();
        let body = canonical_json(&grant).map_err(|_| AgentUpgradeError::Protocol)?;
        let response = tokio::task::spawn_blocking(move || call_helper(&body))
            .await
            .map_err(|_| AgentUpgradeError::Protocol)??;
        if response.schema_version != 1
            || response.request_id.as_deref() != Some(request_id.as_str())
            || response.status != "package-installed"
            || response
                .evidence_sha256
                .as_deref()
                .is_none_or(|value| !lower_hex(value, 64))
        {
            return Err(AgentUpgradeError::Protocol);
        }
        // A real upgrade restarts this service from dpkg postinst before the helper
        // can answer. Reaching here is intentionally not treated as proof that the
        // new runtime is active; the controller completes only after a fresh claim
        // reports the exact target build and binary identities.
        let _ = fs::remove_file(package);
        Err(AgentUpgradeError::Protocol)
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
            .map_err(|_| AgentUpgradeError::Protocol)?
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
                return Err(AgentUpgradeError::Protocol);
            }
            let mut digest = Sha256::new();
            let mut received = 0_u64;
            while let Some(chunk) = response.chunk().await? {
                received = received
                    .checked_add(chunk.len() as u64)
                    .ok_or(AgentUpgradeError::Protocol)?;
                if received > request.package_bytes {
                    return Err(AgentUpgradeError::Protocol);
                }
                digest.update(&chunk);
                file.write_all(&chunk)?;
            }
            if received != request.package_bytes
                || hex::encode(digest.finalize()) != request.package_sha256
            {
                return Err(AgentUpgradeError::Protocol);
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
        return Err(AgentUpgradeError::Protocol);
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
        return Err(AgentUpgradeError::Protocol);
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
        return Err(AgentUpgradeError::Protocol);
    }
    Ok(true)
}

fn call_helper(body: &[u8]) -> Result<HelperResponse, AgentUpgradeError> {
    if body.is_empty() || body.len() > MAX_HELPER_MESSAGE_BYTES {
        return Err(AgentUpgradeError::Protocol);
    }
    let mut stream = UnixStream::connect(HELPER_SOCKET)?;
    stream.set_read_timeout(Some(Duration::from_secs(150)))?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    stream.write_all(&(body.len() as u32).to_be_bytes())?;
    stream.write_all(body)?;
    stream.flush()?;
    let mut prefix = [0_u8; 4];
    stream.read_exact(&mut prefix)?;
    let length = u32::from_be_bytes(prefix) as usize;
    if length == 0 || length > MAX_HELPER_MESSAGE_BYTES {
        return Err(AgentUpgradeError::Protocol);
    }
    let mut response = vec![0_u8; length];
    stream.read_exact(&mut response)?;
    parse_strict(&response).map_err(|_| AgentUpgradeError::Protocol)
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}
