//! Root-owned activation evidence and the signed Controller acknowledgement.
use std::fs::File;
use std::io::Read;
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use rustix::fs::{Mode, OFlags};
use vonk_agent_protocol::{PackageActivationPhase, PackageActivationReceipt, canonical_json, parse_strict};
use crate::agent_upgrade::{AgentUpgradeError, call_helper, validate_helper_response};
use crate::client::AgentHttpClient;
use crate::runtime_identity::AgentRuntimeIdentity;

const RECEIPT: &str = "/var/lib/vonk-forge/package-activation.receipt.json";

pub fn read_receipt(path: &Path) -> Result<Option<PackageActivationReceipt>, AgentUpgradeError> {
    let fd = match rustix::fs::open(path, OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW, Mode::empty()) {
        Ok(fd) => fd,
        Err(rustix::io::Errno::NOENT) => return Ok(None),
        Err(error) => return Err(std::io::Error::from(error).into()),
    };
    let mut file = File::from(fd);
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.uid() != 0 || metadata.nlink() != 1 || metadata.mode() & 0o777 != 0o644 || metadata.len() == 0 || metadata.len() > 16384 {
        return Err(AgentUpgradeError::GrantInvalid);
    }
    let mut raw = Vec::new();
    file.by_ref().take(16385).read_to_end(&mut raw)?;
    if raw.len() as u64 != metadata.len() { return Err(AgentUpgradeError::GrantInvalid); }
    let receipt: PackageActivationReceipt = parse_strict(&raw).map_err(|_| AgentUpgradeError::GrantInvalid)?;
    receipt.validate().map_err(|_| AgentUpgradeError::GrantInvalid)?;
    Ok(Some(receipt))
}

pub async fn acknowledge(client: &AgentHttpClient, identity: &AgentRuntimeIdentity) -> Result<Option<PackageActivationReceipt>, AgentUpgradeError> {
    let Some(receipt) = read_receipt(Path::new(RECEIPT))? else { return Ok(None); };
    if receipt.phase != PackageActivationPhase::Armed || receipt.candidate_binary_sha256 != identity.binary_digest || !identity.self_test_passed {
        return Ok(Some(receipt));
    }
    let grant = client.package_activation_grant(&receipt, identity).await?;
    let request_id = grant.claims.request_id.to_string();
    let body = canonical_json(&grant).map_err(|_| AgentUpgradeError::GrantInvalid)?;
    let response = tokio::task::spawn_blocking(move || call_helper(&body)).await.map_err(|_| AgentUpgradeError::HelperResponseInvalid)??;
    validate_helper_response(&response, &request_id, &receipt.candidate_package_sha256)?;
    if response.status != "package-activation-confirmed" { return Err(AgentUpgradeError::HelperResponseInvalid); }
    let acknowledged = read_receipt(Path::new(RECEIPT))?.ok_or(AgentUpgradeError::HelperResponseInvalid)?;
    if acknowledged.phase != PackageActivationPhase::Acknowledged || acknowledged.attempt_nonce != receipt.attempt_nonce || acknowledged.candidate_package_sha256 != receipt.candidate_package_sha256 {
        return Err(AgentUpgradeError::HelperResponseInvalid);
    }
    Ok(Some(acknowledged))
}
