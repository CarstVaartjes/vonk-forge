#![cfg(test)]

use super::attributes::*;
use crate::inventory::Inventory;
use vonk_agent_protocol::generated::{
    InventoryRequestMemoryPool, NetworkInterface, NetworkInterfaceKind,
};

fn attr(kind: u16, value: &[u8]) -> Vec<u8> {
    let mut bytes = Vec::from(u16::try_from(value.len() + 4).unwrap().to_ne_bytes());
    bytes.extend(kind.to_ne_bytes());
    bytes.extend(value);
    bytes.resize((bytes.len() + 3) & !3, 0);
    bytes
}

fn interface() -> NetworkInterface {
    NetworkInterface {
        name: "wlP9s9".into(),
        kind: NetworkInterfaceKind::Wifi,
        carrier: true,
        link_speed_mbps: None,
        signal_dbm: None,
        frequency_mhz: Some(2412),
        tx_bitrate_mbps: None,
        rx_bitrate_mbps: None,
    }
}

fn station_blob() -> Vec<u8> {
    let mut info = attr(7, &[178]); // signed -78 dBm, not quality percent
    let mut tx = attr(1, &540u16.to_ne_bytes());
    // The extended rate wins when both encodings are present.
    tx.extend(attr(5, &8667u32.to_ne_bytes()));
    info.extend(attr(8 | 0x8000, &tx));
    info.extend(attr(14 | 0x8000, &attr(1, &65u16.to_ne_bytes())));
    info.extend(attr(255, &[1, 2, 3])); // future kernel extension
    attr(STA_INFO | 0x8000, &info)
}

#[test]
fn signed_signal_fractional_rates_and_extended_rate_survive_agent_output() {
    // Wrong implementation: unsigned dBm, 100-kbit/s treated as Mbit/s,
    // 16-bit rate preferred over BITRATE32, or fields omitted at serialization.
    let mut nic = interface();
    station(&mut nic, &station_blob());
    assert_eq!(nic.signal_dbm, Some(-78));
    assert_eq!(nic.tx_bitrate_mbps, Some(866.7));
    assert_eq!(nic.rx_bitrate_mbps, Some(6.5));
    let inventory = Inventory {
        memory_total_bytes: 100,
        memory_available_bytes: 50,
        disk_total_bytes: 100,
        disk_available_bytes: 50,
        state_database_reserve_held: true,
        gpu_count: 1,
        gpu_health: vonk_agent_protocol::generated::GpuHealth::Healthy,
        gpu_memory_total_bytes: 100,
        gpu_memory_free_bytes: 50,
        memory_pool: InventoryRequestMemoryPool::Shared,
        nvidia_driver_version: "580.1".into(),
        container_runtime_version: "podman 5".into(),
        artifact_store_read_only: false,
        capabilities: vec![],
        fabric_address: None,
        fabric_bandwidth_mbps: None,
        network_interfaces: Some(vec![nic]),
        nas_route_interface: Some("wlP9s9".into()),
        host_settings: None,
        installations: None,
        unused_model_bytes: None,
        model_objects: None,
        runs: None,
        installation_bytes: None,
    };
    let request = inventory.to_request("2026-10-18T12:00:00Z".parse().unwrap());
    request.validate().unwrap();
    let expected: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../agent_protocol/src/vonk_agent_protocol/vectors/inventory-wifi-link-v1.json"
    ))
    .unwrap();
    assert_eq!(serde_json::to_value(request).unwrap(), expected);
}

#[test]
fn unreadable_optional_attributes_do_not_invent_measurements() {
    // Wrong implementation: malformed/zero values become plausible rates,
    // or an unknown kernel extension hides otherwise readable fields.
    let mut nic = interface();
    station(&mut nic, &attr(STA_INFO, &attr(7, &[40])));
    assert_eq!(nic.signal_dbm, None);
    assert_eq!(nic.tx_bitrate_mbps, None);
    station(
        &mut nic,
        &attr(STA_INFO, &attr(8, &attr(1, &0u16.to_ne_bytes()))),
    );
    assert_eq!(nic.tx_bitrate_mbps, None);
    for end in 1..4 {
        assert!(parse(&station_blob()[..end]).is_none());
    }
    let mut blob = station_blob();
    blob.pop();
    station(&mut nic, &blob);
    assert_eq!(nic.signal_dbm, None);
    assert!(parse(&[0, 0, 0, 0]).is_none());
}

#[test]
fn channel_is_bound_to_the_selected_managed_interface() {
    // Wrong implementation: another interface's channel, an AP client's
    // frequency or a truncated value is reported as the host's NAS link.
    let mut nic = interface();
    nic.frequency_mhz = None;
    let mut bytes = attr(IFINDEX, &9u32.to_ne_bytes());
    bytes.extend(attr(IFTYPE, &2u32.to_ne_bytes()));
    bytes.extend(attr(WIPHY_FREQ, &6115u32.to_ne_bytes()));
    assert!(radio(&mut nic, &bytes, 10).is_none());
    assert_eq!(nic.frequency_mhz, None);
    radio(&mut nic, &bytes, 9).unwrap();
    assert_eq!(nic.frequency_mhz, Some(6115));
    let mut ap = attr(IFINDEX, &9u32.to_ne_bytes());
    ap.extend(attr(IFTYPE, &3u32.to_ne_bytes()));
    assert!(radio(&mut nic, &ap, 9).is_none());
    let mut malformed = attr(IFINDEX, &9u32.to_ne_bytes());
    malformed.extend(attr(IFTYPE, &2u32.to_ne_bytes()));
    malformed.extend(attr(WIPHY_FREQ, &[1]));
    radio(&mut nic, &malformed, 9).unwrap();
    assert_eq!(nic.frequency_mhz, None);
}
