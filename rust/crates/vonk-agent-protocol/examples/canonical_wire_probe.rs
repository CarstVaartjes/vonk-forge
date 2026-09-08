//! Connected Python/Rust canonical model and digest probe.
use std::io::{self, Read, Write};
use vonk_agent_protocol::{
    AgentClaim, HostRuntimeRequest, OperationProgress, RecipeOperationRequest,
    SignedHostHelperGrant, canonical_generated_json, generated, hex_sha256,
};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let model = std::env::args()
        .nth(1)
        .ok_or("model argument is required")?;
    let mut input = Vec::new();
    io::stdin().read_to_end(&mut input)?;
    let output = match model.as_str() {
        "AgentClaim" => {
            let value: AgentClaim = serde_json::from_slice(&input)?;
            value.validate()?;
            if value.operation.starts_with("recipe.") {
                RecipeOperationRequest::parse(&value)?;
            }
            canonical_generated_json(&value)?
        }
        "SignedHostHelperGrant" => {
            let value: SignedHostHelperGrant = serde_json::from_slice(&input)?;
            value.validate()?;
            canonical_generated_json(&value)?
        }
        "OperationProgress" => {
            let value: OperationProgress = serde_json::from_slice(&input)?;
            value.validate()?;
            canonical_generated_json(&value)?
        }
        "HostRuntimeRequest" => {
            let value: HostRuntimeRequest = serde_json::from_slice(&input)?;
            value.validate()?;
            canonical_generated_json(&value)?
        }
        "RecipeBuildRequest" | "RecipeJobRunRequest" => {
            let payload: generated::AgentClaimPayload = serde_json::from_slice(&input)?;
            let bytes = canonical_generated_json(&payload)?;
            let operation = if model == "RecipeBuildRequest" {
                "recipe.build.v1"
            } else {
                "recipe.job.run.v1"
            };
            let claim = AgentClaim {
                attempt: 1,
                authority_revision: "a".repeat(64),
                deadline: "2099-01-01T00:00:00+00:00".parse()?,
                fence: uuid::Uuid::new_v4(),
                job_id: uuid::Uuid::new_v4(),
                node_id: format!("spk_{}", "1".repeat(32)),
                operation: operation.parse()?,
                operation_id: uuid::Uuid::new_v4(),
                payload_digest: hex_sha256(&bytes),
                payload,
                schema_version: 1,
            };
            RecipeOperationRequest::parse(&claim)?;
            bytes
        }
        _ => return Err("unsupported canonical model".into()),
    };
    io::stdout().write_all(&output)?;
    Ok(())
}
