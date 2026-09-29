use std::io::{self, BufRead};

use serde_json::{Value, json};
use uuid::Uuid;
use vonk_agent_protocol::{AgentClaim, RecipeOperationRequest};

fn main() {
    for line in io::stdin().lock().lines() {
        let line = line.expect("probe input");
        if line.trim().is_empty() {
            continue;
        }
        let input: Value = serde_json::from_str(&line).expect("probe request");
        let claim = if input.get("claim").is_some() {
            serde_json::from_value(input["claim"].clone()).expect("wire claim")
        } else {
            let operation = input["operation"].as_str().expect("operation");
            let payload = input["payload"].clone();
            let payload = serde_json::from_value(payload).expect("typed payload");
            AgentClaim {
                deadline: "2026-12-31T00:00:00Z".parse().expect("deadline"),
                fence: Uuid::new_v4(),
                operation: operation.parse().expect("operation"),
                payload,
            }
        };
        let operation = claim.operation;
        let payload = claim.payload.clone();
        let parsed = RecipeOperationRequest::parse(&claim).expect("valid operation");
        let evidence = match parsed {
            RecipeOperationRequest::BuildCleanup(_) => json!({}),
            RecipeOperationRequest::Build(_) => json!({
                "image_bytes": 1,
                "image_digest": format!("sha256:{}", "a".repeat(64)),
                "oci_layout_sha256": "b".repeat(64),
            }),
            RecipeOperationRequest::ImageImport(_) => json!({}),
            _ => panic!("unexpected operation"),
        };
        println!(
            "{}",
            serde_json::to_string(&json!({
                "operation": operation,
                "payload": payload,
                "evidence": evidence,
            }))
            .expect("probe output")
        );
    }
}
