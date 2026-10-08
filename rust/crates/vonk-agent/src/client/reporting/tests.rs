#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn out_of_contract_inventory_is_clamped_instead_of_refused() {
    let mut request = vonk_agent_protocol::InventoryRequest {
        schema_version: 1,
        observed_at: Utc::now().into(),
        disk_total_bytes: 100,
        // Free raced above total between two statfs samples.
        disk_free_bytes: 101,
        host_memory_total_bytes: 64 * 1024_u64.pow(4),
        host_memory_free_bytes: 1,
        gpu_memory_total_bytes: 0,
        gpu_memory_free_bytes: 0,
        gpu_count: 1,
        memory_pool: vonk_agent_protocol::generated::InventoryRequestMemoryPool::Shared,
        artifact_store_read_only: false,
        capabilities: vec![
            "runtime.oci".to_owned(),
            "runtime.oci".to_owned(),
            "Bad Capability".to_owned(),
        ],
        fabric_address: None,
        fabric_bandwidth_mbps: Some(100),
        network_interfaces: None,
        nas_route_interface: None,
        nvidia_driver_version: "580.1\u{e9}".to_owned(),
        container_runtime_version: "podman 5".to_owned(),
    };
    assert!(request.validate().is_err());
    clamp_inventory_request(&mut request);
    request.validate().unwrap();
    assert_eq!(request.disk_free_bytes, 100);
    assert_eq!(request.capabilities, vec!["runtime.oci".to_owned()]);
    assert_eq!(request.fabric_bandwidth_mbps, None);
    assert_eq!(request.nvidia_driver_version, "580.1");

    // A missing version is never invented: the report stays invalid.
    request.container_runtime_version.clear();
    clamp_inventory_request(&mut request);
    assert!(request.validate().is_err());
}

#[test]
fn inconsistent_network_evidence_never_fails_the_mandatory_report() {
    use vonk_agent_protocol::generated::AgentEvidenceCode;
    // Wrong implementation: the report went out with a repeated NIC name, an
    // invalid name, an impossible link speed and a route naming no NIC, so
    // the Controller refused the whole capacity report.
    let mut request = valid_inventory();
    request.network_interfaces = Some(vec![
        interface("enP7s7", Some(10_000)),
        interface("enP7s7", None),
        interface("bad name", None),
        interface("wlP9s9", Some(0)),
    ]);
    request.nas_route_interface = Some("tailscale0".to_owned());
    assert!(request.validate().is_err());

    let warnings = clamp_inventory_request(&mut request);

    request.validate().unwrap();
    assert!(warnings.contains(&AgentEvidenceCode::AgentEvidenceInventoryNetworkInterfaceDropped));
    assert!(warnings.contains(&AgentEvidenceCode::AgentEvidenceInventoryNasRouteDropped));
    let names: Vec<_> = request
        .network_interfaces
        .as_ref()
        .unwrap()
        .iter()
        .map(|value| (value.name.as_str(), value.link_speed_mbps))
        .collect();
    assert_eq!(names, vec![("enP7s7", Some(10_000)), ("wlP9s9", None)]);
    assert_eq!(request.nas_route_interface, None);
    // The mandatory capacity evidence is untouched.
    assert_eq!(request.disk_free_bytes, 50);
    assert_eq!(request.container_runtime_version, "podman 5");
}

#[test]
fn wholly_invalid_network_evidence_is_unknown_and_a_fresh_report_is_valid() {
    use vonk_agent_protocol::generated::AgentEvidenceCode;
    let mut request = valid_inventory();
    request.network_interfaces = Some(vec![interface("bad name", None)]);
    request.nas_route_interface = Some("bad name".to_owned());
    let warnings = clamp_inventory_request(&mut request);
    assert!(warnings.contains(&AgentEvidenceCode::AgentEvidenceInventoryNetworkDropped));
    assert_eq!(request.network_interfaces, None);
    assert_eq!(request.nas_route_interface, None);
    request.validate().unwrap();
    let mut fresh = valid_inventory();
    fresh.network_interfaces = Some(vec![interface("enP7s7", Some(1000))]);
    fresh.nas_route_interface = Some("enP7s7".to_owned());
    assert!(clamp_inventory_request(&mut fresh).is_empty());
    fresh.validate().unwrap();
}

#[test]
fn an_oversized_interface_list_keeps_the_nas_route() {
    let mut request = valid_inventory();
    request.network_interfaces = Some(
        (0..20)
            .map(|index| interface(&format!("en{index:02}"), None))
            .collect(),
    );
    request.nas_route_interface = Some("en19".to_owned());
    clamp_inventory_request(&mut request);
    request.validate().unwrap();
    assert_eq!(request.network_interfaces.as_ref().unwrap().len(), 16);
    assert_eq!(request.nas_route_interface.as_deref(), Some("en19"));
}

