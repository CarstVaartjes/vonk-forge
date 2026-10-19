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
        host_settings: None,
        installations: None,
        unused_model_bytes: None,
        model_objects: None,
        runs: None,
        installation_bytes: None,
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
        identity_content: Default::default(),
        progress_phase: Default::default(),
        transfer_inactivity: TRANSFER_INACTIVITY,
        distribution_range_bytes: DISTRIBUTION_RANGE_BYTES,
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
async fn unavailable_telemetry_peer_drops_one_sample_and_accepts_the_next() {
    let samples = [telemetry_sample()];
    for status in [200, 401, 429] {
        let (client, server) = observation_client(status).await;
        assert!(
            tokio::time::timeout(Duration::from_secs(1), client.report_telemetry(&samples))
                .await
                .unwrap()
                .is_err()
        );
        finish_capture_peer(server).await;
        let (fresh, server) = observation_client(204).await;
        fresh.report_telemetry(&samples).await.unwrap();
        let request = finish_capture_peer(server).await;
        assert!(request.starts_with(b"POST /agent/telemetry "));
    }
}

#[tokio::test]
async fn an_inventory_returns_the_controllers_verdict_and_an_older_controller_none() {
    const ORPHAN: &str = "5f0c1e2d-3b4a-4c5d-8e6f-7a8b9c0d1e2f";
    let mut request = valid_inventory();
    request.installations = Some(vec![ORPHAN.parse().unwrap()]);
    let verdict = format!(r#"{{"untracked_installations":["{ORPHAN}"]}}"#);
    let (client, server) = request_capture_client(
        200,
        vec!["content-type: application/json".to_owned()],
        verdict.into_bytes(),
        None,
        CONTROLLER_REQUEST_TIMEOUT,
    )
    .await;

    let answer = client
        .report_inventory_request(request.clone())
        .await
        .unwrap();

    let answer = answer.unwrap();
    assert_eq!(
        answer.untracked_installations,
        vec![ORPHAN.parse::<uuid::Uuid>().unwrap()]
    );
    assert_eq!(answer.untracked_runs, None);
    let sent = finish_capture_peer(server).await;
    assert!(String::from_utf8_lossy(&sent).contains(&format!(r#""installations":["{ORPHAN}"]"#)));
    // A verdict without run scopes judged none; one that names them carries them.
    assert_eq!(
        client_answer(format!(
            r#"{{"untracked_installations":[],"untracked_runs":["{ORPHAN}"]}}"#
        ))
        .await
        .and_then(|verdict| verdict.untracked_runs),
        Some(vec![ORPHAN.parse::<uuid::Uuid>().unwrap()])
    );

    // An older Controller answers nothing, and a verdict that cannot be read
    // is none: the inventory is reported either way and nothing is removed.
    for (status, body) in [(204, Vec::new()), (200, br#"{"untracked":[]}"#.to_vec())] {
        let (client, server) =
            request_capture_client(status, Vec::new(), body, None, CONTROLLER_REQUEST_TIMEOUT)
                .await;
        assert_eq!(
            client
                .report_inventory_request(request.clone())
                .await
                .unwrap(),
            None
        );
        finish_capture_peer(server).await;
    }
}

/// The verdict a Controller answering `body` gives an inventory naming runs.
async fn client_answer(body: String) -> Option<vonk_agent_protocol::generated::InventoryVerdict> {
    let mut request = valid_inventory();
    request.runs = Some(vec![uuid::Uuid::nil()]);
    let (client, server) = request_capture_client(
        200,
        vec!["content-type: application/json".to_owned()],
        body.into_bytes(),
        None,
        CONTROLLER_REQUEST_TIMEOUT,
    )
    .await;
    let answer = client.report_inventory_request(request).await.unwrap();
    finish_capture_peer(server).await;
    answer
}

/// An inventory with every optional evidence group present.
fn inventory_with_all_evidence() -> vonk_agent_protocol::InventoryRequest {
    let mut request = valid_inventory();
    request.network_interfaces = Some(vec![interface("enP7s7", Some(1000))]);
    request.nas_route_interface = Some("enP7s7".to_owned());
    request.fabric_address = Some("192.168.100.2".to_owned());
    request.fabric_bandwidth_mbps = Some(200_000);
    request.host_settings = Some(vonk_agent_protocol::generated::HostSettings {
        display_manager_active: false,
        graphical_default_target: false,
        gpu_clock_cap_active: Some(true),
    });
    request.installations = Some(vec![uuid::Uuid::nil()]);
    request.runs = Some(vec![uuid::Uuid::nil()]);
    request.validate().unwrap();
    request
}

#[test]
fn the_inventory_core_keeps_the_capacity_report_and_no_optional_evidence() {
    let request = inventory_with_all_evidence();

    let core = inventory_core(&request);

    core.validate().unwrap();
    let mut expected = valid_inventory();
    expected.observed_at = request.observed_at;
    assert_eq!(core, expected);
}

/// Two exchanges on one listener: answer each request with its status and
/// return the request bodies.
async fn two_answers(
    statuses: [u16; 2],
) -> (AgentHttpClient, tokio::task::JoinHandle<Vec<String>>) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let mut bodies = Vec::new();
        for status in statuses {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 8192];
            loop {
                let size = stream.read(&mut buffer).await.unwrap();
                assert_ne!(size, 0, "request ended early");
                request.extend_from_slice(&buffer[..size]);
                let text = String::from_utf8_lossy(&request).to_string();
                if let Some(end) = text.find("\r\n\r\n") {
                    let length = text[..end]
                        .lines()
                        .find_map(|line| {
                            line.to_ascii_lowercase()
                                .strip_prefix("content-length:")
                                .map(|value| value.trim().parse::<usize>().unwrap())
                        })
                        .unwrap_or(0);
                    if request.len() >= end + 4 + length {
                        bodies.push(text[end + 4..end + 4 + length].to_owned());
                        break;
                    }
                }
            }
            let body = if status == 422 {
                r#"{"detail":"extra field"}"#
            } else {
                ""
            };
            let response = format!(
                "HTTP/1.1 {status} Test\r\ncontent-type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream.write_all(response.as_bytes()).await.unwrap();
        }
        bodies
    });
    let client = authenticated_test_client(&format!("http://{address}/"), TEST_NODE_ID);
    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(
        "x-vonk-fixture-auth",
        reqwest::header::HeaderValue::from_static("enrolled-agent"),
    );
    *client.client.write().await = reqwest::Client::builder()
        .default_headers(headers)
        .timeout(CONTROLLER_REQUEST_TIMEOUT)
        .build()
        .unwrap();
    (client, server)
}

#[tokio::test]
async fn an_older_controller_that_refuses_optional_evidence_still_gets_the_core() {
    let (client, server) = two_answers([422, 204]).await;

    let answer = client
        .report_inventory_request(inventory_with_all_evidence())
        .await
        .unwrap();

    assert_eq!(answer, None);
    let bodies = server.await.unwrap();
    assert!(bodies[0].contains("\"installations\"") && bodies[0].contains("\"host_settings\""));
    for optional in [
        "installations",
        "host_settings",
        "network_interfaces",
        "nas_route_interface",
        "fabric_address",
    ] {
        assert!(!bodies[1].contains(optional), "{optional} in {}", bodies[1]);
    }
    assert!(bodies[1].contains("\"disk_free_bytes\""));
}
