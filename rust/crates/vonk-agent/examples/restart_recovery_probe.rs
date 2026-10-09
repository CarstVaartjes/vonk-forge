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
use vonk_agent::{
    client::{AgentHttpClient, ClientError},
    config::AgentConfig,
    executor::{LoopClient, RecipeExecutor, distribution_success, run_once},
    identity::IdentityPaths,
    oci::OciRuntime,
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    runtime_identity::AgentRuntimeIdentity,
    state::{BeginDecision, StateStore},
};
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult,
    generated::{
        AgentClaimPayload, AgentExecutorProbeMode, AgentExecutorProbeRequest, AgentOperation,
    },
};

struct NoBuildProcess;

impl ProcessRunner for NoBuildProcess {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: std::time::Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        // Exact cleanup observes absent pre-existing units before source fetch.
        // Permit only that read; any build or runtime mutation still fails.
        if program == Program::Systemctl
            && arguments.get(1).map(String::as_str) == Some("list-units")
        {
            return Ok(ProcessOutput {
                success: true,
                stdout: Vec::new(),
                stderr: Vec::new(),
            });
        }
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
        _preflight_fingerprint: Option<&str>,
        _wait_seconds: u64,
        _runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        Ok(self.claim.lock().expect("claim lock").take())
    }

    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        Ok(AgentDirective {
            cancel_requested: false,
            deadline: (chrono::Utc::now() + chrono::Duration::seconds(30)).fixed_offset(),
            fence: progress.fence,
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

fn required<T>(value: Option<T>, name: &str) -> Result<T, Box<dyn std::error::Error>> {
    value.ok_or_else(|| format!("missing {name}").into())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let request: AgentExecutorProbeRequest = serde_json::from_str(&input)?;
    let data_root = PathBuf::from(&request.data_root);
    let claim = request.claim;
    claim.validate()?;
    let mut state = StateStore::open(&data_root.join("state.sqlite"), &request.node_id)?;
    if request.mode == AgentExecutorProbeMode::Recover {
        state.recover_interrupted()?;
        let result = state
            .pending_results()?
            .into_iter()
            .find(|(_, result)| result.fence == claim.fence)
            .ok_or("interrupted result was not retained")?
            .1;
        println!("{}", serde_json::to_string(&result)?);
        return Ok(());
    }
    if !matches!(
        request.mode,
        AgentExecutorProbeMode::ExecuteDistribution
            | AgentExecutorProbeMode::ExecuteBuild
            | AgentExecutorProbeMode::ExecuteUninstall
    ) {
        return Err("unsupported probe mode".into());
    }
    if request.mode == AgentExecutorProbeMode::ExecuteDistribution {
        let decision = state.begin(&claim, chrono::Utc::now())?;
        if let BeginDecision::Replay(result) = decision {
            println!("{}", serde_json::to_string(&result)?);
            return Ok(());
        }
    }
    let ca_path = PathBuf::from(required(request.ca_pem, "ca_pem")?);
    let controller_url: url::Url = required(request.controller_url, "controller_url")?.parse()?;
    let config = AgentConfig {
        enrollment_url: controller_url.clone(),
        controller_url,
        ca_path,
        ca_sha256: required(request.ca_sha256, "ca_sha256")?,
        data_dir: data_root.clone(),
        node_id: request.node_id.clone(),
        fabric_address: None,
        fabric_bandwidth_mbps: None,
    };
    let identity = IdentityPaths {
        certificate: required(request.certificate_pem, "certificate_pem")?.into(),
        chain: required(request.chain_pem, "chain_pem")?.into(),
        private_key: required(request.private_key_pem, "private_key_pem")?.into(),
    };
    let client = AgentHttpClient::from_identity_paths(&config, &identity)?;
    if matches!(
        request.mode,
        AgentExecutorProbeMode::ExecuteBuild | AgentExecutorProbeMode::ExecuteUninstall
    ) {
        if claim.operation != AgentOperation::RecipeBuildV1
            && claim.operation != AgentOperation::RecipeUninstall
        {
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
                data_root: &data_root,
            },
            runtime_root: &data_root,
        };
        run_once(&loop_client, &mut state, &executor, None, 0, None).await?;
        let results = loop_client.results.lock().expect("result lock");
        // Reconciliation can redeliver older attempts alongside this claim.
        // Observe the exact requested fence, allowing identical redelivery only.
        let result = results
            .iter()
            .find(|result| result.fence == claim.fence)
            .ok_or("build probe must produce the requested result")?;
        if results
            .iter()
            .any(|item| item.fence == claim.fence && item != result)
        {
            return Err("build probe must preserve one exact outcome across redelivery".into());
        }
        println!("{}", serde_json::to_string(result)?);
        return Ok(());
    }
    let AgentClaimPayload::ArtifactDistributionPayload(payload) = &claim.payload else {
        return Err("claim is not artifact distribution".into());
    };
    let model_store = data_root.join("distribution/models");
    let distribution_store = model_store.parent().ok_or("model store parent is absent")?;
    let evidence = client
        .download_distribution(&payload.plan_digest, distribution_store)
        .await?;
    let result = state.finish(&claim, distribution_success(evidence))?;
    println!("{}", serde_json::to_string(&result)?);
    Ok(())
}
