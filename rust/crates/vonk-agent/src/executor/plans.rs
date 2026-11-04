//! Plans.

use super::*;

/// Parse and validate one compiled execution plan document.
pub fn parse_compiled_execution_plan(document: &[u8]) -> Result<CompiledExecutionPlan, OciError> {
    let plan: CompiledExecutionPlan = serde_json::from_slice(document)?;
    plan.validate()?;
    Ok(plan)
}

pub(super) fn exact_stop_plan_from_claim(
    claim: &AgentClaim,
    expected_run_id: &str,
    cancel_pending_start: bool,
) -> Option<RecipeStopRequest> {
    let (
        run_id,
        target_runtime_id,
        run_generation,
        installation_id,
        recipe_revision_id,
        mapping_id,
        plan_digest,
        compiled_execution_plan,
    ) = match &claim.payload {
        vonk_agent_protocol::generated::AgentClaimPayload::RecipeStartPayload(start) => (
            start.run_id,
            start.run_id,
            start.run_generation,
            start.installation_id,
            start.recipe_revision_id,
            start.mapping_id,
            start.plan_digest.clone(),
            start.compiled_execution_plan.clone(),
        ),
        vonk_agent_protocol::generated::AgentClaimPayload::RecipeJobRunRequest(job) => (
            job.run_id,
            job.job_id,
            job.run_generation,
            job.installation_id,
            job.recipe_revision_id,
            job.mapping_id,
            job.plan_digest.clone(),
            job.compiled_execution_plan.clone(),
        ),
        _ => return None,
    };
    if run_id.to_string() != expected_run_id {
        return None;
    }
    Some(RecipeStopRequest {
        cancel_pending_start,
        rank: compiled_execution_plan.runtime.placement.rank.clone(),
        role: compiled_execution_plan.runtime.placement.role.clone(),
        recipe_content_sha256: compiled_execution_plan
            .identity
            .recipe_revision_sha256
            .clone(),
        stop_timeout_seconds: compiled_execution_plan.lifecycle.stop_timeout_seconds,
        installation_id,
        mapping_id,
        plan_digest,
        recipe_revision_id,
        run_generation,
        run_id,
        target_runtime_id,
    })
}

/// Seconds a container no claim owns gets to stop before it is killed.
const ORPHAN_STOP_TIMEOUT_SECONDS: u16 = 10;

/// The stop document of a container no claim owns, built from its own managed
/// labels and nothing else.
///
/// The labels carry the whole identity the helper checks (run, target,
/// installation, generation). The document's other required fields are
/// provenance the labels do not carry and the helper never compares, so they
/// repeat the identity the container does carry rather than invent a new one.
pub(super) fn orphan_stop_plan(
    labels: &vonk_agent_protocol::generated::HostHelperContainerLabels,
) -> RecipeStopRequest {
    RecipeStopRequest {
        cancel_pending_start: false,
        rank: 0_u64.into(),
        role: "orphan".to_owned(),
        recipe_content_sha256: labels.plan_digest.clone(),
        stop_timeout_seconds: ORPHAN_STOP_TIMEOUT_SECONDS.into(),
        installation_id: labels.installation_id,
        mapping_id: labels.run_id,
        plan_digest: labels.plan_digest.clone(),
        recipe_revision_id: labels.run_id,
        run_generation: labels.run_generation,
        run_id: labels.run_id,
        target_runtime_id: labels.target_id,
    }
}

/// Build the exact runtime argument vector used for start and inspection.
pub fn runtime_arguments_for_plan(
    plan: &crate::oci::RuntimeStartPlan,
    command: &[String],
) -> Vec<String> {
    let mut arguments = vec![
        plan.archive_sha256.clone(),
        plan.registry_index_digest.clone(),
        plan.platform_manifest_digest.clone(),
        plan.image_reference.clone(),
    ];
    arguments.extend(command.iter().cloned());
    arguments
}

pub fn runtime_arguments_digest(arguments: &[String]) -> Result<String, ProtocolError> {
    canonical_json(&arguments.to_vec()).map(|value| hex_sha256(&value))
}

#[cfg(test)]
mod tests;
