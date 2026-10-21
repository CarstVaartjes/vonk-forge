#![cfg(test)]
//! The agent produces each shared vector the Controller's ingress tests read.
//!
//! Each test builds the message the way production does and compares its wire
//! form with the checked-in vector; clock- and host-dependent diagnostics
//! (collection time, versions, sandbox and storage facts) are compared by shape.

use super::dispatch::upgrade_outcome;
use super::observations::exited_workload_diagnostics;
use super::*;
use crate::agent_upgrade::AgentUpgradeError;
use crate::client::{ExactRecipeRunObservation, build_exact_recipe_run_observations};
use serde_json::Value;

const VECTORS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../agent_protocol/src/vonk_agent_protocol/vectors/"
);

/// Host- and clock-dependent members of a diagnostics document.
const VOLATILE: [&str; 5] = [
    "collected_at",
    "collector_errors",
    "sandbox",
    "storage",
    "versions",
];

fn without_volatile(value: &mut Value) {
    match value {
        Value::Array(items) => items.iter_mut().for_each(without_volatile),
        Value::Object(members) => {
            if members.contains_key("schema_version") && members.contains_key("phase") {
                for name in VOLATILE {
                    members.remove(name);
                }
            }
            members.values_mut().for_each(without_volatile);
        }
        _ => {}
    }
}

/// Assert `produced` is the vector `name`.
fn assert_is_vector(name: &str, mut produced: Value) {
    let path = format!("{VECTORS}{name}");
    let mut vector: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    without_volatile(&mut produced);
    without_volatile(&mut vector);
    assert_eq!(produced, vector, "{name}");
}

fn sent(fence: uuid::Uuid, finished: crate::outcome::Finished) -> Value {
    serde_json::to_value(crate::wire_prose::wire_result(&AgentResult {
        fence,
        result: finished.result,
        state: finished.state,
    }))
    .unwrap()
}

const FENCE: &str = "00000000-0000-4000-8000-000000000031";

#[test]
fn an_installed_upgrade_awaiting_its_identity_is_the_shared_vector() {
    let finished = upgrade_outcome(Ok(())).finish_for(&AgentOperation::AgentUpgradeV1);
    assert_is_vector(
        "agent-upgrade-awaiting-identity-v1.json",
        sent(uuid::Uuid::parse_str(FENCE).unwrap(), finished),
    );
}

#[test]
fn a_refused_upgrade_package_is_the_shared_vector() {
    // The helper's own words name the package path; they cross only as the
    // captured detail and a redacted reason.
    let refused = AgentUpgradeError::HelperRejectedWithCode {
        code: HelperErrorCode::PackageVerificationFailed,
        exit_code: Some(1),
        diagnostic: Some(
            "signature of /var/lib/vonk/incoming/vonk-agent_0.1.2_arm64.deb does not verify"
                .to_owned(),
        ),
    };
    let finished = upgrade_outcome(Err(refused)).finish_for(&AgentOperation::AgentUpgradeV1);
    assert_is_vector(
        "agent-upgrade-refused-v1.json",
        sent(uuid::Uuid::parse_str(FENCE).unwrap(), finished),
    );
}

#[test]
fn an_inspected_exited_workload_is_the_shared_observation_vector() {
    let summary = "exit_code=1 oom_killed=false exit_cause=python_exception exception=\"FileNotFoundError: [Errno 2] No such file or directory: '/opt/vonk/prewarm/sglang/flashinfer/autotune/fused_moe.json'\"";
    let exited = crate::host_runtime::HostRuntimeError::HelperRejected {
        code: HelperErrorCode::RuntimeProcessExited,
        diagnostic: Some(crate::failure_evidence::sanitize_exit_summary(summary)),
        process_logs: Some(Box::new(crate::failure_evidence::FailureProcessLogs {
            stdout: crate::failure_evidence::log_tail(b"rank 0 listening on 8888\n"),
            stderr: crate::failure_evidence::log_tail(
                b"FileNotFoundError: /opt/vonk/prewarm/sglang/flashinfer/autotune/fused_moe.json\n",
            ),
        })),
    };
    let observation = ExactRecipeRunObservation {
        run_id: uuid::Uuid::parse_str("00000000-0000-4000-8000-000000000032").unwrap(),
        run_generation: 1,
        process_running: false,
        endpoint_ready: Some(false),
        failure_diagnostics: exited_workload_diagnostics(&exited),
    };
    let observed_at = chrono::DateTime::parse_from_rfc3339("2026-10-17T08:00:00+00:00")
        .unwrap()
        .with_timezone(&chrono::Utc);
    let report = build_exact_recipe_run_observations(observed_at, &[observation]).unwrap();
    assert_is_vector(
        "recipe-run-observation-exited-v1.json",
        serde_json::to_value(report).unwrap(),
    );
}
