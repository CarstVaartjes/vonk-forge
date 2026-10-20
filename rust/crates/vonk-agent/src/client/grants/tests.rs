#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn host_runtime_grant_ttl_fits_inside_renewed_operation_lease() {
    let payload = ArtifactDistributionPayload {
        assignment: serde_json::from_str::<
            vonk_agent_protocol::generated::ArtifactDistributionPayload,
        >(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../agent_protocol/fixtures/artifact-distribution-payload.json"
        )))
        .unwrap()
        .assignment,
        plan_digest: "c".repeat(64),
    };
    let claim = AgentClaim {
        observation_budget_seconds: 3600,
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        operation: AgentOperation::ArtifactDistributionV1,
        payload: AgentClaimPayload::ArtifactDistributionPayload(payload),
        deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
    };
    let runtime_request = HostRuntimeRequest {
        action: HostRuntimeAction::ImagePull,
        fence: claim.fence,
        arguments: vec!["image-pull".to_owned()],
        job_plan: None,
        installation_id: None,
        reconciliation_identity: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    };
    let request_sha256 = hex_sha256(&canonical_json(&runtime_request).unwrap());
    let (client, server) = host_runtime_grant_client().await;

    client
        .host_runtime_grant(&claim, &runtime_request, &request_sha256)
        .await
        .unwrap();
    let request = finish_capture_peer(server).await;
    let body = request
        .windows(4)
        .position(|value| value == b"\r\n\r\n")
        .map(|index| &request[index + 4..])
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(body).unwrap();

    assert_eq!(body["expires_in_seconds"], 10);
    for field in [
        "start_plan_sha256",
        "stop_plan_sha256",
        "run_generation",
        "runtime_run_id",
        "runtime_target_id",
        "runtime_installation_id",
    ] {
        assert!(
            body.get(field).is_none(),
            "unused grant field {field} is omitted"
        );
    }
}

#[tokio::test]
async fn disposition_retains_full_controller_generation_header() {
    // Match the production ordinary HTTP transport budget, rather than
    // creating a separate fixture-only allowance.
    let (client, server) = request_capture_client(
        200,
        vec![format!("x-vonk-recipe-run-generation: {}", i64::MAX)],
        Vec::new(),
        None,
        CONTROLLER_REQUEST_TIMEOUT,
    )
    .await;
    let disposition = client.recipe_run_disposition(Uuid::new_v4()).await.unwrap();
    assert_eq!(
        disposition,
        RecipeRunDisposition::Known {
            run_generation: Some(i64::MAX as u64)
        }
    );
    finish_capture_peer(server).await;
}
