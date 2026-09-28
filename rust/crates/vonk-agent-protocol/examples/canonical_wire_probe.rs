//! Connected Python/Rust canonical model and digest probe.
use std::io::{self, Read, Write};
use vonk_agent_protocol::{
    AgentClaim, HostRuntimeRequest, OperationProgress, RecipeOperationRequest,
    SignedHostHelperGrant, canonical_generated_json, generated,
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
        "InventoryRequest" => {
            let value: vonk_agent_protocol::InventoryRequest = serde_json::from_slice(&input)?;
            value.validate()?;
            canonical_generated_json(&value)?
        }
        "DistributionAssignment" => {
            let value: vonk_agent_protocol::DistributionAssignment =
                serde_json::from_slice(&input)?;
            value.validate()?;
            canonical_generated_json(&value)?
        }
        "RecipeRunObservationsWire" => {
            let value: vonk_agent_protocol::RecipeRunObservationsWire =
                serde_json::from_slice(&input)?;
            for run in &value.runs {
                run.validate()?;
            }
            canonical_generated_json(&value)?
        }
        "RecipeStartPayload" => {
            let value: generated::RecipeStartPayload = serde_json::from_slice(&input)?;
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
                deadline: "2099-01-01T00:00:00+00:00".parse()?,
                fence: uuid::Uuid::new_v4(),
                operation: operation.parse()?,
                payload,
            };
            RecipeOperationRequest::parse(&claim)?;
            bytes
        }
        _ => return Err("unsupported canonical model".into()),
    };
    io::stdout().write_all(&output)?;
    Ok(())
}
