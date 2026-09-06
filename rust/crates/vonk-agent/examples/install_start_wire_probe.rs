#![forbid(unsafe_code)]

//! A tiny stdin/stdout wire probe for the schema-2 recipe install/start path.
//!
//! The input is one Controller AgentClaim per line.  The probe deliberately
//! goes through the same protocol parser and compiled-plan validator used by
//! the production agent before producing one canonical AgentResult line.

use std::io::{self, BufRead, Write};

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use vonk_agent::workloads::CompiledExecutionPlan;
use vonk_agent_protocol::{AgentClaim, AgentResult, RecipeOperationRequest, canonical_json};

fn digest(value: &Value) -> String {
    let mut hasher = Sha256::new();
    hasher.update(canonical_json(value).expect("bounded JSON is canonicalizable"));
    hex::encode(hasher.finalize())
}

fn required_string<'a>(payload: &'a Map<String, Value>, name: &str) -> &'a str {
    payload
        .get(name)
        .and_then(Value::as_str)
        .unwrap_or_else(|| panic!("recipe payload is missing {name}"))
}

fn plan_from_claim(claim: &AgentClaim) -> Result<CompiledExecutionPlan, String> {
    let raw = claim
        .payload
        .get("compiled_execution_plan")
        .ok_or_else(|| "compiled execution plan is missing".to_owned())?;
    let plan: CompiledExecutionPlan = serde_json::from_value(raw.clone())
        .map_err(|_| "compiled execution plan is invalid".to_owned())?;
    plan.validate()
        .map_err(|_| "compiled execution plan is invalid".to_owned())?;
    Ok(plan)
}

fn model_identity(plan: &CompiledExecutionPlan) -> Result<String, String> {
    let artifact = plan
        .artifacts
        .first()
        .ok_or_else(|| "compiled execution plan has no model artifact".to_owned())?;
    Ok(format!(
        "{}/{}@{}",
        artifact.model.publisher, artifact.model.slug, artifact.model.content_sha256
    ))
}

fn image_digest(plan: &CompiledExecutionPlan) -> Result<String, String> {
    plan.runtime
        .image_digest
        .strip_prefix("sha256:")
        .map(str::to_owned)
        .ok_or_else(|| "compiled execution plan image digest is invalid".to_owned())
}

