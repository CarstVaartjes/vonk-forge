#![forbid(unsafe_code)]

use std::time::Duration;
use tempfile::tempdir;
use vonk_agent::{
    oci::OciRuntime,
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    workloads::CompiledExecutionPlan,
};

struct NoProcess;
impl ProcessRunner for NoProcess {
    fn run(&self, _: Program, _: &[String], _: Duration) -> Result<ProcessOutput, ProcessError> {
        panic!("compiled image policy validation must not launch a process");
    }
}

fn compiled_plan() -> CompiledExecutionPlan {
    serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap()
}

#[test]
fn compiled_controller_image_matches_the_oci_platform_policy() {
    let data = tempdir().unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
    };
    // Consume the actual Controller fixture without rewriting its image fields.
    let plan = compiled_plan();
    plan.validate().unwrap();
    runtime.verify_image(&plan).unwrap();
}

#[test]
fn image_policy_rejects_a_different_runtime_interface_label() {
    let data = tempdir().unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
    };
    let mut plan = compiled_plan();
    plan.runtime_image.runtime_interface_label = "v2".into();
    assert!(runtime.verify_image(&plan).is_err());
}