#[test]
fn a_fabric_address_that_is_not_canonical_is_dropped_with_its_bandwidth() {
    use vonk_agent_protocol::generated::AgentEvidenceCode;
    let mut request = valid_inventory();
    request.fabric_address = Some("192.168.100.002".to_owned());
    request.fabric_bandwidth_mbps = Some(200_000);
    let warnings = clamp_inventory_request(&mut request);
    request.validate().unwrap();
    assert_eq!(
        warnings,
        vec![AgentEvidenceCode::AgentEvidenceInventoryFabricDropped]
    );
    assert_eq!(request.fabric_address, None);
    assert_eq!(request.fabric_bandwidth_mbps, None);
}

#[test]
fn a_consistent_report_is_forwarded_whole_without_warnings() {
    let mut request = valid_inventory();
    request.network_interfaces = Some(vec![interface("enP7s7", Some(1000))]);
    request.nas_route_interface = Some("enP7s7".to_owned());
    request.fabric_address = Some("192.168.100.2".to_owned());
    request.fabric_bandwidth_mbps = Some(200_000);
    let before = request.clone();
    assert!(clamp_inventory_request(&mut request).is_empty());
    assert_eq!(request, before);
}

#[tokio::test]
async fn telemetry_posts_current_contract_without_node_identity() {
    let sample = telemetry_sample();
    let (client, server) = observation_client(204).await;

    client
        .report_telemetry(std::slice::from_ref(&sample))
        .await
        .unwrap();

    let request = finish_capture_peer(server).await;
    let (headers, body) = request
        .windows(4)
        .position(|value| value == b"\r\n\r\n")
        .map(|index| (&request[..index], &request[index + 4..]))
        .unwrap();
    let headers = std::str::from_utf8(headers).unwrap().to_ascii_lowercase();
    assert!(headers.starts_with("post /agent/telemetry http/1.1\r\n"));
    assert!(headers.contains("content-type: application/json"));

    let body: serde_json::Value = serde_json::from_slice(body).unwrap();
    assert_eq!(
        body.as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<Vec<_>>(),
        ["samples"]
    );
    assert_eq!(body["samples"].as_array().unwrap().len(), 1);
    let sample = body["samples"][0].as_object().unwrap();
    let mut keys = sample.keys().cloned().collect::<Vec<_>>();
    keys.sort();
    assert_eq!(
        keys,
        [
            "boot_id",
            "disk_free_bytes",
            "disk_total_bytes",
            "gpu_memory_free_bytes",
            "gpu_memory_total_bytes",
            "gpu_utilization_percent",
            "memory_available_bytes",
            "memory_total_bytes",
            "observed_at",
        ]
    );
    assert!(!body.to_string().contains("node_id"));
    assert_eq!(sample["gpu_utilization_percent"], serde_json::Value::Null);
}

#[tokio::test]
async fn telemetry_allows_the_two_second_controller_budget() {
    let sample = telemetry_sample();
    let (client, server) = request_capture_client(
        204,
        Vec::new(),
        Vec::new(),
        Some(Duration::from_millis(1_500)),
        CONTROLLER_REQUEST_TIMEOUT,
    )
    .await;

    client
        .report_telemetry(std::slice::from_ref(&sample))
        .await
        .expect("telemetry should allow the two-second controller budget");
    finish_capture_peer(server).await;
}

#[tokio::test]
async fn telemetry_rejects_empty_or_more_than_sixteen_samples_before_transport() {
    let client = AgentHttpClient {
        client: Arc::new(RwLock::new(reqwest::Client::new())),
        controller: Url::parse("http://127.0.0.1:9/").unwrap(),
        node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
        progress_phase: Default::default(),
    };
    assert!(matches!(
        client.report_telemetry(&[]).await,
        Err(ClientError::Protocol)
    ));
    let samples = (0..17).map(|_| telemetry_sample()).collect::<Vec<_>>();
    assert!(matches!(
        client.report_telemetry(&samples).await,
        Err(ClientError::Protocol)
    ));
}

#[tokio::test]
async fn telemetry_accepts_only_204_and_preserves_status_classification() {
    let samples = [telemetry_sample()];
    for (status, expected) in [
        (200, "protocol"),
        (401, "authentication"),
        (429, "retryable"),
    ] {
        let (client, server) = observation_client(status).await;
        let error = client.report_telemetry(&samples).await.unwrap_err();
        finish_capture_peer(server).await;
        match expected {
            "protocol" => assert!(matches!(error, ClientError::Protocol)),
            "authentication" => {
                assert_eq!(error.status(), Some(401));
                assert!(!error.retryable());
            }
            "retryable" => {
                assert_eq!(error.status(), Some(429));
                assert!(error.retryable());
            }
            _ => unreachable!("unexpected expected classification"),
        }
    }
}
