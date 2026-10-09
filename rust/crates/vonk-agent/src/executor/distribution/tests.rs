//! Wrong implementations caught: snapshotting the first lease, observing only
//! byte progress for cancellation, and extending the work budget on renewal.
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
        Duration::from_secs(120),
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
        run_with_authority(
            std::future::pending::<()>(),
            lease.clone(),
            cancellation,
            Duration::from_secs(120)
        ),
        async {
            tokio::time::sleep(Duration::from_secs(1)).await;
            owner.send_replace(true);
        }
    );
    assert_eq!(result, None);
    let (_fresh_owner, fresh) = tokio::sync::watch::channel(false);
    assert_eq!(
        run_with_authority(async { 42 }, lease, fresh, Duration::from_secs(1)).await,
        Some(42)
    );
    drop(lease_owner);
}

#[tokio::test(start_paused = true)]
async fn operation_budget_expires_despite_renewals_and_fresh_work_runs() {
    let (_lease_owner, lease) =
        tokio::sync::watch::channel((Utc::now() + chrono::Duration::hours(1)).fixed_offset());
    let (_cancel_owner, cancellation) = tokio::sync::watch::channel(false);
    assert_eq!(
        run_with_authority(
            std::future::pending::<()>(),
            lease.clone(),
            cancellation.clone(),
            Duration::from_secs(1)
        )
        .await,
        None
    );
    assert_eq!(
        run_with_authority(async { 42 }, lease, cancellation, Duration::from_secs(1)).await,
        Some(42)
    );
}
