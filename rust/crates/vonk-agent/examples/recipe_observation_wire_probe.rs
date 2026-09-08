#![forbid(unsafe_code)]

//! Linux production wire probe. `persist-binding` exercises OCI start
//! persistence and retained inspection planning; `serialize` uses the exact
//! client envelope factory used by AgentHttpClient.

use std::fs;
use std::io::{self, BufRead};

use chrono::{DateTime, Utc};
use serde::Deserialize;
use vonk_agent::client::{ExactRecipeRunObservation, build_exact_recipe_run_observations};
use vonk_agent::executor::{recipe_start_success_body, runtime_arguments_for_plan};
use vonk_agent::oci::{OciRuntime, RecipeRunStartIdentity};
use vonk_agent::process::{ProcessError, ProcessOutput, ProcessRunner, Program};
use vonk_agent::workloads::{CompiledExecutionPlan, Placement};
use vonk_agent_protocol::{RecipeStartRequest, parse_strict};

struct NoProcess;

impl ProcessRunner for NoProcess {
    fn run(
        &self,
        _program: Program,
        _arguments: &[String],
        _timeout: std::time::Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        Ok(ProcessOutput {
            success: true,
            stdout: Vec::new(),
            stderr: Vec::new(),
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PersistBindingInput {
    request: RecipeStartRequest,
    artifact_set_digest: String,
    data_root: std::path::PathBuf,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SerializeInput {
    node_id: String,
    observed_at: DateTime<Utc>,
    runs: Vec<ExactRecipeRunObservation>,
}

fn persist_binding(
    input: PersistBindingInput,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let spec: CompiledExecutionPlan =
        serde_json::from_value(input.request.compiled_execution_plan.clone())?;
    let placement = Placement {
        endpoint_address: spec.runtime.placement.endpoint_address,
        rank: input.request.rank,
        role: input.request.role.clone(),
        world_size: input.request.world_size,
        local_address: input.request.local_address,
        master_address: input.request.master_address,
        master_port: input.request.master_port,
        port: Some(input.request.port),
        reserved_memory_bytes: input.request.reserved_memory_bytes,
    };
    let run_generation = input
        .request
        .run_generation
        .ok_or("run generation is required")?;
    fs::create_dir_all(&input.data_root)?;
    let installation = input
        .data_root
        .join("installations")
        .join(input.request.installation_id.to_string());
    fs::create_dir_all(&installation)?;
    fs::write(installation.join("spec.json"), serde_json::to_vec(&spec)?)?;
    fs::write(
        installation.join("recipe-content.sha256"),
        &input.request.recipe_content_sha256,
    )?;
    let identity = RecipeRunStartIdentity {
        mapping_generation: input.request.mapping_generation,
        mapping_id: input.request.mapping_id,
        recipe_content_sha256: input.request.recipe_content_sha256.clone(),
        recipe_revision_id: input.request.recipe_revision_id,
        run_generation,
    };
    let runner = NoProcess;
    let runtime = OciRuntime {
        runner: &runner,
        data_root: &input.data_root,
        huggingface_curl_config: None,
    };
    let start_plan = if matches!(
        input.request.phase,
        Some(vonk_agent_protocol::RecipeStartPhase::CollectiveReadiness)
    ) {
        runtime.prepare_retained_start_with_inspection_identity(
            &spec,
            &input.request.installation_id.to_string(),
            &input.request.run_id.to_string(),
            &placement,
            &identity,
        )?
    } else {
        runtime.prepare_start_with_inspection_identity(
            &spec,
            &input.request.installation_id.to_string(),
            &input.request.run_id.to_string(),
            &placement,
            &identity,
        )?
    };
    let runtime_arguments = runtime_arguments_for_plan(&start_plan, &start_plan.main);
    let evidence = recipe_start_success_body(
        &input.request,
        &spec,
        &input.artifact_set_digest,
        &runtime_arguments,
    )?;
    let binding = runtime
        .recipe_run_inspection_plans()?
        .into_iter()
        .find(|plan| plan.binding.run_id == input.request.run_id)
        .map(|plan| plan.binding)
        .ok_or_else(|| "persisted observation binding was not planned".to_owned())?;
    Ok(serde_json::json!({"binding": binding, "evidence": evidence}))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mode = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "parse".to_owned());
    for line in io::stdin().lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        match mode.as_str() {
            "persist-binding" => println!(
                "{}",
                serde_json::to_string(&persist_binding(serde_json::from_str(&line)?)?)?
            ),
            "serialize" => {
                let input: SerializeInput = serde_json::from_str(&line)?;
                let envelope = build_exact_recipe_run_observations(
                    &input.node_id,
                    input.observed_at,
                    &input.runs,
                )?;
                println!("{}", serde_json::to_string(&envelope)?);
            }
            "parse" => {
                let observation = parse_strict::<ExactRecipeRunObservation>(line.as_bytes())?;
                observation.validate()?;
                println!("{}", serde_json::to_string(&observation)?);
            }
            _ => return Err("unknown recipe observation probe mode".into()),
        }
    }
    Ok(())
}
