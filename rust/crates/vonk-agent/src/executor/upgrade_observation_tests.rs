#![cfg(test)]
//! Wrong implementation: a restart loses upgrade custody, absence completes an
//! upgrade, or a lost handoff response causes another installation.
use super::operation_observation::{OperationObservation, reobserve_operations};
use super::test_support::*;
use super::upgrade_observation::{UpgradeObservation, observe_upgrade};
use super::*;
use std::io::{BufRead, BufReader};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::process::{Command, Stdio};
use vonk_agent_protocol::generated::{
    AgentRuntimeIdentityArchitecture, AgentUpgradePayload, PackageActivationOutcome as Outcome,
};
use vonk_agent_protocol::{
    PackageActivationPhase as Phase, PackageActivationReceipt, PackageRollbackAuthority,
    PackageRollbackSource,
};

pub(super) fn upgrade_claim() -> AgentClaim {
    let mut accepted = claim();
    accepted.operation = AgentOperation::AgentUpgradeV1;
    accepted.deadline = (Utc::now() + ChronoDuration::minutes(5)).fixed_offset();
    accepted.payload = AgentClaimPayload::AgentUpgradePayload(AgentUpgradePayload {
        schema_version: 1,
        architecture: AgentRuntimeIdentityArchitecture::LinuxArm64.to_string(),
        package_bytes: 1000,
        package_sha256: "a".repeat(64),
        package_signature: "b".repeat(128),
        package_url: "https://install.vonkforge.ai/test/vonk-forge-agent.deb".into(),
        package_version: "0.2.0".into(),
        target_binary_digest: "c".repeat(64),
        target_build_digest: format!("sha256:{}", "d".repeat(64)),
        source_package_url: "https://install.vonkforge.ai/source/vonk-forge-agent.deb".into(),
        source_package_bytes: 1000,
        rollback: PackageRollbackAuthority {
            source: PackageRollbackSource {
                package_sha256: "e".repeat(64),
                package_signature: "f".repeat(128),
                package_version: "0.1.0".into(),
                binary_sha256: "1".repeat(64),
                helper_sha256: "2".repeat(64),
            },
            attempt_nonce: "3".repeat(64),
            activation_deadline: 2_000_000_000,
        },
    });
    accepted.validate().unwrap();
    accepted
}

fn receipt(accepted: &AgentClaim, phase: Phase) -> PackageActivationReceipt {
    let request = vonk_agent_protocol::AgentUpgradeRequest::parse(accepted).unwrap();
    PackageActivationReceipt {
        schema_version: 2,
        node_id: NODE_ID.into(),
        source_package_sha256: request.rollback.source.package_sha256,
        source_version: request.rollback.source.package_version,
        source_binary_sha256: request.rollback.source.binary_sha256,
        candidate_package_sha256: request.package_sha256,
        candidate_version: request.package_version,
        candidate_binary_sha256: request.target_binary_digest,
        attempt_nonce: request.rollback.attempt_nonce,
        phase,
        created_at: 100,
        updated_at: 101,
        outcome: match phase {
            Phase::Acknowledged => Outcome::ControllerConfirmedActivation,
            Phase::RolledBack => Outcome::SourceRestoredAndRestarted,
            Phase::Armed | Phase::ActivationFailed | Phase::RollingBack | Phase::RollbackFailed => {
                Outcome::AwaitingControllerActivation
            }
        },
    }
}

fn identity(accepted: &AgentClaim) -> AgentRuntimeIdentity {
    let request = vonk_agent_protocol::AgentUpgradeRequest::parse(accepted).unwrap();
    AgentRuntimeIdentity {
        semantic_version: request.package_version,
        build_digest: request.target_build_digest,
        binary_digest: request.target_binary_digest,
        architecture: AgentRuntimeIdentityArchitecture::LinuxArm64,
        package_activation: None,
    }
}

fn publish(root: &Path, receipt: &PackageActivationReceipt) {
    let temporary = root.join("receipt.tmp");
    fs::write(&temporary, canonical_json(receipt).unwrap()).unwrap();
    fs::set_permissions(&temporary, fs::Permissions::from_mode(0o644)).unwrap();
    File::open(&temporary).unwrap().sync_all().unwrap();
    fs::rename(temporary, root.join("activation.json")).unwrap();
    File::open(root).unwrap().sync_all().unwrap();
}

fn observe(
    root: &Path,
    accepted: &AgentClaim,
    identity: &AgentRuntimeIdentity,
) -> OperationObservation {
    // The fixture owns the same protected file contract under its test uid;
    // production always requires uid 0. No fake in-memory receipt reader.
    observe_upgrade(
        accepted,
        Some(UpgradeObservation {
            node_id: NODE_ID,
            identity,
            receipt: crate::package_activation::read_fixture_receipt(
                &root.join("activation.json"),
                fs::metadata(root).unwrap().uid(),
            ),
        }),
    )
}

