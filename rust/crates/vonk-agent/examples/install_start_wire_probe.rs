#![forbid(unsafe_code)]

//! Stdin/stdout probe for the connected schema-2 recipe install/start wire.
//!
//! Each input line is a Controller AgentClaim.  The probe validates the claim,
//! invokes the production RecipeOperationRequest parser, projects the typed
//! compiled plan through the production OCI argument builder, and emits the
//! exact success body used by the production executor. Uninstall operations
//! are structural wire probes: they do not mutate the probe filesystem.

use std::{
    io::{self, BufRead, Write},
    path::Path,
};

use vonk_agent::{
    compiled_oci::CompiledOciPaths,
    executor::{
        recipe_install_success_body, recipe_start_success_body, recipe_stop_success_body,
        recipe_uninstall_success_body, runtime_arguments_for_plan,
    },
    oci::{RuntimeStartPlan, start_arguments_for_paths},
};
use vonk_agent_protocol::{
    AgentClaim, AgentResult, DistributionAssignment, RecipeOperationRequest, RecipeStartRequest,
};

const PROBE_DATA_ROOT: &str = "/var/lib/vonk-forge";

fn runtime_plan(
    request: &RecipeStartRequest,
    spec: &vonk_agent::workloads::CompiledExecutionPlan,
) -> Result<RuntimeStartPlan, String> {
    let data_root = Path::new(PROBE_DATA_ROOT);
    let run_id = request.run_id.to_string();
    let installation_id = request.installation_id.to_string();
    let run_root = data_root.join("runs").join(&run_id);
    let main = start_arguments_for_paths(
        spec,
        &CompiledOciPaths {
            image_archive: data_root
                .join("oci-archives")
                .join(&spec.runtime_image.oci_layout_sha256),
            model_root: data_root
                .join("installations")
                .join(&installation_id)
                .join("models"),
            input_root: spec.job.as_ref().map(|_| run_root.join("inputs")),
            output_root: run_root.join("outputs"),
            cache_root: data_root
                .join("installations")
                .join(&installation_id)
                .join("runtime-cache"),
            runtime_spec: data_root
                .join("run-metadata")
                .join(&run_id)
                .join("runtime.json"),
        },
        &run_id,
    )
    .map_err(|_| "compiled OCI projection is invalid".to_owned())?;
    Ok(RuntimeStartPlan {
        image_digest: spec.runtime_image.image_digest.clone(),
        registry_index_digest: spec
            .runtime_image
            .registry_manifest_digest
            .clone()
            .unwrap_or_else(|| spec.runtime_image.platform_manifest_digest.clone()),
        platform_manifest_digest: spec.runtime_image.platform_manifest_digest.clone(),
        archive_sha256: spec.runtime_image.oci_layout_sha256.clone(),
        image_reference: spec.runtime_image.local_image_reference(),
        pre_start: Vec::new(),
        main,
    })
}

fn result_for(claim: &AgentClaim) -> Result<AgentResult, String> {
    // Keep this explicit even though RecipeOperationRequest::parse validates
    // the claim too: the wire probe must exercise the authenticated claim
    // boundary before touching the operation payload.
    claim
        .validate()
        .map_err(|_| "agent claim is invalid".to_owned())?;
    let request = RecipeOperationRequest::parse(claim)
        .map_err(|_| "recipe operation payload is invalid".to_owned())?;
    let result = match request {
        RecipeOperationRequest::Install(request) => {
            request
                .compiled_execution_plan
                .validate()
                .map_err(|_| "compiled execution plan is invalid".to_owned())?;
            recipe_install_success_body(request.expected_bytes)
        }
        RecipeOperationRequest::Start(request) => {
            let spec = request.compiled_execution_plan.clone();
            spec.validate()
                .map_err(|_| "compiled execution plan is invalid".to_owned())?;
            let plan = runtime_plan(&request, &spec)?;
            let runtime_arguments = runtime_arguments_for_plan(&plan, &plan.main);
            recipe_start_success_body(
                &request,
                &spec,
                &spec.identity.model_artifact_set_sha256,
                &runtime_arguments,
            )
            .map_err(|_| "readiness evidence is unavailable".to_owned())?
        }
        RecipeOperationRequest::Stop(_request) => recipe_stop_success_body(),
        RecipeOperationRequest::Uninstall(_request) => recipe_uninstall_success_body(0),
        _ => return Err(
            "probe only accepts recipe.install, recipe.start, recipe.stop, and recipe.uninstall"
                .to_owned(),
        ),
    };
    let message = AgentResult {
        attempt: claim.attempt,
        deadline: claim.deadline,
        fence: claim.fence,
        job_id: claim.job_id,
        node_id: claim.node_id.clone(),
        operation_id: claim.operation_id,
        result: serde_json::from_value(result)
            .map_err(|_| "canonical agent result is invalid".to_owned())?,
        schema_version: 1,
        state: vonk_agent_protocol::generated::AgentResultState::Succeeded,
    };
    message
        .validate()
        .map_err(|_| "canonical agent result is invalid".to_owned())?;
    Ok(message)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let distribution_mode = std::env::args().nth(1).as_deref() == Some("--distribution");
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut output = io::BufWriter::new(stdout.lock());
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        if distribution_mode {
            let assignment: DistributionAssignment = serde_json::from_str(&line)?;
            assignment.validate()?;
            serde_json::to_writer(&mut output, &assignment)?;
        } else {
            let claim: AgentClaim = serde_json::from_str(&line)?;
            let result = result_for(&claim).map_err(io::Error::other)?;
            serde_json::to_writer(&mut output, &result)?;
        }
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}
