#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn a_request_document_between_the_retired_private_cap_and_the_exchange_ceiling_is_read() {
    // Wrong implementation: the helper read the request file under a
    // private `MAX_RUNTIME_REQUEST_BYTES = 64 * 1024` round number, so a
    // legitimate many-mount command line the plan admits -- and the agent
    // admitted under the same byte budget -- was refused as
    // `helper.unsafe_path` after a successful install, blaming the path
    // rather than the bound.
    let temp = tempfile::tempdir().unwrap();
    let requests = temp.path().join("runtime-requests");
    fs::create_dir_all(&requests).unwrap();
    let request = HostRuntimeRequest {
        inspect_target: None,
        action: HostRuntimeAction::ImagePull,
        fence: uuid::Uuid::new_v4(),
        arguments: (0..3000)
            .map(|index| format!("--mount=type=bind,src=/run/vonk/models/{index:05}"))
            .collect(),
        installation_id: None,
        reconciliation_identity: None,
        job_plan: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    };
    let body = vonk_agent_protocol::canonical_json(&request).unwrap();
    assert!(
        body.len() > 64 * 1024,
        "this document must exceed the retired private cap, got {} bytes",
        body.len()
    );
    assert!(body.len() <= vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES);
    let digest = hex_sha256(&body);
    let path = requests.join(format!("{digest}.json"));
    fs::write(&path, &body).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();

    let executor = OperationExecutor::new(
        ManagedRoots::under(temp.path()).with_runtime_requests(&requests),
        &[0; 32],
        MissingContainerRunner,
        None,
    )
    .unwrap();
    let read = executor
        .read_runtime_request(&digest)
        .expect("a request inside the exchange ceiling must be read");
    assert_eq!(read.arguments, request.arguments);
}

/// The canonical job claim re-placed as `rank` of `world_size` (a pair of
/// Sparks sharing a fabric when `world_size` is 2).
fn job_rank(rank: u64, world_size: u64) -> vonk_agent_protocol::RecipeJobRunRequest {
    let claim: vonk_agent_protocol::generated::AgentClaim = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    let vonk_agent_protocol::generated::AgentClaimPayload::RecipeJobRunRequest(job) = claim.payload
    else {
        panic!("expected canonical JobRun claim");
    };
    let mut value = serde_json::to_value(&job).unwrap();
    let plan = &mut value["compiled_execution_plan"];
    if world_size > 1 {
        plan["runtime"]["placement"] = serde_json::json!({
            "rank": rank,
            "role": if rank == 0 { "entrypoint" } else { "worker" },
            "world_size": world_size,
            "local_address": format!("192.168.100.{}", 10 + rank),
            "master_address": "192.168.100.10",
            "master_port": 29500,
            "endpoint_address": null,
            "port": null,
            "memory_floor_bytes": 2147483648_u64,
            "reserved_memory_bytes": 34359738368_u64,
        });
        plan["topology"] = serde_json::json!({"name": "dual", "node_count": world_size});
        plan["security"]["network_mode"] = serde_json::json!("host");
        plan["security"]["gpu"] = serde_json::json!(true);
    } else {
        plan["runtime"]["placement"]["rank"] = serde_json::json!(rank);
    }
    serde_json::from_value(value).unwrap()
}

#[test]
fn every_rank_of_a_multi_rank_job_is_authorized_and_a_rank_beyond_the_world_is_not() {
    // Wrong implementation: the job plan gate still demanded rank 0 of a
    // world of one, so both Sparks of a two-Spark job were refused as
    // `helper_operation_invalid` before Docker ran.
    let temp = tempfile::tempdir().unwrap();
    let executor = OperationExecutor::new(
        ManagedRoots::under(temp.path()),
        &[0; 32],
        MissingContainerRunner,
        None,
    )
    .unwrap();
    let authorize = |job: &vonk_agent_protocol::RecipeJobRunRequest| {
        let arguments = executor
            .projected_runtime_arguments(
                &job.compiled_execution_plan,
                job.installation_id,
                job.job_id,
            )
            .unwrap_or_default();
        let fence = uuid::Uuid::new_v4();
        let request = runtime_request_identity(
            &fence,
            RuntimeRequestTestParts {
                action: HostRuntimeAction::Start,
                arguments,
                run_generation: job.run_generation,
                start_plan: None,
                stop_plan: None,
            },
        );
        let request = HostRuntimeRequest {
            job_plan: Some(job.clone()),
            ..request
        };
        let plan_sha256 = hex_sha256(&canonical_json(job).unwrap());
        let grant = RuntimeRequestGrantBinding {
            gpu_clock: None,
            fence: &fence,
            installation_intent_nonce: None,
            installation_intent_ordinal: Some(1),
            installation_id: None,
            reconciliation_identity: None,
            start_plan_sha256: Some(&plan_sha256),
            stop_plan_sha256: None,
            run_generation: Some(job.run_generation),
            runtime_run_id: Some(&job.run_id),
            runtime_target_id: Some(&job.job_id),
            runtime_installation_id: Some(&job.installation_id),
        };
        executor.authorize_runtime_effect(&request, grant)
    };

    assert!(authorize(&job_rank(0, 1)).unwrap().is_some());
    for rank in [0, 1] {
        assert!(authorize(&job_rank(rank, 2)).unwrap().is_some());
    }
    assert!(matches!(
        authorize(&job_rank(1, 1)),
        Err(OperationError::InvalidOperation)
    ));
    let mut beyond = job_rank(1, 2);
    beyond.compiled_execution_plan.runtime.placement.rank = 2_u64.into();
    assert!(matches!(
        authorize(&beyond),
        Err(OperationError::InvalidOperation)
    ));
}
