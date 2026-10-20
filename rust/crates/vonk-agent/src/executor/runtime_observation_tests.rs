#![cfg(test)]
//! A restarted idle agent observes the effect behind every runtime custody
//! seam, without executing a workload or treating the probe as agent contact.
use super::test_support::*;
use super::*;
use std::io::{BufRead, BufReader};
use std::os::unix::net::UnixListener;
use std::process::{Command, Stdio};
use vonk_agent_protocol::generated::{HostHelperResponse, HostHelperResponseStatus};

pub(super) fn runtime_claims() -> Vec<AgentClaim> {
    let mut job: AgentClaim = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    job.deadline = (Utc::now() + ChronoDuration::minutes(5)).fixed_offset();
    let AgentClaimPayload::RecipeJobRunRequest(request) = &job.payload else {
        panic!("job");
    };
    let stop = exact_stop_plan_from_claim(&job, &request.run_id.to_string(), true).unwrap();
    let mut stopped = job.clone();
    stopped.operation = AgentOperation::RecipeStop;
    stopped.payload = AgentClaimPayload::RecipeStopPayload(stop);
    let mut start = claim();
    start.deadline = job.deadline;
    let AgentClaimPayload::RecipeInstallPayload(install) = &start.payload else {
        panic!("install");
    };
    let mut plan = serde_json::to_value(&install.compiled_execution_plan).unwrap();
    plan["topology"]["name"] = json!("dual");
    plan["topology"]["node_count"] = json!(2);
    plan["runtime"]["placement"]["world_size"] = json!(2);
    plan["runtime"]["placement"]["master_address"] = json!("192.168.100.10");
    plan["runtime"]["placement"]["master_port"] = json!(29500);
    plan["runtime"]["placement"]["local_address"] = json!("192.168.100.10");
    plan["runtime"]["placement"]["endpoint_address"] = json!("192.168.100.10");
    start.operation = AgentOperation::RecipeStart;
    start.payload = serde_json::from_value(json!({
        "run_id": request.run_id, "installation_id": request.installation_id,
        "recipe_revision_id": request.recipe_revision_id, "mapping_id": request.mapping_id,
        "run_generation": request.run_generation, "plan_digest": request.plan_digest,
        "compiled_execution_plan": plan, "phase": RecipeStartPhase::RankLaunch,
        "start_deadline": start.deadline.to_rfc3339(),
    }))
    .unwrap();
    vec![start, stopped, job]
}

fn completion(accepted: &AgentClaim) -> ExecutionResult {
    match RecipeOperationRequest::parse(accepted).unwrap() {
        RecipeOperationRequest::Start(start) => recipe_start_success(&start),
        RecipeOperationRequest::Stop(_) => ExecutionResult::done(RecipeStopResult::default()),
        RecipeOperationRequest::JobRun(job) => ExecutionResult::done(job_receipt(
            &job,
            0,
            Instant::now(),
            empty_job_output_manifest(),
            None,
        )),
        _ => unreachable!(),
    }
}

