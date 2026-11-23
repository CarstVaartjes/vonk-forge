#![cfg(test)]
use super::*;

fn inventory() -> InventoryRequest {
    InventoryRequest {
        schema_version: 1,
        observed_at: "2026-08-03T00:00:00Z".parse().unwrap(),
        disk_total_bytes: 2 * 1024_u64.pow(4),
        disk_free_bytes: 1024_u64.pow(4),
        host_memory_total_bytes: 2 * 1024_u64.pow(4),
        host_memory_free_bytes: 1024_u64.pow(4),
        gpu_memory_total_bytes: 100_000,
        gpu_memory_free_bytes: 80_000,
        gpu_count: 1,
        gpu_health: Some(generated::GpuHealth::Healthy),
        memory_pool: MemoryPool::Separate,
        artifact_store_read_only: false,
        capabilities: vec![generated::AgentOperation::RecipeBuildV1.to_string()],
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
        nvidia_driver_version: "550.1".to_owned(),
        container_runtime_version: "podman-5".to_owned(),
    }
}

#[test]
fn inventory_health_survives_the_shared_python_vector() {
    // Catches an independent Rust shape dropping or renaming GPU health.
    let mut document: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/inventory-wifi-link-v1.json"
    ))
    .unwrap();
    for health in [
        generated::GpuHealth::Healthy,
        generated::GpuHealth::NeedsReset,
        generated::GpuHealth::Unavailable,
    ] {
        document["gpu_health"] = serde_json::to_value(health).unwrap();
        let bytes = serde_json::to_vec(&document).unwrap();
        let request: InventoryRequest = crate::parse_strict(&bytes).unwrap();
        assert_eq!(request.gpu_health, Some(health));
        assert_eq!(serde_json::to_value(request).unwrap(), document);
    }
}

#[test]
fn inventory_validation_matches_python_bounds_and_shapes() {
    let value = inventory();
    value.validate().unwrap();

    let mut invalid = value.clone();
    invalid.gpu_count = 65;
    assert!(invalid.validate().is_err());
    let mut invalid = value.clone();
    invalid.capabilities = vec!["Recipe.Build".to_owned()];
    assert!(invalid.validate().is_err());
    let mut invalid = value.clone();
    invalid.fabric_bandwidth_mbps = Some(0);
    assert!(invalid.validate().is_err());
    let mut invalid = value;
    invalid.nvidia_driver_version = "é".to_owned();
    assert!(invalid.validate().is_err());
}

#[test]
fn network_evidence_must_name_its_route_interface() {
    let wifi = NetworkInterface {
        name: "wlP9s9".to_owned(),
        kind: NetworkInterfaceKind::Wifi,
        link_speed_mbps: None,
        carrier: true,
        signal_dbm: None,
        frequency_mhz: None,
        tx_bitrate_mbps: None,
        rx_bitrate_mbps: None,
    };
    let mut value = inventory();
    value.network_interfaces = Some(vec![wifi.clone()]);
    value.nas_route_interface = Some("wlP9s9".to_owned());
    value.validate().unwrap();
    value.nas_route_interface = Some("enP7s7".to_owned());
    assert!(value.validate().is_err());
    value.nas_route_interface = None;
    value.network_interfaces = Some(vec![wifi.clone(), wifi]);
    assert!(value.validate().is_err());
}
