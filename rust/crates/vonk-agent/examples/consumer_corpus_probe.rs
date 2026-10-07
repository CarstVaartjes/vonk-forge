#![forbid(unsafe_code)]
//! Actual generated wire parsers and durable agent result writer/restart reader.
use std::{
    io::{self, Read},
    path::PathBuf,
};
use vonk_agent::{
    client::{AgentHttpClient, ClientError},
    config::AgentConfig,
    identity::IdentityPaths,
    outcome::ExecutionResult,
    state::{BeginDecision, StateStore},
};

use vonk_agent_protocol::generated::{
    BoundedErrorResponse, FailureDiagnostics, RequestValidationProblem,
};
use vonk_agent_protocol::{AgentClaim, AgentResult, canonical_json, parse_strict};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut raw = Vec::new();
    io::stdin().read_to_end(&mut raw)?;
    let args: Vec<String> = std::env::args().skip(1).collect();
    let bytes = match args.first().map(String::as_str) {
        Some("FailureDiagnostics") => {
            let value: FailureDiagnostics = parse_strict(&raw)?;
            value.validate()?;
            canonical_json(&value)?
        }
        Some("AgentResult") => {
            let value: AgentResult = parse_strict(&raw)?;
            value.validate()?;
            canonical_json(&value)?
        }
        Some("OperationProgress") => {
            let value: vonk_agent_protocol::OperationProgress = parse_strict(&raw)?;
            value.validate()?;
            canonical_json(&value)?
        }
        Some("AgentProgress") => {
            let value: vonk_agent_protocol::AgentProgress = parse_strict(&raw)?;
            value.validate()?;
            canonical_json(&value)?
        }
        Some("RecipeStartPayload") => canonical_json(&parse_strict::<
            vonk_agent_protocol::generated::RecipeStartPayload,
        >(&raw)?)?,
        Some("RecipeBuildEnvironmentArgument") => canonical_json(&parse_strict::<
            vonk_agent_protocol::generated::RecipeBuildEnvironmentArgument,
        >(&raw)?)?,
        Some("BoundedErrorResponse") => {
            canonical_json(&parse_strict::<BoundedErrorResponse>(&raw)?)?
        }
        Some("RequestValidationProblem") => {
            canonical_json(&parse_strict::<RequestValidationProblem>(&raw)?)?
        }
        Some("http" | "heartbeat") => {
            if args.len() != 5 {
                return Err(
                    "expected mode, configuration, certificate, chain and private-key paths".into(),
                );
            }
            let config = AgentConfig::load(std::path::Path::new(&args[1]))?;
            let client = AgentHttpClient::from_identity_paths(
                &config,
                &IdentityPaths {
                    certificate: PathBuf::from(&args[2]),
                    chain: PathBuf::from(&args[3]),
                    private_key: PathBuf::from(&args[4]),
                },
            )?;
            if args.first().map(String::as_str) == Some("heartbeat") {
                let progress: vonk_agent_protocol::AgentProgress = parse_strict(&raw)?;
                progress.validate()?;
                let directive = client.heartbeat(&progress).await?;
                println!("{}", String::from_utf8(canonical_json(&directive)?)?);
                return Ok(());
            }
            let result: AgentResult = parse_strict(&raw)?;
            result.validate()?;
            let error = client
                .submit_result(&result)
                .await
                .expect_err("peer must refuse result");
            let (status, summary, decision) = match error {
                ClientError::ResultRejected(error) | ClientError::Controller(error) => {
                    (error.status, error.summary, error.decision)
                }
                other => return Err(format!("wrong boundary failure: {other}").into()),
            };
            serde_json::to_vec(
                &serde_json::json!({"status": status, "summary": summary, "decision": decision}),
            )?
        }
        Some("state") => {
            let claim: AgentClaim = parse_strict(&raw)?;
            let path = PathBuf::from(args.get(1).ok_or("missing state path")?);
            // The caller issues a fresh live claim; each real state admission
            // observes wall time, including admission after reopening the store.
            let now = chrono::Utc::now();
            let mut state = StateStore::open(&path, "spk_11111111111111111111111111111111")?;
            assert!(matches!(state.begin(&claim, now)?, BeginDecision::Execute));
            let produced = state.finish(
                &claim,
                ExecutionResult::failed("consumer corpus stop failure"),
            )?;
            drop(state);
            let mut restarted = StateStore::open(&path, "spk_11111111111111111111111111111111")?;
            let pending = restarted.pending_results()?;
            assert_eq!(pending.len(), 1);
            assert_eq!(canonical_json(&pending[0].1)?, canonical_json(&produced)?);
            let BeginDecision::Replay(replayed) = restarted.begin(&claim, chrono::Utc::now())?
            else {
                return Err("restart did not replay".into());
            };
            assert_eq!(canonical_json(&*replayed)?, canonical_json(&produced)?);
            canonical_json(&*replayed)?
        }
        _ => return Err("unknown canonical consumer".into()),
    };
    println!("{}", String::from_utf8(bytes)?);
    Ok(())
}
