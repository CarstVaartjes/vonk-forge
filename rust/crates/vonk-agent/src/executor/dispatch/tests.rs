#![cfg(test)]
use super::*;
use crate::executor::test_support::{NoProcess, claim};
use uuid::Uuid;

#[tokio::test]
async fn an_invalid_compiled_plan_is_refused_at_dispatch_with_its_rule_text() {
    let data = tempfile::tempdir().unwrap();
    let runtime = tempfile::tempdir().unwrap();
    let client = AgentHttpClient::for_http_test(
        "http://127.0.0.1:1/",
        "spk_0123456789abcdef0123456789abcdef",
    );
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime_root: runtime.path(),
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
    };
    let mut plan: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
    ))
    .unwrap();
    plan["runtime"]["placement"]["endpoint_address"] = serde_json::json!("100.100.20.30");
    // A single-node service may only bridge; host networking is refused.
    plan["security"]["network_mode"] = serde_json::json!("host");
    let mut start = claim();
    start.operation = AgentOperation::RecipeStart;
    start.payload = serde_json::from_value(serde_json::json!({
        "run_id": "00000000-0000-4000-8000-0000000000aa",
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "recipe_revision_id": "00000000-0000-4000-8000-0000000000bb",
        "mapping_id": "00000000-0000-4000-8000-0000000000cc",
        "run_generation": 1,
        "plan_digest": "a".repeat(64),
        "compiled_execution_plan": plan,
    }))
    .unwrap();
    let (_lease, deadline) = tokio::sync::watch::channel(start.deadline);
    let (_cancel, cancellation) = tokio::sync::watch::channel(false);
    let result = executor.execute(&start, deadline, cancellation).await;
    let ExecutionResult::Failed(failure) = result else {
        panic!("an invalid plan must be refused");
    };
    assert_eq!(failure.reason, "compiled execution plan is invalid");
    assert!(
        failure
            .diagnostic
            .as_deref()
            .is_some_and(|text| text.contains("compiled security")),
        "{:?}",
        failure.diagnostic
    );
}

#[tokio::test]
async fn a_refusal_past_its_cool_down_is_offered_again_and_never_holds_the_claim_lane() {
    // P6: a refused result is evidence the Controller never confirmed. It
    // is offered again on its schedule, never retired after 900 s, and the
    // fresh claim runs in the same pass.
    use crate::executor::test_support::{FailedExecutor, NODE_ID, RecordingClient};
    use std::sync::{Arc, Mutex};
    let root = tempfile::tempdir().unwrap();
    let mut state = StateStore::open(&root.path().join("state.sqlite"), NODE_ID).unwrap();
    let old = claim();
    state.begin(&old, Utc::now()).unwrap();
    let retained = state.finish(&old, recipe_install_success(0)).unwrap();
    state
        .reject_result(
            &retained,
            &ControllerError::from_status(422),
            Utc::now() - chrono::Duration::seconds(901),
        )
        .unwrap();
    let mut fresh = old.clone();
    fresh.fence = Uuid::new_v4();
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(fresh.clone()))),
        fail_heartbeat: false,
        heartbeats: Arc::new(Mutex::new(Vec::new())),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    tokio::time::timeout(
        Duration::from_secs(1),
        run_once(&client, &mut state, &FailedExecutor, None, 0, None),
    )
    .await
    .unwrap()
    .unwrap()
    .settled();
    let results = client.results.lock().unwrap().clone();
    assert!(results.iter().any(|result| result == &retained));
    assert!(results.iter().any(|result| result.fence == fresh.fence));
    // The Controller accepted it this time: acknowledged, refusal cleared.
    assert!(
        state.pending_results().unwrap().is_empty(),
        "unexpected content"
    );
    assert_eq!(state.retained_rejection(&retained).unwrap(), None);
}

#[test]
fn unavailable_package_preparation_allows_a_fresh_upgrade_handoff() {
    use crate::agent_upgrade::AgentUpgradeError;
    use vonk_agent_protocol::generated::{AgentResultResult, HelperErrorCode};

    // Preparation loss is recoverable bookkeeping, not a permanent refusal.
    let unavailable = upgrade_outcome(Err(AgentUpgradeError::HelperRejectedWithCode {
        code: HelperErrorCode::PackagePreparationUnavailable,
        exit_code: None,
        diagnostic: None,
    }))
    .finish_for(&AgentOperation::AgentUpgradeV1);
    assert!(matches!(
        unavailable.result,
        AgentResultResult::OutcomeUnknown(_)
    ));
    let fresh = upgrade_outcome(Ok(())).finish_for(&AgentOperation::AgentUpgradeV1);
    assert!(matches!(fresh.result, AgentResultResult::OutcomeUnknown(_)));

    // The same dispatch still refuses unverified package bytes at ingress.
    let unverified = upgrade_outcome(Err(AgentUpgradeError::HelperRejectedWithCode {
        code: HelperErrorCode::PackageVerificationFailed,
        exit_code: None,
        diagnostic: None,
    }));
    assert!(matches!(unverified, ExecutionResult::Failed(_)));
    assert!(matches!(
        upgrade_outcome(Ok(())),
        ExecutionResult::Unknown(_)
    ));
}

