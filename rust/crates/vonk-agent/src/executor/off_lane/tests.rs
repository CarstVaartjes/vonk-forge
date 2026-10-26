#![cfg(test)]

use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
};

use chrono::Duration as ChronoDuration;
use tempfile::tempdir;
use vonk_agent_protocol::generated::{AgentResultResult, AgentResultState};
use vonk_agent_protocol::runtime_preflight::RuntimePreflightResult;

use super::super::test_support::{NODE_ID, claim};
use super::*;

/// The Controller as the lane sees it: it hands out the queued orders in turn
/// and records every renewal and result.
#[derive(Clone, Default)]
struct QueueClient {
    queued: Arc<Mutex<VecDeque<AgentClaim>>>,
    renewed: Arc<Mutex<Vec<uuid::Uuid>>>,
    results: Arc<Mutex<Vec<AgentResult>>>,
}

#[async_trait]
impl LoopClient for QueueClient {
    async fn claim(
        &self,
        _preflight_fingerprint: Option<&str>,
        _wait_seconds: u64,
        _runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        Ok(self.queued.lock().unwrap().pop_front())
    }

    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        self.renewed.lock().unwrap().push(progress.fence);
        Ok(AgentDirective {
            cancel_requested: false,
            deadline: (Utc::now() + ChronoDuration::seconds(30)).fixed_offset(),
            fence: progress.fence,
        })
    }

    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.results.lock().unwrap().push(result.clone());
        Ok(())
    }
}

/// A build that compiles until it is released; every other order is quick.
struct CompilingExecutor {
    release: tokio::sync::Notify,
}

#[async_trait(?Send)]
impl Executor for CompilingExecutor {
    async fn observe(
        &self,
        _claim: &AgentClaim,
    ) -> Option<operation_observation::OperationObservation> {
        panic!("an executing order is settled by its executor, never probed")
    }

    async fn execute(
        &self,
        claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        _cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        match claim.operation {
            AgentOperation::RecipeBuildV1 => {
                self.release.notified().await;
                ExecutionResult::done(vonk_agent_protocol::RecipeBuildEvidence {
                    image_bytes: 1,
                    image_digest: format!("sha256:{}", "a".repeat(64)),
                    oci_layout_sha256: "b".repeat(64),
                })
            }
            AgentOperation::RuntimePreflightV1 => ExecutionResult::done(RuntimePreflightResult {
                fingerprint: "a".repeat(64),
                observed_at: 1,
                findings: Vec::new(),
            }),
            other => panic!("unexpected order {other}"),
        }
    }
}

fn order(operation: AgentOperation, payload: serde_json::Value) -> AgentClaim {
    let mut order = claim();
    order.fence = uuid::Uuid::new_v4();
    order.operation = operation;
    order.payload = serde_json::from_value(payload).unwrap();
    order.validate().unwrap();
    order
}

fn build_order() -> AgentClaim {
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-build-claim-v1.json"
    ))
    .unwrap();
    order(
        AgentOperation::RecipeBuildV1,
        vector["base_payload"].clone(),
    )
}

fn probe_order() -> AgentClaim {
    order(
        AgentOperation::RuntimePreflightV1,
        serde_json::json!({
            "source_build": false,
            "minimum_free_bytes": 0,
            "fabric_connectivity": "none",
            "fabric_minimum_mbps": 0,
        }),
    )
}

fn policy() -> RunOncePolicy<'static> {
    RunOncePolicy {
        preflight_fingerprint: None,
        wait_seconds: 0,
        runtime_identity: None,
        heartbeat_interval: Duration::from_millis(10),
        heartbeat_retry_interval: Duration::from_millis(1),
        lease_renewed: crate::systemd_notify::watchdog,
    }
}

