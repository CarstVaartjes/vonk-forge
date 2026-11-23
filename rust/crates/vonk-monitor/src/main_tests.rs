#![cfg(test)]
use super::{INTERVAL, next_tick_after};
use std::time::Duration;

#[tokio::test]
async fn slow_sample_is_reportable_after_its_first_tick_budget() {
    // Catches discarding the finished worker after a driver query times out.
    let (release, receiver) = tokio::sync::oneshot::channel();
    let mut sampling = Some(tokio::spawn(async move { receiver.await.unwrap() }));
    assert!(
        super::completed_sample(&mut sampling, tokio::time::Instant::now())
            .await
            .is_none()
    );
    assert!(sampling.is_some());
    release.send(42).unwrap();
    assert_eq!(
        super::completed_sample(&mut sampling, tokio::time::Instant::now() + INTERVAL)
            .await
            .unwrap()
            .unwrap(),
        42
    );
    assert!(sampling.is_none());
}

#[test]
fn boot_projection_loss_ends_a_sample_and_the_next_tick_can_initialize() {
    let root = std::env::temp_dir().join(format!(
        "vonk-monitor-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir(&root).unwrap();
    let config = vonk_agent::config::AgentConfig::parse(&format!(
            "enrollment_url = \"https://enroll.example/\"\ncontroller_url = \"https://control.example/\"\nca_path = \"/etc/ca.pem\"\nca_sha256 = \"{}\"\ndata_dir = \"{}\"\nnode_id = \"spk_0123456789abcdef0123456789abcdef\"\n",
            "a".repeat(64), root.display(),
        )).unwrap();
    let boot = root.join("boot-id");
    assert!(super::collector_for(&config, &boot).is_none());
    std::fs::write(&boot, b"damaged projection").unwrap();
    assert!(super::collector_for(&config, &boot).is_none());
    std::fs::write(&boot, b"10000000-0000-4000-8000-000000000001\n").unwrap();
    assert!(super::collector_for(&config, &boot).is_some());
    std::fs::remove_file(boot).unwrap();
    std::fs::remove_dir(root).unwrap();
}

#[test]
fn missed_ticks_are_skipped_without_backlog() {
    let started = tokio::time::Instant::now();
    let finished = started + Duration::from_secs(7);
    assert_eq!(
        next_tick_after(started, finished),
        started + Duration::from_secs(8)
    );
}

#[test]
fn normal_tick_is_exactly_two_seconds() {
    let started = tokio::time::Instant::now();
    assert_eq!(next_tick_after(started, started), started + INTERVAL);
}
