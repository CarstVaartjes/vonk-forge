#![forbid(unsafe_code)]

//! Exercise the agent's persistent claim journal and real HTTPS distribution
//! client in separate processes for Controller restart recovery acceptance.

use std::{
    io::{self, Read},
    path::PathBuf,
};

use serde::Deserialize;
use vonk_agent::{
    client::AgentHttpClient,
    config::AgentConfig,
    executor::distribution_success_evidence,
    identity::IdentityPaths,
    state::{BeginDecision, StateStore},
};
use vonk_agent_protocol::{AgentClaim, generated::AgentClaimPayload};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    mode: String,
    data_root: PathBuf,
    claim: AgentClaim,
    controller_url: Option<String>,
    ca_sha256: Option<String>,
    ca_pem: Option<PathBuf>,
    certificate_pem: Option<PathBuf>,
    chain_pem: Option<PathBuf>,
    private_key_pem: Option<PathBuf>,
}

fn required<T>(value: Option<T>, name: &str) -> Result<T, Box<dyn std::error::Error>> {
    value.ok_or_else(|| format!("missing {name}").into())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let request: Request = serde_json::from_str(&input)?;
    let claim = request.claim;
    claim.validate()?;
    let mut state = StateStore::open(&request.data_root.join("state.sqlite"), &claim.node_id)?;
    if request.mode == "recover" {
        state.recover_interrupted()?;
        let result = state
            .pending_results()?
            .into_iter()
            .find(|(_, result)| result.operation_id == claim.operation_id)
            .ok_or("interrupted result was not retained")?
            .1;
        println!("{}", serde_json::to_string(&result)?);
        return Ok(());
    }
    if request.mode != "execute-distribution" {
        return Err("unsupported probe mode".into());
    }
    let decision = state.begin(&claim, chrono::Utc::now())?;
    if let BeginDecision::Replay(result) = decision {
        println!("{}", serde_json::to_string(&result)?);
        return Ok(());
    }
    let AgentClaimPayload::ArtifactDistributionPayload(payload) = &claim.payload else {
        return Err("claim is not artifact distribution".into());
    };
    let ca_path = required(request.ca_pem, "ca_pem")?;
    let controller_url: url::Url = required(request.controller_url, "controller_url")?.parse()?;
    let config = AgentConfig {
        enrollment_url: controller_url.clone(),
        controller_url,
        ca_path,
        ca_sha256: required(request.ca_sha256, "ca_sha256")?,
        data_dir: request.data_root.clone(),
        node_id: claim.node_id.clone(),
        poll_min_seconds: 1,
        poll_max_seconds: 2,
        fabric_address: None,
        fabric_bandwidth_mbps: None,
        huggingface_curl_config: None,
    };
    let identity = IdentityPaths {
        certificate: required(request.certificate_pem, "certificate_pem")?,
        chain: required(request.chain_pem, "chain_pem")?,
        private_key: required(request.private_key_pem, "private_key_pem")?,
    };
    let client = AgentHttpClient::from_identity_paths(&config, &identity)?;
    let evidence = client
        .download_distribution(
            &payload.plan_digest,
            &request.data_root.join("distribution"),
            &request.data_root.join("oci-archives"),
        )
        .await?;
    let result = state.finish(&claim, "succeeded", distribution_success_evidence(evidence))?;
    println!("{}", serde_json::to_string(&result)?);
    Ok(())
}