fn delivered(client: &QueueClient, fence: uuid::Uuid) -> Option<AgentResult> {
    client
        .results
        .lock()
        .unwrap()
        .iter()
        .find(|result| result.fence == fence)
        .cloned()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_probe_is_claimed_and_delivered_while_a_build_compiles_beside_the_lane() {
    // Wrong implementation: the lane runs the build to completion before its
    // next claim (incident 2026-10-26: a 2.5 h build held the 1 s probe).
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let mut state = StateStore::open(&path, NODE_ID).unwrap();
    let (build, probe) = (build_order(), probe_order());
    let client = QueueClient::default();
    client
        .queued
        .lock()
        .unwrap()
        .extend([build.clone(), probe.clone()]);
    let executor = CompilingExecutor {
        release: tokio::sync::Notify::new(),
    };
    let beside = Beside::default();

    let lane = beside.alongside(async {
        for _ in 0..2 {
            let step =
                run_once_with_heartbeat_interval(&client, &mut state, &executor, policy(), || {
                    Ok(())
                })
                .await
                .unwrap();
            if let LaneStep::OffLane(claim) = step {
                assert_eq!(claim.fence(), build.fence);
                let store = state.reopen().unwrap();
                let client = client.clone();
                let executor = &executor;
                beside.start(claim.fence(), async move {
                    claim.execute(client, store, executor).await
                });
            }
        }
        // The probe was claimed, run and delivered; the build still compiles
        // and keeps its own lease.
        let probed = delivered(&client, probe.fence).expect("probe delivered beside the build");
        assert_eq!(probed.state, AgentResultState::Succeeded);
        assert!(delivered(&client, build.fence).is_none());
        while !client.renewed.lock().unwrap().contains(&build.fence) {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        // A pass of the lane never probes or settles the executing build.
        run_once_with_heartbeat_interval(&client, &mut state, &executor, policy(), || Ok(()))
            .await
            .unwrap();
        assert!(delivered(&client, build.fence).is_none());
        executor.release.notify_one();
    });
    // A lane that waits for the build never claims the probe: bounded, not hung.
    tokio::time::timeout(Duration::from_secs(10), lane)
        .await
        .expect("the lane claimed the probe while the build compiled");
    beside.drain().await;

    let built = delivered(&client, build.fence).expect("the build delivers its own result");
    assert_eq!(built.state, AgentResultState::Succeeded);
    assert!(matches!(built.result, AgentResultResult::OutcomeDone(_)));
    assert!(!Executing::contains(&state, build.fence));
    assert!(state.pending_results().unwrap().is_empty());
}

#[test]
fn off_lane_operations_are_the_shared_vector_the_controller_reads() {
    // Wrong implementation: the agent runs an order beside its lane that the
    // Controller still counts as holding the lane, or the reverse.
    let every = [
        AgentOperation::AgentUpgradeV1,
        AgentOperation::ArtifactDistributionV1,
        AgentOperation::RecipeBuildCleanupV1,
        AgentOperation::RecipeBuildV1,
        AgentOperation::RecipeInstall,
        AgentOperation::RecipeJobRunV1,
        AgentOperation::RecipeReconcile,
        AgentOperation::RecipeStart,
        AgentOperation::RecipeStop,
        AgentOperation::RecipeUninstall,
        AgentOperation::RuntimePreflightV1,
    ];
    // A new operation fails to compile here until it is placed on a side.
    for operation in &every {
        match operation {
            AgentOperation::AgentUpgradeV1
            | AgentOperation::ArtifactDistributionV1
            | AgentOperation::RecipeBuildCleanupV1
            | AgentOperation::RecipeBuildV1
            | AgentOperation::RecipeInstall
            | AgentOperation::RecipeJobRunV1
            | AgentOperation::RecipeReconcile
            | AgentOperation::RecipeStart
            | AgentOperation::RecipeStop
            | AgentOperation::RecipeUninstall
            | AgentOperation::RuntimePreflightV1 => {}
        }
    }
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/fixtures/off-lane-operations.json"
    ))
    .unwrap();
    let mut off_lane: Vec<String> = every
        .iter()
        .filter(|operation| runs_off_lane(operation))
        .map(ToString::to_string)
        .collect();
    off_lane.sort();
    assert_eq!(serde_json::json!(off_lane), vector["off_lane_operations"]);
}
