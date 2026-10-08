#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn host_runtime_grant_ttl_fits_inside_renewed_operation_lease() {
    let payload = ArtifactDistributionPayload {
        plan_digest: "c".repeat(64),
    };
    let claim = AgentClaim {
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

#[test]
fn job_run_grant_binding_hashes_the_typed_plan_and_keeps_target_distinct() {
    let (claim, plan) = job_run_plan_fixture();
    let request = HostRuntimeRequest {
        action: HostRuntimeAction::Start,
        fence: Uuid::parse_str(claim["fence"].as_str().unwrap()).unwrap(),
        arguments: vec!["job-run".to_owned()],
        job_plan: Some(plan.clone()),
        installation_id: None,
        reconciliation_identity: None,
        run_generation: Some(plan.run_generation),
        start_plan: None,
        stop_plan: None,
    };
    request.validate().unwrap();

    let binding = super::host_runtime_plan_binding(&request).unwrap();
    let plan_sha256 = hex_sha256(&vonk_agent_protocol::canonical_generated_json(&plan).unwrap());
    assert_eq!(
        binding.start_plan_sha256.as_deref(),
        Some(plan_sha256.as_str())
    );
    assert_eq!(binding.stop_plan_sha256, None);
    assert_eq!(binding.run_generation, Some(plan.run_generation));
    assert_eq!(binding.runtime_run_id, Some(plan.run_id));
    assert_eq!(binding.runtime_target_id, Some(plan.job_id));
    assert_ne!(binding.runtime_run_id, binding.runtime_target_id);
    assert_eq!(binding.runtime_installation_id, Some(plan.installation_id));

    let agent_claim: AgentClaim = serde_json::from_value(claim.clone()).unwrap();
    let grant_request = super::build_host_runtime_grant_request(
        &agent_claim,
        &request,
        &hex_sha256(&canonical_json(&request).unwrap()),
    )
    .unwrap();
    assert_eq!(
        grant_request.start_plan_sha256.as_deref(),
        Some(plan_sha256.as_str())
    );
    assert_eq!(grant_request.stop_plan_sha256, None);
    assert_eq!(grant_request.run_generation, Some(plan.run_generation));
    assert_eq!(grant_request.runtime_run_id, Some(plan.run_id));
    assert_eq!(grant_request.runtime_target_id, Some(plan.job_id));
    assert_eq!(
        grant_request.runtime_installation_id,
        Some(plan.installation_id)
    );
}

#[test]
fn job_run_stop_grant_binding_uses_exact_stop_target_and_logical_parent() {
    let (claim, plan) = job_run_plan_fixture();
    let stop_plan: vonk_agent_protocol::generated::RecipeStopPayload =
        serde_json::from_value(json!({
            "run_id": plan.run_id,
            "target_runtime_id": plan.job_id,
            "run_generation": plan.run_generation,
            "installation_id": plan.installation_id,
            "recipe_revision_id": plan.recipe_revision_id,
            "mapping_id": plan.mapping_id,
            "plan_digest": plan.plan_digest,
            "rank": plan.compiled_execution_plan.runtime.placement.rank,
            "role": plan.compiled_execution_plan.runtime.placement.role,
            "recipe_content_sha256": plan.compiled_execution_plan.identity.recipe_revision_sha256,
            "stop_timeout_seconds": plan.compiled_execution_plan.lifecycle.stop_timeout_seconds,
            "cancel_pending_start": false
        }))
        .unwrap();
    let request = HostRuntimeRequest {
        action: HostRuntimeAction::Stop,
        fence: Uuid::parse_str(claim["fence"].as_str().unwrap()).unwrap(),
        arguments: Vec::new(),
        job_plan: None,
        installation_id: None,
        reconciliation_identity: None,
        run_generation: Some(stop_plan.run_generation),
        start_plan: None,
        stop_plan: Some(stop_plan.clone()),
    };
    request.validate().unwrap();

    let binding = super::host_runtime_plan_binding(&request).unwrap();
    let stop_sha256 =
        hex_sha256(&vonk_agent_protocol::canonical_generated_json(&stop_plan).unwrap());
    assert_eq!(binding.start_plan_sha256, None);
    assert_eq!(
        binding.stop_plan_sha256.as_deref(),
        Some(stop_sha256.as_str())
    );
    assert_eq!(binding.run_generation, Some(plan.run_generation));
    assert_eq!(binding.runtime_run_id, Some(plan.run_id));
    assert_eq!(binding.runtime_target_id, Some(plan.job_id));
    assert_eq!(binding.runtime_installation_id, Some(plan.installation_id));

    let agent_claim: AgentClaim = serde_json::from_value(claim.clone()).unwrap();
    let grant_request = super::build_host_runtime_grant_request(
        &agent_claim,
        &request,
        &hex_sha256(&canonical_json(&request).unwrap()),
    )
    .unwrap();
    assert_eq!(grant_request.start_plan_sha256, None);
    assert_eq!(
        grant_request.stop_plan_sha256.as_deref(),
        Some(stop_sha256.as_str())
    );
    assert_eq!(grant_request.run_generation, Some(plan.run_generation));
    assert_eq!(grant_request.runtime_run_id, Some(plan.run_id));
    assert_eq!(grant_request.runtime_target_id, Some(plan.job_id));
    assert_eq!(
        grant_request.runtime_installation_id,
        Some(plan.installation_id)
    );
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
