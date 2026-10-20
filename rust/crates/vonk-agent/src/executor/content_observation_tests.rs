#![cfg(test)]
//! Wrong implementation: restart converts a completed effect to interrupted,
//! or an idle tick reexecutes it because the receipt/ack was lost.
use super::content_observation::*;
use super::run_once;
use super::test_support::*;
use std::io::{BufRead, BufReader};
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Stdio};
struct UnavailableProcess;
impl ProcessRunner for UnavailableProcess {
    fn run(&self, _: Program, _: &[String], _: Duration) -> Result<ProcessOutput, ProcessError> {
        Err(ProcessError::Timeout)
    }
}

fn prepare_effect(root: &Path, claim: &AgentClaim) {
    let AgentClaimPayload::RecipeInstallPayload(request) = &claim.payload else {
        panic!("install fixture");
    };
    let source = root.join("distribution/models");
    fs::create_dir_all(&source).unwrap();
    for artifact in &request.compiled_execution_plan.artifacts {
        let path = source.join(&artifact.sha256);
        fs::write(&path, vec![0; artifact.size_bytes as usize]).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root,
    };
    runtime
        .install(
            &request.compiled_execution_plan,
            &request.installation_id.to_string(),
            &request
                .compiled_execution_plan
                .identity
                .recipe_revision_sha256,
        )
        .unwrap();
}

#[test]
fn content_death_child() {
    let Ok(root) = std::env::var("VONK_CONTENT_DEATH_ROOT") else {
        return;
    };
    let root = Path::new(&root);
    let claim: AgentClaim =
        vonk_agent_protocol::parse_strict(&fs::read(root.join("claim.json")).unwrap()).unwrap();
    let point: u8 = std::env::var("VONK_CONTENT_DEATH_POINT")
        .unwrap()
        .parse()
        .unwrap();
    let mut store = StateStore::open(&root.join("state.sqlite"), NODE_ID).unwrap();
    assert_eq!(
        store.begin(&claim, Utc::now()).unwrap(),
        BeginDecision::Execute
    );
    if point > 0 {
        prepare_effect(root, &claim);
    }
    if point > 1 {
        store.finish(&claim, recipe_install_success(0)).unwrap();
    }
    if point > 2 {
        let result = store.pending_results().unwrap().remove(0).1;
        // The receiving peer has the bytes; no local ack has been committed.
        fs::write(
            root.join("received.json"),
            vonk_agent_protocol::canonical_json(&result).unwrap(),
        )
        .unwrap();
    }
    println!("content-ready");
    std::io::stdout().flush().unwrap();
    let mut line = String::new();
    std::io::stdin().read_line(&mut line).unwrap();
    panic!("parent must kill the child at the requested boundary");
}

