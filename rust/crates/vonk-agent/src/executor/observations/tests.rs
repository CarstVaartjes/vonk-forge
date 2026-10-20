#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn sixty_five_exact_observations_use_two_bounded_native_wire_batches() {
    let server = ObservationServer::new(Some(204));
    let root = tempdir().unwrap();
    let installation = Uuid::new_v4().to_string();
    let mut plan: crate::workloads::CompiledExecutionPlan = serde_json::from_str(include_str!(
        "../../../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    let installed = root.path().join("installations").join(&installation);
    fs::create_dir_all(&installed).unwrap();
    fs::write(
        installed.join("spec.json"),
        serde_json::to_vec(&plan).unwrap(),
    )
    .unwrap();
    fs::write(
        installed.join("recipe-content.sha256"),
        &plan.identity.recipe_revision_sha256,
    )
    .unwrap();
    plan.runtime.placement.endpoint_address = Some("192.168.1.211".parse().unwrap());
    plan.security.network_mode = "bridge".parse().unwrap();
    plan.validate().unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    let expected: std::collections::BTreeSet<_> = (0..65).map(|_| Uuid::new_v4()).collect();
    for id in &expected {
        runtime
            .prepare_start_with_inspection_identity(
                &plan,
                &installation,
                &id.to_string(),
                &plan.runtime.placement,
                &crate::oci::RecipeRunStartIdentity { run_generation: 2 },
            )
            .unwrap();
    }
    let database = root.path().join("state.sqlite");
    let mut observations = Vec::new();
    // This test aggregates successive independently bounded pages to verify
    // the wire batches. Its aggregate fixture budget is the scan's freshness
    // budget; claim-lane latency is covered by the partial-history regression.
    let deadline = Instant::now() + crate::oci::MAX_EMPTY_SCAN_AGE.to_std().unwrap();
    loop {
        assert!(
            Instant::now() < deadline,
            "native observation fixture exceeded its elapsed budget"
        );
        let mut state = StateStore::open(&database, NODE_ID).unwrap();
        let checkpoint = state.observation_checkpoint().unwrap();
        let page = runtime
            .recipe_run_inspection_page(checkpoint.as_ref())
            .unwrap();
        assert!(page.failures.is_empty());
        // Physical liveness is the fixture input at this boundary. Exact
        // IDs/generations come only from the real persisted Start reader.
        observations.extend(page.plans.into_iter().map(|plan| {
            Ok(ExactRecipeRunObservation {
                run_id: plan.run_id,
                run_generation: plan.run_generation,
                process_running: true,
                endpoint_ready: Some(true),
                failure_diagnostics: None,
            })
        }));
        state
            .save_observation_checkpoint(page.checkpoint.as_ref())
            .unwrap();
        if page.complete {
            break;
        }
    }
    assert_eq!(
        report_recipe_run_observation_page(&server.client, Utc::now(), observations, false)
            .await
            .unwrap(),
        65
    );
    let reports = server.finish();
    assert_eq!(reports.len(), 2);
    assert_eq!(reports[0]["runs"].as_array().unwrap().len(), 64);
    assert_eq!(reports[1]["runs"].as_array().unwrap().len(), 1);
    let delivered: std::collections::BTreeSet<_> = reports
        .iter()
        .flat_map(|report| report["runs"].as_array().unwrap())
        .map(|run| {
            assert_eq!(run["run_generation"], 2);
            Uuid::parse_str(run["run_id"].as_str().unwrap()).unwrap()
        })
        .collect();
    assert_eq!(delivered, expected);
}

#[tokio::test]
async fn partial_zero_page_does_not_send_an_empty_wire_snapshot() {
    let server = ObservationServer::new(Some(204));
    assert_eq!(
        report_recipe_run_observation_page(&server.client, Utc::now(), vec![], false)
            .await
            .unwrap(),
        0
    );
    assert!(server.finish().is_empty());
}

#[tokio::test]
async fn exact_snapshot_reports_multiple_runs_together() {
    let server = ObservationServer::new(Some(204));
    let ids = [Uuid::new_v4(), Uuid::new_v4()];
    let results = ids
        .into_iter()
        .map(|id| Ok(exact_observation(id)))
        .collect();
    assert_eq!(
        report_complete_recipe_run_observations(&server.client, Utc::now(), results)
            .await
            .unwrap(),
        2
    );
    let reports = server.finish();
    assert_eq!(reports.len(), 1);
    assert!(reports[0]["observed_at"].is_string());
    let runs = reports[0]["runs"].as_array().unwrap();
    assert_eq!(runs.len(), 2);
    for id in ids {
        assert!(runs.iter().any(|run| run["run_id"] == id.to_string()));
    }
}

#[tokio::test]
async fn exact_snapshot_report_failure_never_reports_empty() {
    for status in [Some(503), Some(422), None] {
        let server = ObservationServer::new(status);
        let _error = report_complete_recipe_run_observations(
            &server.client,
            Utc::now(),
            vec![Ok(exact_observation(Uuid::new_v4()))],
        )
        .await
        .unwrap_err();
        let reports = server.finish();
        assert_eq!(reports.len(), 1);
        assert_eq!(reports[0]["runs"].as_array().unwrap().len(), 1);
    }
}

#[tokio::test]
async fn exact_snapshot_inspection_failure_preserves_other_runs_without_reporting_empty() {
    // Wrong implementation: a denied or unavailable retained run discards
    // current healthy receipts, eventually expiring that unrelated run.
    for error in [
        crate::host_runtime::HostRuntimeError::HelperProtocol(
            crate::host_runtime::HelperProtocolCause::InspectionOutcome,
        ),
        crate::host_runtime::HostRuntimeError::Controller(ClientError::Controller(Box::new(
            crate::client::ControllerError::from_status(403),
        ))),
    ] {
        let server = ObservationServer::new(Some(204));
        let current_run = Uuid::new_v4();
        let result = report_complete_recipe_run_observations(
            &server.client,
            Utc::now(),
            vec![
                Ok(exact_observation(current_run)),
                Err(RecipeObservationError::Inspection(error)),
            ],
        )
        .await;
        assert!(result.is_err());
        let reports = server.finish();
        assert_eq!(reports.len(), 1);
        let runs = reports[0]["runs"].as_array().unwrap();
        assert_eq!(runs.len(), 1);
        assert_eq!(runs[0]["run_id"], current_run.to_string());
    }
}

#[tokio::test]
async fn unowned_run_never_fails_or_hides_owned_run_reports() {
    // Live regression: a run from before a Controller database rebuild
    // turned every sweep into a failed collection.
    let server = ObservationServer::new(Some(204));
    let valid_run = Uuid::new_v4();
    let result = report_complete_recipe_run_observations(
        &server.client,
        Utc::now(),
        vec![
            Err(RecipeObservationError::UnownedRun),
            Ok(exact_observation(valid_run)),
        ],
    )
    .await
    .unwrap();
    assert_eq!(result, 1);
    let reports = server.finish();
    assert_eq!(reports.len(), 1);
    assert_eq!(reports[0]["runs"].as_array().unwrap().len(), 1);
    assert_eq!(reports[0]["runs"][0]["run_id"], valid_run.to_string());

    // With nothing owned left, the node truthfully reports no owned runs.
    let server = ObservationServer::new(Some(204));
    assert_eq!(
        report_complete_recipe_run_observations(
            &server.client,
            Utc::now(),
            vec![Err(RecipeObservationError::UnownedRun)],
        )
        .await
        .unwrap(),
        0
    );
    let reports = server.finish();
    assert_eq!(reports.len(), 1);
    assert_eq!(reports[0]["runs"], json!([]));
}

#[tokio::test]
async fn exact_snapshot_failed_inspection_is_not_proof_of_an_empty_node() {
    let server = ObservationServer::new(Some(204));
    let result = report_complete_recipe_run_observations(
        &server.client,
        Utc::now(),
        vec![Err(RecipeObservationError::Inspection(
            crate::host_runtime::HostRuntimeError::HelperProtocol(
                crate::host_runtime::HelperProtocolCause::InspectionOutcome,
            ),
        ))],
    )
    .await;
    assert!(result.is_err());
    assert!(server.finish().is_empty());
}

#[tokio::test]
async fn exact_snapshot_reports_empty_only_when_no_managed_runs_exist() {
    let data = tempdir().unwrap();
    let runtime = tempdir().unwrap();
    let server = ObservationServer::new(Some(204));
    let runner = NoProcess;
    let executor = RecipeExecutor {
        client: &server.client,
        helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime: OciRuntime {
            runner: &runner,
            data_root: data.path(),
        },
        runtime_root: runtime.path(),
    };
    assert_eq!(
        executor
            .report_exact_recipe_run_observations()
            .await
            .unwrap(),
        0
    );
    let reports = server.finish();
    assert_eq!(reports.len(), 1);
    assert!(reports[0]["observed_at"].is_string());
    assert_eq!(reports[0]["runs"], json!([]));
}

// A single bounded page is not a complete sweep, even for a small fixture:
// scheduling delays can consume its elapsed budget before an entry is read.
async fn complete_observation_sweep(executor: &RecipeExecutor<'_, NoProcess>) -> usize {
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut checkpoint = None;
    let mut reported = 0;
    loop {
        assert!(
            Instant::now() < deadline,
            "observation fixture budget expired"
        );
        let page = executor
            .report_recipe_run_observation_page(checkpoint.as_ref())
            .await
            .unwrap();
        reported += page.reported;
        checkpoint = page.checkpoint;
        if checkpoint.is_none() {
            return reported;
        }
    }
}

fn assert_fresh_run_preparation(runtime: &OciRuntime<'_, NoProcess>) {
    use vonk_agent_protocol::generated::CompiledSecurityNetworkMode;

    let mut plan: crate::workloads::CompiledExecutionPlan = serde_json::from_str(include_str!(
        "../../../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    let installation_id = Uuid::new_v4().to_string();
    let installation = runtime
        .data_root
        .join("installations")
        .join(&installation_id);
    fs::create_dir_all(&installation).unwrap();
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&plan).unwrap(),
    )
    .unwrap();
    fs::write(
        installation.join("recipe-content.sha256"),
        &plan.identity.recipe_revision_sha256,
    )
    .unwrap();
    plan.runtime.placement.endpoint_address = Some("192.168.1.211".parse().unwrap());
    plan.security.network_mode = CompiledSecurityNetworkMode::Bridge;
    runtime
        .prepare_start_with_inspection_identity(
            &plan,
            &installation_id,
            &Uuid::new_v4().to_string(),
            &plan.runtime.placement,
            &crate::oci::RecipeRunStartIdentity { run_generation: 2 },
        )
        .expect("retired bookkeeping must not block a fresh run");
}

#[tokio::test]
async fn unparseable_lifecycle_of_an_unowned_run_is_retired_not_skipped_forever() {
    // Live regression: a lifecycle written before an agent upgrade could
    // no longer be parsed, so the run was skipped every sweep forever.
    use std::os::unix::fs::PermissionsExt;
    let data = tempdir().unwrap();
    let runtime = tempdir().unwrap();
    let run_id = "e85c4710-e437-4d12-8191-499596aa2a4c";
    fs::create_dir_all(data.path().join("runs").join(run_id)).unwrap();
    let metadata = data.path().join("run-metadata").join(run_id);
    fs::create_dir_all(&metadata).unwrap();
    fs::set_permissions(&metadata, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(metadata.join("lifecycle.json"), br#"{"installation_id":1}"#).unwrap();
    fs::write(metadata.join("runtime.json"), b"{}").unwrap();
    let runner = NoProcess;
    let server = ObservationServer::with_disposition(Some(204), Some("unowned"));
    let executor = RecipeExecutor {
        client: &server.client,
        helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime: OciRuntime {
            runner: &runner,
            data_root: data.path(),
        },
        runtime_root: runtime.path(),
    };
    assert_eq!(complete_observation_sweep(&executor).await, 0);
    // A bounded page may stop before reaching this run. Resume its cursor to
    // complete the sweep; the next complete sweep has no remaining claim.
    assert_eq!(complete_observation_sweep(&executor).await, 0);
    assert_fresh_run_preparation(&executor.runtime);
    let requests = server.finish();
    // An uncertain page need not publish absence. Retirement must happen
    // once, and a fresh sweep must not ask to retire the same claim again.
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.get("disposition").is_some())
            .count(),
        1
    );
    assert_eq!(
        requests[0],
        json!({ "disposition": format!("/agent/recipe-runs/{run_id}/disposition") })
    );
    for report in &requests[1..] {
        assert!(report["observed_at"].is_string());
        assert_eq!(report["runs"], json!([]));
    }
    // Only the unusable lifecycle is retired; the run stays as history.
    assert!(!metadata.join("lifecycle.json").exists());
    assert!(metadata.join("runtime.json").exists());
    assert!(data.path().join("runs").join(run_id).is_dir());
}

#[tokio::test]
async fn unreadable_lifecycle_of_an_unowned_run_is_retired_whatever_the_local_error() {
    // An oversized lifecycle is an artifact error, not a parse error.
    use std::os::unix::fs::PermissionsExt;
    let data = tempdir().unwrap();
    let runtime = tempdir().unwrap();
    let run_id = "ab69f1ba-1fa2-4283-bdf1-a6d2ed59549c";
    fs::create_dir_all(data.path().join("runs").join(run_id)).unwrap();
    let metadata = data.path().join("run-metadata").join(run_id);
    fs::create_dir_all(&metadata).unwrap();
    fs::set_permissions(&metadata, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(metadata.join("lifecycle.json"), vec![b' '; 20 * 1024]).unwrap();
    let runner = NoProcess;
    let server = ObservationServer::with_disposition(Some(204), Some("unowned"));
    let executor = RecipeExecutor {
        client: &server.client,
        helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime: OciRuntime {
            runner: &runner,
            data_root: data.path(),
        },
        runtime_root: runtime.path(),
    };
    assert_eq!(complete_observation_sweep(&executor).await, 0);
    assert!(!metadata.join("lifecycle.json").exists());
    assert_fresh_run_preparation(&executor.runtime);
    server.finish();
}

#[tokio::test]
async fn known_disposition_carries_the_controller_generation() {
    let run_id = uuid::Uuid::parse_str("ab69f1ba-1fa2-4283-bdf1-a6d2ed59549c").unwrap();
    for (answer, expected) in [
        ("generation=7", Some(7)),
        ("generation=0", None),
        ("generation=junk", None),
        ("", None),
    ] {
        let server = ObservationServer::with_disposition(Some(204), Some(answer));
        assert_eq!(
            server.client.recipe_run_disposition(run_id).await.unwrap(),
            crate::client::RecipeRunDisposition::Known {
                run_generation: expected
            }
        );
        server.finish();
    }
}

#[test]
fn a_run_is_reported_once_per_process() {
    let key = "test-once/4f6c2a1e";
    assert!(first_report_of_run(key));
    assert!(!first_report_of_run(key));
}

#[test]
fn observation_report_diagnostic_preserves_only_the_safe_client_category() {
    assert_eq!(
        RecipeObservationError::Report(ClientError::Protocol).to_string(),
        "exact recipe run observation could not be reported: controller protocol response is invalid"
    );
    let error = ClientError::CredentialRead(std::io::Error::other("secret/path/token"));
    assert_eq!(
        RecipeObservationError::Report(error).to_string(),
        "exact recipe run observation could not be reported: agent credential could not be read"
    );
}

#[tokio::test]
async fn durable_partial_history_page_does_not_starve_an_unrelated_ready_claim() {
    let directory = tempdir().unwrap();
    let data = directory.path().join("data");
    for _ in 0..4097 {
        fs::create_dir_all(data.join("runs").join(Uuid::new_v4().to_string())).unwrap();
    }
    let database = directory.path().join("state.sqlite");
    let server = ObservationServer::new(Some(204));
    let runner = NoProcess;
    let mut state = StateStore::open(&database, NODE_ID).unwrap();
    let recipes = RecipeExecutor {
        client: &server.client,
        helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime_root: directory.path(),
        runtime: OciRuntime {
            runner: &runner,
            data_root: &data,
        },
    };
    let page = recipes
        .report_recipe_run_observation_page(None)
        .await
        .unwrap();
    assert!(!page.empty_snapshot_safe);
    assert!(page.checkpoint.is_some());
    state
        .save_observation_checkpoint(page.checkpoint.as_ref())
        .unwrap();
    drop(state);
    let mut state = StateStore::open(&database, NODE_ID).unwrap();
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: false,
        heartbeats: Arc::new(Mutex::new(Vec::new())),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let events = Arc::new(Mutex::new(Vec::new()));
    let executor = OrderingExecutor {
        events: events.clone(),
    };
    run_once_with_claim_hook(&client, &mut state, &executor, None, 0, None, || Ok(()))
        .await
        .unwrap();
    assert_eq!(*events.lock().unwrap(), ["execute"]);
    assert_eq!(state.observation_checkpoint().unwrap(), page.checkpoint);
    assert_eq!(client.results.lock().unwrap().len(), 1);
    assert!(
        server.finish().is_empty(),
        "partial zero history is never an empty observation report"
    );
}

#[tokio::test]
async fn ended_observation_loss_does_not_hold_the_next_claim() {
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: false,
        heartbeats: Arc::new(Mutex::new(Vec::new())),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let events = Arc::new(Mutex::new(Vec::new()));
    struct RecoveringExecutor {
        events: Arc<Mutex<Vec<&'static str>>>,
    }
    #[async_trait(?Send)]
    impl Executor for RecoveringExecutor {
        async fn observe(
            &self,
            _claim: &AgentClaim,
        ) -> Option<crate::executor::operation_observation::OperationObservation> {
            None
        }

        async fn execute(
            &self,
            _: &AgentClaim,
            _: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            _: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            let mut events = self.events.lock().unwrap();
            events.push("execute");
            if events.len() == 1 {
                temporary_runtime_observation_failure()
            } else {
                recipe_install_success(0)
            }
        }
    }
    let executor = RecoveringExecutor {
        events: events.clone(),
    };
    run_once_with_claim_hook(&client, &mut state, &executor, None, 0, None, || Ok(()))
        .await
        .unwrap();
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    *client.claim.lock().unwrap() = Some(fresh.clone());
    run_once_with_claim_hook(&client, &mut state, &executor, None, 0, None, || Ok(()))
        .await
        .unwrap();
    assert_eq!(*events.lock().unwrap(), ["execute", "execute"]);
    assert!(client.results.lock().unwrap().iter().any(|result| {
        result.fence == fresh.fence && result.state == AgentResultState::Succeeded
    }));
}

/// A serving workload that died after its start succeeded: the background
/// sweep finds it not running, asks again for its output, and the helper's
/// real process-exit answer over a real socket becomes the observation's
/// diagnostics -- the engine's own words and exit facts, without its secrets.
#[tokio::test]
async fn exited_serving_workload_carries_its_retained_output() {
    use std::io::{Read, Write};
    use vonk_agent_protocol::generated::{
        HostHelperProcessLogs, HostHelperResponse, HostHelperResponseStatus,
    };
    let temp = tempdir().unwrap();
    let socket = temp.path().join("helper.sock");
    let requests = temp.path().join("requests");
    let listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    let helper = std::thread::spawn(move || {
        let mut asked_for_output = Vec::new();
        for _ in 0..2 {
            let (mut stream, _) = listener.accept().unwrap();
            let mut prefix = [0; 4];
            stream.read_exact(&mut prefix).unwrap();
            let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
            stream.read_exact(&mut body).unwrap();
            let request: vonk_agent_protocol::RecipeRunInspectionRequest =
                vonk_agent_protocol::parse_strict(&body).unwrap();
            let with_output = request.include_logs == Some(true);
            asked_for_output.push(with_output);
            let mut response = HostHelperResponse {
                installation_intent_nonce: None,
                schema_version: 1,
                request_id: Some(request.request_id),
                status: HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
                process_running: Some(false),
                job_progress_at: None,
                exit_code: None,
                error_code: None,
                diagnostic: None,
                process_logs: None,
            };
            if with_output {
                response.status = HostHelperResponseStatus::Rejected;
                response.process_running = None;
                response.error_code = Some(HelperErrorCode::RuntimeProcessExited);
                response.diagnostic = Some("exit_code=1 exit_cause=unclassified".into());
                response.process_logs = Some(HostHelperProcessLogs {
                    stdout: crate::failure_evidence::log_tail(
                        b"INFO:     Application startup complete.\n",
                    ),
                    stderr: crate::failure_evidence::log_tail(
                        b"ERROR [core.py:612] EngineCore encountered a fatal error.\n\
                          Traceback (most recent call last):\n\
                          RuntimeError: CUDA error: an illegal memory access was encountered\n\
                          HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz0123456789\n",
                    ),
                });
            }
            let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
            stream
                .write_all(&(body.len() as u32).to_be_bytes())
                .unwrap();
            stream.write_all(&body).unwrap();
        }
        asked_for_output
    });
    let client = crate::client::AgentHttpClient::for_http_test(
        "http://127.0.0.1:9/",
        "spk_0123456789abcdef0123456789abcdef",
    );
    let boundary = HostRuntimeBoundary {
        client: &client,
        request_root: &requests,
        helper_socket: &socket,
    };
    let arguments = vec![Uuid::new_v4().to_string()];
    let observed = boundary
        .inspect_recipe_run_evidence_for_observation(arguments.clone(), false, None)
        .await
        .unwrap();
    assert!(!observed.running);
    let Err(exited) = boundary
        .inspect_recipe_run_evidence_for_observation(arguments, true, None)
        .await
    else {
        panic!("an exited container answers with its process-exit account");
    };
    assert_eq!(helper.join().unwrap(), [false, true]);

    let diagnostics = exited_workload_diagnostics(&exited).unwrap();
    diagnostics.validate().unwrap();
    assert!(
        diagnostics
            .stderr
            .text
            .contains("RuntimeError: CUDA error: an illegal memory access was encountered")
    );
    assert!(
        diagnostics
            .stderr
            .text
            .contains("EngineCore encountered a fatal error")
    );
    assert!(!diagnostics.stderr.text.contains("hf_abcdefghij"));
    assert!(
        diagnostics
            .stdout
            .text
            .contains("Application startup complete")
    );
    assert!(
        diagnostics
            .preflight
            .iter()
            .any(|property| property.name == "exit_code" && property.value == "1")
    );

    // A container already gone (recovery removed it) or a look that could not
    // happen carries nothing, so it never replaces a kept account.
    for code in [
        HelperErrorCode::RuntimeRunMissing,
        HelperErrorCode::OperationCommandFailed,
    ] {
        let missing = crate::host_runtime::HostRuntimeError::HelperRejected {
            code,
            diagnostic: None,
            process_logs: None,
        };
        assert!(exited_workload_diagnostics(&missing).is_none());
    }
}

/// A background observation names the generation of the run's own record, so
/// the helper answers for the container only when its label carries that
/// generation: another generation's container is never reported as this run.
#[tokio::test]
async fn an_observation_names_the_generation_of_its_run() {
    use std::io::{Read, Write};
    use vonk_agent_protocol::generated::{HostHelperResponse, HostHelperResponseStatus};
    let server = ObservationServer::new(Some(204));
    let root = tempdir().unwrap();
    let runtime_root = tempdir().unwrap();
    let installation = Uuid::new_v4().to_string();
    let mut plan: crate::workloads::CompiledExecutionPlan = serde_json::from_str(include_str!(
        "../../../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    let installed = root.path().join("installations").join(&installation);
    fs::create_dir_all(&installed).unwrap();
    fs::write(
        installed.join("spec.json"),
        serde_json::to_vec(&plan).unwrap(),
    )
    .unwrap();
    fs::write(
        installed.join("recipe-content.sha256"),
        &plan.identity.recipe_revision_sha256,
    )
    .unwrap();
    plan.runtime.placement.endpoint_address = Some("127.0.0.1".parse().unwrap());
    plan.security.network_mode = "bridge".parse().unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    runtime
        .prepare_start_with_inspection_identity(
            &plan,
            &installation,
            &Uuid::new_v4().to_string(),
            &plan.runtime.placement,
            &crate::oci::RecipeRunStartIdentity { run_generation: 2 },
        )
        .unwrap();
    let socket = runtime_root.path().join("helper.sock");
    let requests = runtime_root.path().join("runtime-requests");
    let listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    let helper = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut prefix = [0; 4];
        stream.read_exact(&mut prefix).unwrap();
        let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
        stream.read_exact(&mut body).unwrap();
        let request: vonk_agent_protocol::RecipeRunInspectionRequest =
            vonk_agent_protocol::parse_strict(&body).unwrap();
        let inspected: vonk_agent_protocol::HostRuntimeRequest = serde_json::from_slice(
            &fs::read(requests.join(format!("{}.json", request.request_sha256))).unwrap(),
        )
        .unwrap();
        let response = HostHelperResponse {
            installation_intent_nonce: None,
            schema_version: 1,
            request_id: Some(request.request_id),
            status: HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
            process_running: Some(true),
            job_progress_at: None,
            exit_code: None,
            error_code: None,
            diagnostic: None,
            process_logs: None,
        };
        let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
        stream
            .write_all(&(body.len() as u32).to_be_bytes())
            .unwrap();
        stream.write_all(&body).unwrap();
        inspected.run_generation
    });
    let executor = RecipeExecutor {
        client: &server.client,
        helper_socket: &socket,
        runtime,
        runtime_root: runtime_root.path(),
    };
    assert_eq!(
        executor
            .report_exact_recipe_run_observations()
            .await
            .unwrap(),
        1
    );
    assert_eq!(helper.join().unwrap(), Some(2));
    let reports = server.finish();
    assert_eq!(reports[0]["runs"][0]["run_generation"], 2);
}