#[test]
fn runtime_death_child() {
    let Ok(root) = std::env::var("VONK_RUNTIME_DEATH_ROOT") else {
        return;
    };
    let root = Path::new(&root);
    let accepted: AgentClaim =
        vonk_agent_protocol::parse_strict(&fs::read(root.join("claim.json")).unwrap()).unwrap();
    let point: u8 = std::env::var("VONK_RUNTIME_DEATH_POINT")
        .unwrap()
        .parse()
        .unwrap();
    let mut store = StateStore::open(&root.join("state.sqlite"), NODE_ID).unwrap();
    store.begin(&accepted, Utc::now()).unwrap();
    if point > 0 {
        // The independent runtime survives this process. Its test helper reads
        // this durable marker, never the agent's SQLite running/completed word.
        fs::write(
            root.join("effect"),
            canonical_json(&accepted.payload).unwrap(),
        )
        .unwrap();
        if let AgentClaimPayload::RecipeStartPayload(start) = &accepted.payload {
            let mut installation = accepted.clone();
            installation.operation = AgentOperation::RecipeInstall;
            installation.payload = serde_json::from_value(json!({
                "installation_id": start.installation_id, "plan_digest": start.plan_digest,
                "expected_bytes": 0, "compiled_execution_plan": start.compiled_execution_plan,
            }))
            .unwrap();
            super::content_observation_tests::prepare_effect(root, &installation);
        }
        if let AgentClaimPayload::RecipeJobRunRequest(job) = &accepted.payload {
            let scope = root.join("runs").join(job.job_id.to_string());
            fs::create_dir_all(scope.join("outputs")).unwrap();
            OciRuntime {
                runner: &NoProcess,
                data_root: root,
            }
            .persist_job_intent(job)
            .unwrap();
        }
    }
    if point > 1 {
        store.finish(&accepted, completion(&accepted)).unwrap();
    }
    if point > 2 {
        fs::write(
            root.join("received.json"),
            canonical_json(&store.pending_results().unwrap().remove(0).1).unwrap(),
        )
        .unwrap();
    }
    println!("runtime-ready");
    std::io::stdout().flush().unwrap();
    let mut input = String::new();
    std::io::stdin().read_line(&mut input).unwrap();
    panic!("parent kills the owned child");
}

