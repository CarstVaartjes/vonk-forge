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
    executor::{recipe_install_success, recipe_start_success, runtime_arguments_for_plan},
    oci::{RuntimeStartPlan, start_arguments_for_paths},
    outcome::ExecutionResult,
};
use vonk_agent_protocol::{
    AgentClaim, AgentResult, DistributionAssignment, RecipeOperationRequest, RecipeStartRequest,
    compiled_oci::CompiledOciPaths,
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
        registry_index_digest: spec.runtime_image.image_digest.clone(),
        platform_manifest_digest: spec.runtime_image.image_digest.clone(),
        archive_sha256: spec.runtime_image.oci_layout_sha256.clone(),
        image_reference: spec.runtime_image.local_image_reference(),
        main,
    })
}

fn result_for(claim: &AgentClaim) -> Result<AgentResult, String> {
    // The production parser validates this exact claim before touching its
    // payload. Exercise that boundary once, as the normal worker does.
    let request = RecipeOperationRequest::parse(claim)
        .map_err(|_| "recipe operation payload is invalid".to_owned())?;
    let result = match request {
        RecipeOperationRequest::Install(request) => {
            request
                .compiled_execution_plan
                .validate()
                .map_err(|_| "compiled execution plan is invalid".to_owned())?;
            recipe_install_success(request.expected_bytes)
        }
        RecipeOperationRequest::Start(request) => {
            let spec = request.compiled_execution_plan.clone();
            // The production OCI projection validates this same immutable spec
            // before building arguments; do not repeat its full document and
            // per-artifact validation immediately before that boundary.
            let plan = runtime_plan(&request, &spec)?;
            let _ = runtime_arguments_for_plan(&plan, &plan.main);
            recipe_start_success(&request)
        }
        RecipeOperationRequest::Stop(_request) => {
            ExecutionResult::done(vonk_agent_protocol::generated::RecipeStopResult::default())
        }
        RecipeOperationRequest::Uninstall(_request) => {
            ExecutionResult::done(vonk_agent_protocol::generated::RecipeUninstallResult::default())
        }
        _ => return Err(
            "probe only accepts recipe.install, recipe.start, recipe.stop, and recipe.uninstall"
                .to_owned(),
        ),
    };
    let finished = result.finish(claim);
    let message = AgentResult {
        fence: claim.fence,
        result: finished.result,
        state: finished.state,
    };
    message
        .validate()
        .map_err(|_| "canonical agent result is invalid".to_owned())?;
    Ok(message)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    let distribution_mode = arguments.iter().any(|value| value == "--distribution");
    // Constructing the typed claim validators dominates one probe run (~100ms,
    // against ~4ms for each further claim in the same process), so the wire
    // tests ask many independent questions per process and read one verdict per
    // line.  Every claim still crosses the production parser.
    let verdicts_mode = arguments.iter().any(|value| value == "--verdicts");
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut output = io::BufWriter::new(stdout.lock());
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        if verdicts_mode {
            let verdict = match serde_json::from_str::<AgentClaim>(&line) {
                Ok(claim) => result_for(&claim).is_ok(),
                Err(_) => false,
            };
            writeln!(output, "{}", u8::from(verdict))?;
            output.flush()?;
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