#[test]
fn helper_install_and_response_loss_reach_unknown_wire_then_accept_a_fresh_handoff() {
    use std::io::{Read, Write};
    use std::os::unix::net::UnixListener;
    use vonk_agent_protocol::generated::{
        AgentResultResult, HostHelperResponse, HostHelperResponseStatus,
    };
    use vonk_agent_protocol::{AgentResult, canonical_json, parse_strict};
    let root = tempfile::tempdir().unwrap();
    let socket = root.path().join("helper.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    let request_id = Uuid::new_v4();
    listener.set_nonblocking(true).unwrap();
    let peer = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(5);
        for valid in [false, true, true] {
            let (mut stream, _) = loop {
                assert!(Instant::now() < deadline);
                match listener.accept() {
                    Ok(connection) => break connection,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    Err(error) => panic!("helper fixture unavailable: {error}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            let mut prefix = [0; 4];
            stream.read_exact(&mut prefix).unwrap();
            let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
            stream.read_exact(&mut body).unwrap();
            if !valid {
                continue;
            }
            let response = HostHelperResponse {
                schema_version: 1,
                request_id: Some(request_id),
                installation_intent_nonce: None,
                status: HostHelperResponseStatus::PackageInstalled,
                diagnostic: None,
                process_logs: None,
                error_code: None,
                exit_code: None,
                process_running: None,
                job_progress_at: None,

                container_labels: None,
            };
            let bytes = canonical_json(&response).unwrap();
            stream
                .write_all(&u32::try_from(bytes.len()).unwrap().to_be_bytes())
                .unwrap();
            stream.write_all(&bytes).unwrap();
        }
    });
    for expected_install in [false, true, true] {
        let observed = crate::agent_upgrade::call_helper_within(
            &socket,
            b"signed request",
            &Inactivity::new(Duration::from_secs(1)),
        )
        .and_then(|response| {
            crate::agent_upgrade::installed_handoff(&response, &request_id.to_string())
        });
        assert_eq!(observed.is_ok(), expected_install);
        let finished = upgrade_outcome(observed).finish_for(&AgentOperation::AgentUpgradeV1);
        let result = AgentResult {
            fence: request_id,
            result: finished.result,
            state: finished.state,
        };
        let consumed: AgentResult = parse_strict(&canonical_json(&result).unwrap()).unwrap();
        consumed
            .validate_for_operation(&AgentOperation::AgentUpgradeV1)
            .unwrap();
        assert!(matches!(
            consumed.result,
            AgentResultResult::OutcomeUnknown(_)
        ));
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while !peer.is_finished() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(1));
    }
    assert!(peer.is_finished());
    drop(peer);
}

#[tokio::test]
async fn superseded_distribution_yields_without_network_or_host_effects() {
    let data = tempfile::tempdir().unwrap();
    let runtime = tempfile::tempdir().unwrap();
    let client = AgentHttpClient::for_http_test(
        "http://127.0.0.1:1/",
        "spk_0123456789abcdef0123456789abcdef",
    );
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime_root: runtime.path(),
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
    };
    let mut obsolete = claim();
    obsolete.operation = AgentOperation::ArtifactDistributionV1;
    obsolete.payload =
        vonk_agent_protocol::generated::AgentClaimPayload::ArtifactDistributionPayload(
            vonk_agent_protocol::generated::ArtifactDistributionPayload {
                assignment: serde_json::from_str::<
                    vonk_agent_protocol::generated::ArtifactDistributionPayload,
                >(include_str!(concat!(
                    env!("CARGO_MANIFEST_DIR"),
                    "/../../../agent_protocol/fixtures/artifact-distribution-payload.json"
                )))
                .unwrap()
                .assignment,
                plan_digest: "a".repeat(64),
            },
        );
    let (_lease, deadline) = tokio::sync::watch::channel(obsolete.deadline);
    let (cancel, cancellation) = tokio::sync::watch::channel(true);
    let result = tokio::time::timeout(
        Duration::from_secs(1),
        executor.execute(&obsolete, deadline, cancellation),
    )
    .await
    .unwrap();
    // Before any transfer or helper effect, cancellation is confirmed;
    // there is no unknown effect for the Controller to reconcile.
    let ExecutionResult::Failed(failure) = result else {
        panic!("expected confirmed cancellation before distribution effects");
    };
    assert_eq!(failure.code, Some(FailureCode::OperationCancelled));
    assert!(fs::read_dir(data.path()).unwrap().next().is_none());
    assert!(fs::read_dir(runtime.path()).unwrap().next().is_none());
    // Cancellation ownership is per request; ending an obsolete transfer
    // leaves a new operation free to execute on the normal dispatch path.
    drop(cancel);
    let mut current = obsolete.clone();
    current.fence = Uuid::new_v4();
    // The accepted assignment now enters resumable object transfer directly.
    // End this attempt's authority before its network retry backoff, rather
    // than assuming that an unavailable Controller answers within one second.
    current.deadline = (Utc::now() + chrono::Duration::milliseconds(250)).fixed_offset();
    let (_lease, deadline) = tokio::sync::watch::channel(current.deadline);
    let (_cancel, cancellation) = tokio::sync::watch::channel(false);
    let result = tokio::time::timeout(
        Duration::from_secs(1),
        executor.execute(&current, deadline, cancellation),
    )
    .await
    .unwrap();
    // The fresh transfer reaches the unavailable Controller, rather than
    // inheriting cancellation or being refused by stale local ownership.
    let ExecutionResult::Failed(failure) = &result else {
        panic!("expected the fresh distribution to reach its network dependency");
    };
    assert_eq!(
        failure.code,
        Some(FailureCode::RuntimeObservationUnavailable)
    );
    assert_eq!(failure.stage, Some(FailureStage::ArtifactDistribution));
    assert!(data.path().join("distribution/models").is_dir());
    assert_eq!(
        failure.failure_kind,
        Some(AgentFailureKind::TemporaryDependency)
    );
    let finished = result.finish(&current);
    AgentResult {
        fence: current.fence,
        result: finished.result,
        state: finished.state,
    }
    .validate_for_operation(&current.operation)
    .unwrap();
}
