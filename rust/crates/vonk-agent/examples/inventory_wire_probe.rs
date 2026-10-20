use chrono::DateTime;
use vonk_agent_protocol::{
    InventoryRequest, MemoryPool, canonical_generated_json, generated::ModelStoreObject,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let request = InventoryRequest {
        schema_version: 1,
        observed_at: DateTime::parse_from_rfc3339("2026-08-03T00:00:00Z")?,
        disk_total_bytes: 16 * 1024 * 1024 * 1024,
        disk_free_bytes: 12 * 1024 * 1024 * 1024,
        host_memory_total_bytes: 64 * 1024 * 1024 * 1024,
        host_memory_free_bytes: 32 * 1024 * 1024 * 1024,
        gpu_memory_total_bytes: 16 * 1024 * 1024 * 1024,
        gpu_memory_free_bytes: 12 * 1024 * 1024 * 1024,
        gpu_count: 1,
        memory_pool: MemoryPool::Separate,
        artifact_store_read_only: false,
        capabilities: vec![
            "build.rootless-podman.v1".to_owned(),
            "recipe.build.v1".to_owned(),
            "recipe.image.pull.v1".to_owned(),
            "runtime.vonk.v1".to_owned(),
        ],
        fabric_address: None,
        fabric_bandwidth_mbps: None,
        network_interfaces: None,
        nas_route_interface: None,
        host_settings: None,
        installations: None,
        unused_model_bytes: None,
        model_objects: Some(
            (0..1024)
                .map(|i| ModelStoreObject {
                    sha256: if i == 0 {
                        vonk_agent_protocol::hex_sha256(b"inventory weights")
                    } else {
                        format!("{i:064x}")
                    },
                    bytes: b"inventory weights".len() as u64,
                })
                .collect(),
        ),
        runs: None,
        installation_bytes: None,
        nvidia_driver_version: "550.1".to_owned(),
        container_runtime_version: "5.0".to_owned(),
    };
    request.validate()?;
    let root = tempfile::tempdir()?;
    let wire = root.path().join("inventory.json");
    std::fs::write(&wire, canonical_generated_json(&request)?)?;
    let request = vonk_agent::inventory::collect_process(
        std::path::Path::new("/bin/cat"),
        &[wire.to_string_lossy().into_owned()],
    )
    .await?;
    println!(
        "{}",
        String::from_utf8(canonical_generated_json(&request)?)?
    );
    Ok(())
}