fn start_evidence(claim: &AgentClaim, plan: &CompiledExecutionPlan) -> Result<Value, String> {
    let payload = claim
        .payload
        .as_object()
        .ok_or_else(|| "recipe payload is not an object".to_owned())?;
    let model_identity = model_identity(plan)?;
    let image_digest = image_digest(plan)?;
    let recipe_revision_id = required_string(payload, "recipe_revision_id");
    let recipe_content_sha256 = required_string(payload, "recipe_content_sha256");
    let rank = payload
        .get("rank")
        .cloned()
        .ok_or_else(|| "recipe payload is missing rank".to_owned())?;
    let world_size = payload
        .get("world_size")
        .cloned()
        .ok_or_else(|| "recipe payload is missing world_size".to_owned())?;
    let memory_reservation_bytes = payload
        .get("reserved_memory_bytes")
        .cloned()
        .ok_or_else(|| "recipe payload is missing reserved_memory_bytes".to_owned())?;
    let artifact_set_digest = plan.identity.model_artifact_set_sha256.clone();
    let phase = payload.get("phase").and_then(Value::as_str);
    let exact_inspection = payload.get("run_generation").is_some();
    let mut identity = Map::new();

    identity.insert("recipe_revision_id".to_owned(), json!(recipe_revision_id));
    identity.insert(
        "recipe_content_sha256".to_owned(),
        json!(recipe_content_sha256),
    );
    identity.insert("image_digest".to_owned(), json!(image_digest));
    identity.insert("artifact_set_digest".to_owned(), json!(artifact_set_digest));
    identity.insert("model_identity".to_owned(), json!(model_identity));
    identity.insert("rank".to_owned(), rank);
    identity.insert("world_size".to_owned(), world_size);
    identity.insert(
        "memory_reservation_bytes".to_owned(),
        memory_reservation_bytes,
    );

    if phase == Some("rank-launch") {
        identity.insert("phase".to_owned(), json!("rank-launch"));
        identity.insert(
            "run_id".to_owned(),
            json!(required_string(payload, "run_id")),
        );
        identity.insert("role".to_owned(), json!(required_string(payload, "role")));
        for name in ["local_address", "master_address", "master_port"] {
            identity.insert(
                name.to_owned(),
                payload
                    .get(name)
                    .cloned()
                    .ok_or_else(|| format!("recipe payload is missing {name}"))?,
            );
        }
        identity.insert("process_running".to_owned(), json!(true));
        identity.insert("fabric_projection_bound".to_owned(), json!(true));
        identity.insert("launched".to_owned(), json!(true));
    } else {
        let endpoint_address = required_string(payload, "endpoint_address");
        let port = payload
            .get("port")
            .and_then(Value::as_u64)
            .ok_or_else(|| "recipe payload has an invalid port".to_owned())?;
        let host = if endpoint_address.contains(':') {
            format!("[{endpoint_address}]")
        } else {
            endpoint_address.to_owned()
        };
        identity.insert(
            "endpoint".to_owned(),
            json!(format!("http://{host}:{port}")),
        );
        identity.insert("ready".to_owned(), json!(true));
        if phase == Some("collective-readiness") {
            identity.insert("phase".to_owned(), json!("collective-readiness"));
            identity.insert(
                "run_id".to_owned(),
                json!(required_string(payload, "run_id")),
            );
            identity.insert("role".to_owned(), json!(required_string(payload, "role")));
        }
    }

    if exact_inspection {
        identity.insert(
            "run_generation".to_owned(),
            payload
                .get("run_generation")
                .cloned()
                .ok_or_else(|| "recipe payload is missing run_generation".to_owned())?,
        );
        identity.insert(
            "runtime_arguments_sha256".to_owned(),
            json!(digest(&json!({
                "runtime": &plan.runtime,
                "placement": &plan.topology,
            }))),
        );
        if phase != Some("rank-launch") {
            for name in ["local_address", "master_address", "master_port"] {
                identity.insert(
                    name.to_owned(),
                    payload
                        .get(name)
                        .cloned()
                        .ok_or_else(|| format!("recipe payload is missing {name}"))?,
                );
            }
        }
    }

    let evidence_digest = digest(&Value::Object(identity.clone()));
    identity.insert("evidence_digest".to_owned(), json!(evidence_digest));
    Ok(Value::Object(identity))
}

fn result_for(claim: &AgentClaim) -> Result<AgentResult, String> {
    // Validate the complete production claim before accepting any plan or
    // emitting any result, then call the production recipe parser explicitly.
    claim
        .validate()
        .map_err(|_| "agent claim is invalid".to_owned())?;
    let _request = RecipeOperationRequest::parse(claim)
        .map_err(|_| "recipe operation payload is invalid".to_owned())?;
    let plan = plan_from_claim(claim)?;
    let payload = claim
        .payload
        .as_object()
        .ok_or_else(|| "recipe payload is not an object".to_owned())?;
    let evidence = if claim.operation == "recipe.install" {
        json!({
            "installed_bytes": payload
                .get("expected_bytes")
                .and_then(Value::as_u64)
                .ok_or_else(|| "recipe payload has invalid expected_bytes".to_owned())?,
        })
    } else if claim.operation == "recipe.start" {
        start_evidence(claim, &plan)?
    } else {
        return Err("probe only accepts recipe.install and recipe.start".to_owned());
    };
    let result = AgentResult {
        attempt: claim.attempt,
        deadline: claim.deadline,
        fence: claim.fence,
        job_id: claim.job_id,
        node_id: claim.node_id.clone(),
        operation_id: claim.operation_id,
        result: json!({"status": "ok", "evidence": evidence}),
        schema_version: 1,
        state: "succeeded".to_owned(),
    };
    result
        .validate()
        .map_err(|_| "canonical agent result is invalid".to_owned())?;
    Ok(result)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut output = io::BufWriter::new(stdout.lock());
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let claim: AgentClaim = serde_json::from_str(&line)?;
        let result = result_for(&claim).map_err(io::Error::other)?;
        serde_json::to_writer(&mut output, &result)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}
