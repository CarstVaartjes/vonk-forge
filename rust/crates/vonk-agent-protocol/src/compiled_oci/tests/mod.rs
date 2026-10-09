#![cfg(test)]

use super::{
    CompiledOciError, CompiledOciPaths, ExecInvocationLimits, OciNetworkMode,
    OciMount, measure_exec_invocation, project, start_arguments_for_paths,
};
use crate::compiled_execution_plan::{CompiledEnvironmentEntry, CompiledExecutionPlan};
use serde_json::{Value, json};
use std::path::PathBuf;

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap()
}

fn paths() -> CompiledOciPaths {
    CompiledOciPaths {
        model_root: PathBuf::from("/run/vonk/models"),
        input_root: None,
        output_root: PathBuf::from("/run/vonk/outputs"),
        cache_root: PathBuf::from("/run/vonk/cache"),
        runtime_spec: PathBuf::from("/run/vonk/runtime.json"),
    }
}

fn helper_environment() -> [(&'static str, &'static str); 3] {
    [
        ("LANG", "C.UTF-8"),
        ("LC_ALL", "C.UTF-8"),
        (
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ),
    ]
}

mod accounting;
mod projection;
mod topology;