#[tokio::test]
async fn runtime_process_death_at_every_custody_seam_recovers_without_new_claims() {
    for accepted in runtime_claims() {
        accepted.validate().unwrap();
        let seams = (0..4).map(|point| (point, false));
        // An exit during stability is the same measured exit, not an unread
        // probe that loses its cause until another idle tick.
        let stability_exit =
            (accepted.operation == AgentOperation::RecipeStart).then_some((1, true));
        for (point, exits_during_stability) in seams.chain(stability_exit) {
            let dir = tempdir().unwrap();
            let root = dir.path();
            fs::write(root.join("claim.json"), canonical_json(&accepted).unwrap()).unwrap();
            let mut child = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "executor::runtime_observation_tests::runtime_death_child",
                    "--nocapture",
                ])
                .env("VONK_RUNTIME_DEATH_ROOT", root)
                .env("VONK_RUNTIME_DEATH_POINT", point.to_string())
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()
                .unwrap();
            let (ready, received) = std::sync::mpsc::channel();
            let stdout = child.stdout.take().unwrap();
            let reader = thread::spawn(move || {
                ready
                    .send(
                        BufReader::new(stdout)
                            .lines()
                            .any(|line| line.is_ok_and(|line| line.contains("runtime-ready"))),
                    )
                    .unwrap();
            });
            let reached = received.recv_timeout(Duration::from_secs(20));
            child.kill().unwrap();
            let deadline = Instant::now() + Duration::from_secs(10);
            while child.try_wait().unwrap().is_none() || !reader.is_finished() {
                assert!(
                    Instant::now() < deadline,
                    "owned child must exit after kill"
                );
                thread::sleep(Duration::from_millis(1));
            }
            drop(reader);
            assert!(reached.unwrap(), "runtime child must reach seam");
            let socket = root.join("helper.sock");
            let listener = UnixListener::bind(&socket).unwrap();
            listener.set_nonblocking(true).unwrap();
            let peer_root = root.to_owned();
            let operation = accepted.operation;
            let stop = Arc::new(AtomicBool::new(false));
            let done = stop.clone();
            let peer = thread::spawn(move || {
                let deadline = Instant::now() + Duration::from_secs(20);
                let mut inspections = 0;
                while !done.load(Ordering::SeqCst) {
                    assert!(Instant::now() < deadline);
                    let (mut stream, _) = match listener.accept() {
                        Ok(connection) => connection,
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(1));
                            continue;
                        }
                        Err(error) => panic!("helper: {error}"),
                    };
                    stream
                        .set_read_timeout(Some(Duration::from_secs(2)))
                        .unwrap();
                    let mut prefix = [0; 4];
                    stream.read_exact(&mut prefix).unwrap();
                    let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
                    stream.read_exact(&mut body).unwrap();
                    let frame: vonk_agent_protocol::RecipeRunInspectionRequest =
                        vonk_agent_protocol::parse_strict(&body)
                            .expect("only read-only inspection");
                    let request: vonk_agent_protocol::HostRuntimeRequest =
                        vonk_agent_protocol::parse_strict(
                            &fs::read(
                                peer_root
                                    .join("runtime-requests")
                                    .join(format!("{}.json", frame.request_sha256)),
                            )
                            .unwrap(),
                        )
                        .unwrap();
                    assert_eq!(request.action, HostRuntimeAction::RunInspect);
                    assert_eq!(request.arguments.len(), 6);
                    let effect = peer_root.join("effect").exists();
                    let absent = if operation == AgentOperation::RecipeStop {
                        effect
                    } else {
                        !effect
                    };
                    inspections += 1;
                    let rank_exited = exits_during_stability && inspections > 1;
                    let response = HostHelperResponse {
                        schema_version: 1,
                        request_id: Some(frame.request_id),
                        status: if absent {
                            HostHelperResponseStatus::Rejected
                        } else {
                            HostHelperResponseStatus::ContainerRuntimeRequestExecuted
                        },
                        error_code: absent.then_some(HelperErrorCode::RuntimeRunMissing),
                        process_running: (!absent)
                            .then_some(operation != AgentOperation::RecipeJobRunV1 && !rank_exited),
                        exit_code: if rank_exited {
                            Some(7)
                        } else {
                            (!absent && operation == AgentOperation::RecipeJobRunV1).then_some(0)
                        },
                        installation_intent_nonce: None,
                        job_progress_at: None,
                        diagnostic: None,
                        process_logs: None,
                    };
                    let body = canonical_json(&response).unwrap();
                    stream
                        .write_all(&(body.len() as u32).to_be_bytes())
                        .unwrap();
                    stream.write_all(&body).unwrap();
                }
            });
            let http = AgentHttpClient::for_http_test("http://127.0.0.1:1/", NODE_ID);
            let executor = RecipeExecutor {
                client: &http,
                helper_socket: &socket,
                runtime: OciRuntime {
                    runner: &NoProcess,
                    data_root: root,
                },
                runtime_root: root,
            };
            let client = RecordingClient {
                cancel_requested: false,
                claim: Arc::new(Mutex::new(None)),
                fail_heartbeat: false,
                heartbeats: Arc::default(),
                results: Arc::default(),
            };
            let mut state =
                StateStore::open_recovered(&root.join("state.sqlite"), NODE_ID).unwrap();
            run_once(&client, &mut state, &executor, None, 0, None)
                .await
                .unwrap();
            stop.store(true, Ordering::SeqCst);
            tokio::time::timeout(
                Duration::from_secs(20),
                tokio::task::spawn_blocking(move || std::thread::JoinHandle::join(peer)),
            )
            .await
            .unwrap()
            .unwrap()
            .unwrap();
            assert!(
                client.heartbeats.lock().unwrap().is_empty(),
                "observation never renews authority"
            );
            let results = client.results.lock().unwrap();
            if point == 0 {
                assert!(
                    results.is_empty(),
                    "missing receipt is not restart failure or renewed execution"
                );
                assert_eq!(state.observation_claims().unwrap().len(), 1);
            } else {
                assert_eq!(results.len(), 1);
                assert_eq!(
                    results[0].state,
                    if exits_during_stability {
                        AgentResultState::Failed
                    } else {
                        AgentResultState::Succeeded
                    }
                );
                assert_eq!(results[0].fence, accepted.fence);
                if point == 3 {
                    let sent: AgentResult = vonk_agent_protocol::parse_strict(
                        &fs::read(root.join("received.json")).unwrap(),
                    )
                    .unwrap();
                    assert_eq!(results[0], sent, "lost ack must replay identical receipt");
                }
            }
        }
    }
}
