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
        huggingface_curl_config: None,
    };
    // Consume the actual Controller fixture without rewriting its image fields.
    let plan = compiled_plan();
    plan.validate().unwrap();
    runtime.verify_image(&plan).unwrap();
}

#[test]
fn image_policy_rejects_architecture_aliases_and_invalid_identity() {
    let data = tempdir().unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
        huggingface_curl_config: None,
    };
    for architecture in [
        "linux/arm64",
        "linux-amd64",
        "linux/amd64",
        "aarch64",
        "arm64",
    ] {
        let mut plan = compiled_plan();
        plan.runtime_image.architecture = architecture.into();
        assert!(runtime.verify_image(&plan).is_err(), "{architecture}");
    }
    for field in [
        "runtime_interface",
        "runtime_interface_label",
        "image_digest",
    ] {
        let mut plan = compiled_plan();
        match field {
            "runtime_interface" => plan.runtime_image.runtime_interface = "vonk.runtime.v2".into(),
            "runtime_interface_label" => plan.runtime_image.runtime_interface_label = "v2".into(),
            "image_digest" => plan.runtime.image_digest = format!("sha256:{}", "f".repeat(64)),
            _ => unreachable!(),
        }
        assert!(runtime.verify_image(&plan).is_err(), "{field}");
    }
}
