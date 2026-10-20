#![cfg(test)]

use super::*;
use crate::failure_evidence::{FailureProcessLogs, log_tail};
use crate::outcome::{ExecutionResult, Failure, UnknownEvidence};
use serde_json::Value;
use vonk_agent_protocol::generated::{AgentOperation, FailureStage, WaitReason};

#[test]
fn redaction_follows_the_shared_rule() {
    // The Controller redacts by the same vector: a sentence reads the same
    // whichever side redacted it.
    let vector: Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/host-path-redaction-v1.json"
    ))
    .unwrap();
    assert_eq!(vector["marker"], HOST_PATH_MARKER);
    assert_eq!(vector["shortest_prose_bound"], SHORTEST_PROSE_BOUND);
    for case in vector["cases"].as_array().unwrap() {
        let text = case["text"].as_str().unwrap();
        assert_eq!(redact_host_paths(text), case["redacted"], "{text}");
    }
}

fn prose_leaves(value: &Value, found: &mut Vec<String>) {
    match value {
        Value::Array(items) => items.iter().for_each(|item| prose_leaves(item, found)),
        Value::Object(fields) => {
            for (key, field) in fields {
                match field {
                    Value::String(text) if PROSE_LEAVES.contains(&key.as_str()) => {
                        found.push(text.clone());
                    }
                    _ => prose_leaves(field, found),
                }
            }
        }
        _ => {}
    }
}

#[test]
fn no_host_path_leaves_the_agent_in_any_result_form() {
    // The class guard: whichever constructor built the result, the bytes sent
    // carry no host path in a sentence, and its core is what was built.
    let logs = FailureProcessLogs {
        exit_code: None,
        stdout: log_tail(b"weights from /outputs/cache\n"),
        stderr: log_tail(b"FileNotFoundError: /opt/vonk/prewarm/x.json\n"),
    };
    let results = [
        ExecutionResult::Failed(
            Failure::new("cannot open /opt/vonk/prewarm/x.json")
                .diagnostic("exception=\"FileNotFoundError: '/opt/vonk/x.json'\"")
                .process_logs(Some(logs)),
        ),
        ExecutionResult::cancelled("stopped while writing C:\\cache\\x"),
        ExecutionResult::unknown(
            WaitReason::RuntimeEffectUnconfirmed,
            "lost the workload at /run/vonk/x",
            UnknownEvidence::at(FailureStage::Unknown).because("probe of /proc/1 failed"),
        ),
    ];
    for operation in [
        AgentOperation::RecipeStart,
        AgentOperation::RecipeInstall,
        AgentOperation::RecipeStop,
        AgentOperation::ArtifactDistributionV1,
    ] {
        for built in results.clone() {
            let finished = built.finish_for(&operation);
            let result = AgentResult {
                fence: uuid::Uuid::nil(),
                result: finished.result,
                state: finished.state,
            };
            let sent = serde_json::to_value(wire_result(&result)).unwrap();
            let mut prose = Vec::new();
            prose_leaves(&sent, &mut prose);
            assert!(!prose.is_empty());
            for text in &prose {
                assert!(!text.contains(['/', '\\']), "{operation:?}: {text}");
            }
            let original = serde_json::to_value(&result).unwrap();
            assert_eq!(sent["state"], original["state"]);
            assert_eq!(sent["result"]["kind"], original["result"]["kind"]);
            assert_eq!(sent["result"]["code"], original["result"]["code"]);
            // The captured streams are evidence the contract admits paths in.
            let stderr = &original["result"]["evidence"]["diagnostics"]["stderr"];
            assert_eq!(&sent["result"]["evidence"]["diagnostics"]["stderr"], stderr);
        }
    }
}

#[test]
fn a_reason_nested_in_a_done_body_is_prose_too() {
    let vector: Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
    ))
    .unwrap();
    let mut receipt: vonk_agent_protocol::generated::RecipeJobRunResult =
        serde_json::from_value(vector["result"].clone()).unwrap();
    receipt.reason = Some("exited at /opt/x".to_owned());
    let finished =
        ExecutionResult::done(receipt.clone()).finish_for(&AgentOperation::RecipeJobRunV1);
    let result = AgentResult {
        fence: uuid::Uuid::nil(),
        result: finished.result,
        state: finished.state,
    };

    let sent = serde_json::to_value(wire_result(&result)).unwrap();

    assert_eq!(sent["result"]["result"]["reason"], "exited at <host-path>");
    assert_eq!(
        sent["result"]["result"]["output_manifest"],
        vector["result"]["output_manifest"]
    );
}
