#![cfg(all(test, target_os = "linux"))]

use super::*;

#[tokio::test]
async fn blocked_os_collector_is_killed_reaped_and_a_fresh_wire_observation_succeeds() {
    let root = tempfile::tempdir().unwrap();
    let fifo = root.path().join("blocked");
    rustix::fs::mknodat(
        rustix::fs::CWD,
        &fifo,
        rustix::fs::FileType::Fifo,
        rustix::fs::Mode::from_raw_mode(0o600),
        0,
    )
    .unwrap();
    let pid_file = root.path().join("collector.pid");
    let blocked = vec![
        "-c".to_owned(),
        "printf '%s' \"$$\" > \"$1\"; exec /bin/cat \"$2\"".to_owned(),
        "collector".to_owned(),
        pid_file.to_string_lossy().into_owned(),
        fifo.to_string_lossy().into_owned(),
    ];
    assert!(
        collect_process_with_budget(Path::new("/bin/sh"), &blocked, Duration::from_secs(1))
            .await
            .is_err()
    );
    let pid = fs::read_to_string(&pid_file)
        .unwrap()
        .parse::<i32>()
        .unwrap();
    let request = vonk_agent_protocol::generated::InventoryRequest {
        schema_version: 1,
        observed_at: chrono::Utc::now().into(),
        disk_total_bytes: 100,
        disk_free_bytes: 50,
        host_memory_total_bytes: 100,
        host_memory_free_bytes: 50,
        gpu_memory_total_bytes: 100,
        gpu_memory_free_bytes: 50,
        gpu_count: 1,
        gpu_health: Some(vonk_agent_protocol::generated::GpuHealth::Healthy),
        memory_pool: MemoryPool::Shared,
        artifact_store_read_only: false,
        capabilities: vec![],
        fabric_address: None,
        fabric_bandwidth_mbps: None,
        network_interfaces: None,
        nas_route_interface: None,
        host_settings: None,
        installations: None,
        unused_model_bytes: None,
        model_objects: None,
        runs: None,
        installation_bytes: None,
        nvidia_driver_version: "590".into(),
        container_runtime_version: "29".into(),
    };
    let wire = root.path().join("inventory.json");
    fs::write(
        &wire,
        vonk_agent_protocol::canonical_json(&request).unwrap(),
    )
    .unwrap();
    let arguments = vec![wire.to_string_lossy().into_owned()];
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let fresh = loop {
        if let Ok(request) =
            collect_process_with_budget(Path::new("/bin/cat"), &arguments, Duration::from_secs(1))
                .await
        {
            break request;
        }
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(10)).await;
    };
    // Canonical wire timestamps use microseconds; compare the complete
    // wire values rather than the producer clock's extra nanoseconds.
    assert_eq!(
        vonk_agent_protocol::canonical_json(&fresh).unwrap(),
        vonk_agent_protocol::canonical_json(&request).unwrap()
    );
    // The original blocking filesystem observer released its OS process,
    // rather than merely dropping a future and substituting a closure.
    assert!(
        rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid).unwrap()).is_err()
    );
}