struct UpgradeProbe<'a> {
    root: &'a Path,
    recipes: RecipeExecutor<'a, NoProcess>,
    identity: AgentRuntimeIdentity,
}
#[async_trait(?Send)]
impl Executor for UpgradeProbe<'_> {
    async fn observe(&self, accepted: &AgentClaim) -> Option<OperationObservation> {
        Some(
            self.recipes
                .observe_operation(
                    accepted,
                    Some(UpgradeObservation {
                        node_id: NODE_ID,
                        identity: &self.identity,
                        receipt: crate::package_activation::read_fixture_receipt(
                            &self.root.join("activation.json"),
                            fs::metadata(self.root).unwrap().uid(),
                        ),
                    }),
                )
                .await,
        )
    }
    async fn execute(
        &self,
        _: &AgentClaim,
        _: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        _: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        panic!("observing a package handoff must never install a package")
    }
}

#[test]
fn upgrade_death_child() {
    let Ok(root) = std::env::var("VONK_UPGRADE_DEATH_ROOT") else {
        return;
    };
    let root = Path::new(&root);
    let accepted: AgentClaim =
        vonk_agent_protocol::parse_strict(&fs::read(root.join("claim.json")).unwrap()).unwrap();
    let point: u8 = std::env::var("VONK_UPGRADE_DEATH_POINT")
        .unwrap()
        .parse()
        .unwrap();
    let mut store = StateStore::open(&root.join("state.sqlite"), NODE_ID).unwrap();
    store.begin(&accepted, Utc::now()).unwrap();
    if point > 0 {
        publish(root, &receipt(&accepted, Phase::Acknowledged));
    }
    if point > 1 {
        let OperationObservation::Confirmed(result) =
            observe(root, &accepted, &identity(&accepted))
        else {
            panic!("exact activation");
        };
        store.finish(&accepted, *result).unwrap();
    }
    if point > 2 {
        fs::write(
            root.join("received.json"),
            canonical_json(&store.pending_results().unwrap().remove(0).1).unwrap(),
        )
        .unwrap();
    }
    println!("upgrade-ready");
    std::io::stdout().flush().unwrap();
    let mut line = String::new();
    std::io::stdin().read_line(&mut line).unwrap();
    panic!("parent kills this owned child at the custody seam");
}

#[tokio::test]
async fn upgrade_process_death_at_every_custody_seam_reobserves_without_installing() {
    for point in 0..4 {
        let directory = tempdir().unwrap();
        let root = directory.path();
        let accepted = upgrade_claim();
        fs::write(root.join("claim.json"), canonical_json(&accepted).unwrap()).unwrap();
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "executor::upgrade_observation_tests::upgrade_death_child",
                "--nocapture",
            ])
            .env("VONK_UPGRADE_DEATH_ROOT", root)
            .env("VONK_UPGRADE_DEATH_POINT", point.to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let (ready, received) = std::sync::mpsc::channel();
        let stdout = child.stdout.take().unwrap();
        let reader = thread::spawn(move || {
            let _ = ready.send(
                BufReader::new(stdout)
                    .lines()
                    .any(|line| line.is_ok_and(|line| line.contains("upgrade-ready"))),
            );
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
        assert!(reached.unwrap(), "child must reach requested seam");
        let http = AgentHttpClient::for_http_test("http://127.0.0.1:1/", NODE_ID);
        let executor = UpgradeProbe {
            root,
            identity: identity(&accepted),
            recipes: RecipeExecutor {
                client: &http,
                helper_socket: root,
                runtime: OciRuntime {
                    runner: &NoProcess,
                    data_root: root,
                },
                runtime_root: root,
            },
        };
        let client = RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(None)),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let mut store = StateStore::open_recovered(&root.join("state.sqlite"), NODE_ID).unwrap();
        run_once(&client, &mut store, &executor, None, 0, None)
            .await
            .unwrap();
        if point == 0 {
            assert!(
                client.results.lock().unwrap().is_empty(),
                "disappearance cannot complete an upgrade"
            );
            assert_eq!(store.observation_claims().unwrap().len(), 1);
            assert!(
                matches!(store.begin(&accepted, Utc::now()), Err(StateError::Busy)),
                "same fence cannot reinstall"
            );
            publish(root, &receipt(&accepted, Phase::Acknowledged));
            run_once(&client, &mut store, &executor, None, 0, None)
                .await
                .unwrap();
        }
        assert!(
            client.heartbeats.lock().unwrap().is_empty(),
            "observation is not agent contact or lease renewal"
        );
        let results = client.results.lock().unwrap();
        assert_eq!(results.len(), 1);
        assert!(matches!(
            results[0].result,
            AgentResultResult::OutcomeDone(_)
        ));
        if point == 3 {
            let sent: AgentResult =
                vonk_agent_protocol::parse_strict(&fs::read(root.join("received.json")).unwrap())
                    .unwrap();
            assert_eq!(
                results[0], sent,
                "lost acknowledgement replays identical custody"
            );
        }
    }
}

