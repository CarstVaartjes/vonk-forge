use chrono::{DateTime, Utc};
use vonk_agent_protocol::InventoryRequest;

fn main() {
    let request = InventoryRequest {
        schema_version: 1,
        observed_at: DateTime::parse_from_rfc3339("2026-08-03T00:00:00Z")
            .expect("probe timestamp")
            .with_timezone(&Utc),
        disk_total_bytes: 16 * 1024 * 1024 * 1024,
        disk_free_bytes: 12 * 1024 * 1024 * 1024,
        host_memory_total_bytes: 64 * 1024 * 1024 * 1024,
        host_memory_free_bytes: 32 * 1024 * 1024 * 1024,
        gpu_memory_total_bytes: 16 * 1024 * 1024 * 1024,
        gpu_memory_free_bytes: 12 * 1024 * 1024 * 1024,
        gpu_count: 1,
        artifact_store_read_only: false,
        capabilities: vec![
            "build.rootless-podman.v1".to_owned(),
            "recipe.build.v1".to_owned(),
            "recipe.image.import.v1".to_owned(),
        ],
        fabric_address: None,
        fabric_bandwidth_mbps: None,
        nvidia_driver_version: "550.1".to_owned(),
        container_runtime_version: "5.0".to_owned(),
    };
    request.validate().expect("valid inventory probe");
    println!(
        "{}",
        serde_json::to_string(&request).expect("inventory JSON")
    );
}
