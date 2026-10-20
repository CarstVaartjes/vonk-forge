//! Wrong implementations caught: snapshotting the first lease and observing
//! only byte progress for cancellation.
use super::*;

#[tokio::test(start_paused = true)]
async fn silent_work_outlives_initial_lease_when_authority_renews() {
    let initial = (Utc::now() + chrono::Duration::seconds(30)).fixed_offset();
    let (owner, lease) = tokio::sync::watch::channel(initial);
    let (_cancel_owner, cancellation) = tokio::sync::watch::channel(false);
    let renew = async move {
        tokio::time::sleep(Duration::from_secs(20)).await;
        owner.send_replace((Utc::now() + chrono::Duration::seconds(90)).fixed_offset());
        tokio::time::sleep(Duration::from_secs(80)).await;
    };
    let work = run_with_authority(
        async {
            tokio::time::sleep(Duration::from_secs(60)).await;
            42
        },
        lease,
        cancellation,
    );
    let (result, ()) = tokio::join!(work, renew);
    assert_eq!(result, Some(42));
}

#[tokio::test(start_paused = true)]
async fn supersession_interrupts_silent_work_and_fresh_work_runs() {
    let (lease_owner, lease) =
        tokio::sync::watch::channel((Utc::now() + chrono::Duration::seconds(90)).fixed_offset());
    let (owner, cancellation) = tokio::sync::watch::channel(false);
    let (result, ()) = tokio::join!(
        run_with_authority(std::future::pending::<()>(), lease.clone(), cancellation),
        async {
            tokio::time::sleep(Duration::from_secs(1)).await;
            owner.send_replace(true);
        }
    );
    assert_eq!(result, None);
    let (_fresh_owner, fresh) = tokio::sync::watch::channel(false);
    assert_eq!(
        run_with_authority(async { 42 }, lease, fresh).await,
        Some(42)
    );
    drop(lease_owner);
}

#[tokio::test]
async fn a_reissued_distribution_drops_the_old_counter_baseline_before_copying() {
    // Wrong implementation: re-observation reuses the fence, and executing the
    // next copy keeps its old high-water snapshot instead of a fresh attempt.
    use crate::executor::test_support::{NODE_ID, NoProcess, claim};
    let root = tempfile::tempdir().unwrap();
    let client = AgentHttpClient::for_http_test("http://127.0.0.1:9/", NODE_ID);
    let mut order = claim();
    order.operation = vonk_agent_protocol::generated::AgentOperation::ArtifactDistributionV1;
    order.payload = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/fixtures/artifact-distribution-payload.json"
    ))
    .unwrap();
    order.validate().unwrap();
    client.set_progress_phase(order.fence, ProgressPhase::Copying);
    client.set_progress_bytes(order.fence, 100, 200);
    let previous = LoopClient::progress_observation(&client, order.fence).unwrap();
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: std::path::Path::new("unused"),
        runtime: crate::oci::OciRuntime {
            runner: &NoProcess,
            data_root: root.path(),
        },
        runtime_root: root.path(),
    };
    let (_lease_owner, lease) = tokio::sync::watch::channel(order.deadline);
    // Cancel before network effects. Admission still starts an attempt, and
    // its real execution entry point must publish the reset baseline.
    let (_cancel_owner, cancellation) = tokio::sync::watch::channel(true);
    executor
        .execute_distribution(&order, lease, cancellation)
        .await;
    let current = LoopClient::progress_observation(&client, order.fence).unwrap();
    assert_eq!(current.completed_bytes, 0);
    assert!(current.checkpoint.is_some());
    assert_ne!(
        current.checkpoint.unwrap().key,
        previous.checkpoint.unwrap().key
    );
}
