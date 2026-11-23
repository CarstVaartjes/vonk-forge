#![cfg(test)]
use super::*;

#[tokio::test(start_paused = true)]
async fn progress_restarts_the_window_and_silence_ends_it() {
    let inactivity = Inactivity::new(Duration::from_secs(10));
    let dripping = async {
        for _ in 0..5 {
            tokio::time::sleep(Duration::from_secs(8)).await;
            inactivity.observe_bytes(1);
        }
        "complete"
    };
    // Forty seconds of slow progress outlive four whole windows.
    assert_eq!(inactivity.bound(dripping).await, Ok("complete"));
    let silent = std::future::pending::<()>();
    let started = Instant::now();
    assert_eq!(
        inactivity.bound(silent).await,
        Err(Stalled {
            window: Duration::from_secs(10),
            bytes: 5
        })
    );
    // The window counts from the last byte, not from the start of the wait.
    assert_eq!(started.elapsed(), Duration::from_secs(10));
}

#[tokio::test(start_paused = true)]
async fn a_peer_verifying_a_complete_upload_is_waited_for() {
    let inactivity = Inactivity::new(Duration::from_secs(1));
    let body = futures_util::stream::iter([Ok::<_, ()>(vec![0_u8; 4])]);
    let mut sent = Box::pin(inactivity.observe_upload(body, 4));
    // The transport stops at the declared length, before the stream ends.
    assert!(sent.next().await.is_some());
    let verifying = tokio::time::sleep(Duration::from_secs(30));
    assert_eq!(inactivity.bound(verifying).await, Ok(()));
    inactivity.observe_bytes(1);
    assert!(
        inactivity
            .bound(std::future::pending::<()>())
            .await
            .is_err()
    );
}

#[tokio::test(start_paused = true)]
async fn zero_bytes_are_not_progress() {
    let inactivity = Inactivity::new(Duration::from_secs(1));
    tokio::time::sleep(Duration::from_millis(900)).await;
    inactivity.observe_bytes(0);
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(inactivity.expired());
}
