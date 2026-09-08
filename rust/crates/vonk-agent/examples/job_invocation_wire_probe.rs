//! Exercise a real Controller job claim through production preparation.
use std::{
    fs,
    io::{self, Read},
};
use vonk_agent::{
    executor::{parse_compiled_execution_plan, prepare_job_invocation, recipe_job_input_manifest},
    oci::OciRuntime,
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
};
use vonk_agent_protocol::{AgentClaim, RecipeOperationRequest};

struct NoProcess;
impl ProcessRunner for NoProcess {
    fn run(
        &self,
        _: Program,
        _: &[String],
        _: std::time::Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        panic!("preparation must not execute a process")
    }
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let doc: serde_json::Value = serde_json::from_str(&input)?;
    let claim: AgentClaim = serde_json::from_value(doc["claim"].clone())?;
    let RecipeOperationRequest::JobRun(request) = RecipeOperationRequest::parse(&claim)? else {
        return Err("not a job".into());
    };
    let installed = parse_compiled_execution_plan(&doc["installed"])?;
    let invocation = prepare_job_invocation(&installed, &request)?;
    let data = tempfile::tempdir()?;
    let run_id = request.job_id.to_string();
    fs::create_dir_all(data.path().join("runs").join(&run_id).join("inputs"))?;
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
        huggingface_curl_config: None,
    };
    for file in &request.inputs {
        let bytes: Vec<u8> = serde_json::from_value(doc["input_contents"][&file.name].clone())?;
        let destination = runtime.job_input_destination(&run_id, &file.name)?;
        fs::write(destination, bytes)?;
    }
    let manifest = recipe_job_input_manifest(&request)?;
    runtime.write_job_input_manifest(
        &run_id,
        &request
            .inputs
            .iter()
            .map(|file| file.name.clone())
            .collect::<Vec<_>>(),
        &manifest,
        &request.input_manifest_sha256,
    )?;
    let manifest: serde_json::Value = serde_json::from_slice(&manifest)?;
    let plan = runtime.prepare_job_start(
        &installed,
        &request.installation_id.to_string(),
        &run_id,
        &invocation.runtime.placement,
        &invocation,
    )?;
    let retained: serde_json::Value = serde_json::from_slice(&fs::read(
        data.path()
            .join("run-metadata")
            .join(run_id)
            .join("runtime.json"),
    )?)?;
    println!(
        "{}",
        serde_json::json!({"arguments": plan.main, "runtime": retained, "input_manifest": manifest})
    );
    Ok(())
}