#[test]
fn package_observation_requires_exact_transaction_and_running_identity() {
    let root = tempdir().unwrap();
    let accepted = upgrade_claim();
    let running = identity(&accepted);
    let exact = receipt(&accepted, Phase::Acknowledged);
    for changed in 0..11 {
        let mut wrong = exact.clone();
        match changed {
            0 => wrong.node_id = format!("spk_{}", "4".repeat(32)),
            1 => wrong.attempt_nonce = "4".repeat(64),
            2 => wrong.source_package_sha256 = "4".repeat(64),
            3 => wrong.source_version = "0.0.1".into(),
            4 => wrong.source_binary_sha256 = "4".repeat(64),
            5 => wrong.candidate_package_sha256 = "4".repeat(64),
            6 => wrong.candidate_version = "0.3.0".into(),
            7 => wrong.candidate_binary_sha256 = "4".repeat(64),
            8 => {
                wrong.created_at = 2_000_000_001;
                wrong.updated_at = wrong.created_at;
            }
            9 => wrong.updated_at = 2_000_000_001,
            10 => wrong.phase = Phase::Armed,
            _ => unreachable!(),
        }
        publish(root.path(), &wrong);
        let OperationObservation::Progressing(wait) = observe(root.path(), &accepted, &running)
        else {
            panic!("mismatched/unconfirmed activation cannot complete");
        };
        assert!(!wait.reason.is_empty());
        assert!(wait.next_check > Utc::now());
        assert_eq!(wait.deadline, accepted.deadline);
    }
    publish(root.path(), &exact);
    for changed in 0..3 {
        let mut wrong = running.clone();
        match changed {
            0 => wrong.binary_digest = "4".repeat(64),
            1 => wrong.build_digest = format!("sha256:{}", "4".repeat(64)),
            2 => wrong.architecture = AgentRuntimeIdentityArchitecture::LinuxAmd64,
            _ => unreachable!(),
        }
        assert!(matches!(
            observe(root.path(), &accepted, &wrong),
            OperationObservation::Progressing(_)
        ));
    }
    assert!(matches!(
        observe(root.path(), &accepted, &running),
        OperationObservation::Confirmed(_)
    ));
}

#[tokio::test]
async fn rollback_and_expired_unknown_preserve_evidence_and_recover_on_idle_tick() {
    let root = tempdir().unwrap();
    let accepted = upgrade_claim();
    let mut running = identity(&accepted);
    for phase in [
        Phase::ActivationFailed,
        Phase::RollingBack,
        Phase::RollbackFailed,
        Phase::RolledBack,
    ] {
        publish(root.path(), &receipt(&accepted, phase));
        assert!(matches!(
            observe(root.path(), &accepted, &running),
            OperationObservation::Progressing(_)
        ));
    }
    running.binary_digest = "1".repeat(64);
    let OperationObservation::Confirmed(result) = observe(root.path(), &accepted, &running) else {
        panic!("exact rollback must settle");
    };
    let finished = result.finish_for(&accepted.operation);
    let AgentResultResult::OutcomeFailed(failure) = finished.result else {
        panic!("rollback is not upgrade success");
    };
    assert_eq!(
        failure.evidence.unwrap().package_activation.unwrap(),
        receipt(&accepted, Phase::RolledBack)
    );
    fs::write(root.path().join("activation.json"), b"damaged").unwrap();
    assert!(matches!(
        observe(root.path(), &accepted, &running),
        OperationObservation::Progressing(_)
    ));
    let http = AgentHttpClient::for_http_test("http://127.0.0.1:1/", NODE_ID);
    let executor = UpgradeProbe {
        root: root.path(),
        identity: identity(&accepted),
        recipes: RecipeExecutor {
            client: &http,
            helper_socket: root.path(),
            runtime: OciRuntime {
                runner: &NoProcess,
                data_root: root.path(),
            },
            runtime_root: root.path(),
        },
    };
    let database = root.path().join("state.sqlite");
    let mut store = StateStore::open(&database, NODE_ID).unwrap();
    store.begin(&accepted, Utc::now()).unwrap();
    reobserve_operations(
        &mut store,
        &executor,
        None,
        accepted.deadline.with_timezone(&Utc),
    )
    .await
    .unwrap();
    // Past the claim's lease, within its activation deadline: the helper's
    // watcher still owns the transaction, so no unknown is recorded and the
    // claim stays observed.
    assert!(store.pending_results().unwrap().is_empty());
    assert_eq!(store.observation_claims().unwrap().len(), 1);
    drop(store);
    publish(root.path(), &receipt(&accepted, Phase::Acknowledged));
    let mut store = StateStore::open_recovered(&database, NODE_ID).unwrap();
    reobserve_operations(&mut store, &executor, None, Utc::now())
        .await
        .unwrap();
    assert!(matches!(
        store.pending_results().unwrap()[0].1.result,
        AgentResultResult::OutcomeDone(_)
    ));
}

#[test]
fn an_unconfirmed_transaction_past_its_activation_deadline_is_unknown() {
    let root = tempdir().unwrap();
    let mut accepted = upgrade_claim();
    let AgentClaimPayload::AgentUpgradePayload(payload) = &mut accepted.payload else {
        unreachable!()
    };
    payload.rollback.activation_deadline = Utc::now().timestamp() - 1;
    let running = identity(&accepted);
    assert!(matches!(
        observe(root.path(), &accepted, &running),
        OperationObservation::Unknown(_)
    ));
}