#[tokio::test]
async fn process_death_at_four_boundaries_recovers_on_an_idle_tick_without_execution() {
    for point in 0..4 {
        let directory = tempdir().unwrap();
        let root = directory.path();
        let claim = claim();
        fs::write(
            root.join("claim.json"),
            vonk_agent_protocol::canonical_json(&claim).unwrap(),
        )
        .unwrap();
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "executor::content_observation_tests::content_death_child",
                "--nocapture",
            ])
            .env("VONK_CONTENT_DEATH_ROOT", root)
            .env("VONK_CONTENT_DEATH_POINT", point.to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(20);
        let stdout = child.stdout.take().unwrap();
        let (ready, observed) = std::sync::mpsc::channel();
        let _reader = thread::spawn(move || {
            let found = BufReader::new(stdout)
                .lines()
                .any(|line| line.is_ok_and(|text| text.contains("content-ready")));
            let _ = ready.send(found);
        });
        let reached = observed.recv_timeout(deadline.saturating_duration_since(Instant::now()));
        let _ = child.kill();
        while child.try_wait().unwrap().is_none() {
            assert!(Instant::now() < deadline, "child must exit after kill");
            thread::sleep(Duration::from_millis(1));
        }
        assert!(reached.unwrap(), "child must reach fault boundary");
        let http = AgentHttpClient::for_http_test("http://127.0.0.1/", NODE_ID);
        let executor = RecipeExecutor {
            client: &http,
            helper_socket: Path::new("unused"),
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
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let mut state = StateStore::open_recovered(&root.join("state.sqlite"), NODE_ID).unwrap();
        run_once(&client, &mut state, &executor, None, 0, None)
            .await
            .unwrap();
        assert!(
            client.heartbeats.lock().unwrap().is_empty(),
            "a probe cannot renew a lease"
        );
        let results = client.results.lock().unwrap();
        if point == 0 {
            assert!(
                results.is_empty(),
                "missing effect is unknown, not restart failure or completion"
            );
            assert_eq!(state.observation_claims().unwrap().len(), 1);
            assert!(
                !root.join("installations").exists(),
                "observer cannot install"
            );
        } else {
            assert_eq!(results.len(), 1);
            assert_eq!(results[0].fence, claim.fence);
            assert_eq!(results[0].state, AgentResultState::Succeeded);
            assert!(state.observation_claims().unwrap().is_empty());
            assert!(state.pending_results().unwrap().is_empty());
            if point == 3 {
                let sent: AgentResult = vonk_agent_protocol::parse_strict(
                    &fs::read(root.join("received.json")).unwrap(),
                )
                .unwrap();
                assert_eq!(results[0], sent, "lost ack replays the same receipt");
            }
        }
    }
}

#[tokio::test]
async fn every_content_kind_reaches_its_real_probe_and_preserves_unknown() {
    // Enum exhaustiveness catches new kinds; invocation catches a registered
    // placeholder which returns no observation instead of probing its owner.
    let root = tempdir().unwrap();
    let http = AgentHttpClient::for_http_test("http://127.0.0.1:1/", NODE_ID);
    let executor = RecipeExecutor {
        client: &http,
        helper_socket: &root.path().join("missing-helper"),
        runtime: OciRuntime {
            runner: &UnavailableProcess,
            data_root: root.path(),
        },
        runtime_root: root.path(),
    };
    let base = claim();
    let AgentClaimPayload::RecipeInstallPayload(install) = &base.payload else {
        panic!("install");
    };
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-build-claim-v1.json"
    ))
    .unwrap();
    let mut build = base.clone();
    build.operation = AgentOperation::RecipeBuildV1;
    build.payload = serde_json::from_value(vector["base_payload"].clone()).unwrap();
    build.validate().unwrap();
    let payloads = [
        (
            AgentOperation::RuntimePreflightV1,
            serde_json::json!({"source_build": false, "minimum_free_bytes": 0, "fabric_connectivity": "none", "fabric_minimum_mbps": 0}),
        ),
        (
            AgentOperation::ArtifactDistributionV1,
            serde_json::json!({"plan_digest": "a".repeat(64)}),
        ),
        (
            AgentOperation::RecipeBuildCleanupV1,
            serde_json::json!({"build_id": Uuid::new_v4(), "operation_id": Uuid::new_v4()}),
        ),
        (
            AgentOperation::RecipeInstall,
            serde_json::to_value(&base.payload).unwrap(),
        ),
        (
            AgentOperation::RecipeReconcile,
            serde_json::json!({"installation_id": install.installation_id, "plan_digest": "b".repeat(64)}),
        ),
        (
            AgentOperation::RecipeUninstall,
            serde_json::json!({"installation_id": install.installation_id, "plan_digest": "b".repeat(64), "recipe_content_sha256": "c".repeat(64)}),
        ),
    ];
    for (operation, payload) in payloads {
        let mut accepted = base.clone();
        accepted.operation = operation;
        accepted.payload = serde_json::from_value(payload).unwrap();
        accepted.validate().unwrap();
        let result = executor
            .observe(&accepted)
            .await
            .expect("real observer for content kind");
        assert!(
            !matches!(result, ContentObservation::Confirmed(_)),
            "absent evidence cannot complete {operation}"
        );
    }
    assert!(executor.observe(&build).await.is_some());
}
