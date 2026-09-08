use std::io::{self, BufRead};

use serde_json::{Value, json};
use uuid::Uuid;
use vonk_agent_protocol::{AgentClaim, RecipeOperationRequest, canonical_json, hex_sha256};

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
            let payload_bytes = canonical_json(&payload).expect("payload canonicalization");
            AgentClaim {
                attempt: 1,
                authority_revision: "a".repeat(64),
                deadline: "2026-12-31T00:00:00Z".parse().expect("deadline"),
                fence: Uuid::new_v4(),
                job_id: Uuid::new_v4(),
                node_id: "spk_11111111111111111111111111111111".to_owned(),
                operation: operation.parse().expect("operation"),
                operation_id: Uuid::new_v4(),
                payload: serde_json::from_value(payload.clone()).expect("typed payload"),
                payload_digest: hex_sha256(&payload_bytes),
                schema_version: 1,
            }
        };
        let operation = claim.operation.clone();
        let payload = claim.payload.clone();
        let parsed = RecipeOperationRequest::parse(&claim).expect("valid operation");
        let evidence = match parsed {
            RecipeOperationRequest::Build(request) => json!({
                "build_input_sha256": request.build_input_sha256,
                "image_bytes": 1,
                "image_digest": format!("sha256:{}", "a".repeat(64)),
                "oci_layout_sha256": "b".repeat(64),
                "policy": {
                    "passed": true,
                    "dockerfile": request.dockerfile,
                    "findings": [],
                },
            }),
            RecipeOperationRequest::ImageImport(request) => json!({
                "build_id": request.build_id,
                "image_bytes": request.image_bytes,
                "image_digest": request.image_digest,
                "oci_layout_sha256": request.oci_layout_sha256,
            }),
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
