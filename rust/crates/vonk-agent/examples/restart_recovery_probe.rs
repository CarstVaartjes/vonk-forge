#![forbid(unsafe_code)]

//! Exercise the agent's persistent claim journal and real HTTPS distribution
//! client in separate processes for Controller restart recovery acceptance.
//! `execute-build` runs the production build executor and result normalizer
//! against a real HTTPS source response; it must never launch a build process.

use std::{
    io::{self, Read},
    path::PathBuf,
    sync::{Arc, Mutex},
};

use async_trait::async_trait;
use serde::Deserialize;
use vonk_agent::{
    client::{AgentHttpClient, ClientError},
    config::AgentConfig,
    executor::{LoopClient, RecipeExecutor, distribution_success_evidence, run_once},
    identity::IdentityPaths,
    oci::OciRuntime,
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    runtime_identity::AgentRuntimeIdentity,
    state::{BeginDecision, StateStore},
};
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, generated::AgentClaimPayload,
};

struct NoBuildProcess;

impl ProcessRunner for NoBuildProcess {
    fn run(
        &self,
        _program: Program,
        _arguments: &[String],
        _timeout: std::time::Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        panic!("source failure must be reported before any build process starts")
    }
}

// Only lease delivery/result capture is local. Source fetching, execution,
// normalization and durable result serialization use production owners.
#[derive(Clone)]
struct BuildLoop {
    claim: Arc<Mutex<Option<AgentClaim>>>,
    results: Arc<Mutex<Vec<AgentResult>>>,
}

#[async_trait]
impl LoopClient for BuildLoop {
    async fn claim(
        &self,
        _capabilities: &[&str],
        _wait_seconds: u64,
        _runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        Ok(self.claim.lock().expect("claim lock").take())
    }

    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        Ok(AgentDirective {
            attempt: progress.attempt,
            cancel_requested: false,
            deadline: progress.deadline,
            fence: progress.fence,
            job_id: progress.job_id,
            node_id: progress.node_id.clone(),
            operation_id: progress.operation_id,
            schema_version: progress.schema_version,
        })
    }

    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.results
            .lock()
            .expect("result lock")
            .push(result.clone());
        Ok(())
    }
}

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
    if !matches!(
        request.mode.as_str(),
        "execute-distribution" | "execute-build"
    ) {
        return Err("unsupported probe mode".into());
    }
    if request.mode == "execute-distribution" {
        let decision = state.begin(&claim, chrono::Utc::now())?;
        if let BeginDecision::Replay(result) = decision {
            println!("{}", serde_json::to_string(&result)?);
            return Ok(());
        }
    }
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
    if request.mode == "execute-build" {
        if claim.operation != "recipe.build.v1" {
            return Err("claim is not recipe build".into());
        }
        let loop_client = BuildLoop {
            claim: Arc::new(Mutex::new(Some(claim.clone()))),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let runner = NoBuildProcess;
        let executor = RecipeExecutor {
            client: &client,
            runtime: OciRuntime {
                runner: &runner,
                data_root: &request.data_root,
                huggingface_curl_config: None,
            },
            runtime_root: &request.data_root,
            observation_receipt_public_key: [0; 32],
        };
        run_once(
            &loop_client,
            &mut state,
            &executor,
            &["recipe.build.v1"],
            0,
            None,
        )
        .await?;
        let results = loop_client.results.lock().expect("result lock");
        if results.len() != 1 {
            return Err("build probe must produce exactly one result".into());
        }
        println!("{}", serde_json::to_string(&results[0])?);
        return Ok(());
    }
    let AgentClaimPayload::ArtifactDistributionPayload(payload) = &claim.payload else {
        return Err("claim is not artifact distribution".into());
    };
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
