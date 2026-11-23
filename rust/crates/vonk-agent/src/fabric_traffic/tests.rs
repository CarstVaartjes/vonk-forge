#![cfg(test)]

use std::{fs, time::Duration};

use super::*;

const VECTOR: &str = include_str!(
    "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/fabric-devices-v1.json"
);

/// A sysfs shaped like a Spark's: devices under `class/infiniband`, each with
/// a `device` link to its PCI function under `devices`.
fn sysfs(devices: &serde_json::Value, reading: &str) -> tempfile::TempDir {
    let root = tempfile::tempdir().unwrap();
    for device in devices.as_array().unwrap() {
        let name = device["name"].as_str().unwrap();
        let pci = root
            .path()
            .join("devices")
            .join(device["pci"].as_str().unwrap());
        fs::create_dir_all(&pci).unwrap();
        let class = root.path().join("class/infiniband").join(name);
        fs::create_dir_all(class.join("ports/1/counters")).unwrap();
        std::os::unix::fs::symlink(&pci, class.join("device")).unwrap();
        fs::write(
            class.join("ports/1/state"),
            format!("{}\n", device["state"].as_str().unwrap()),
        )
        .unwrap();
        let [rx, tx] = [0, 1].map(|index| device[reading][index].as_u64().unwrap());
        fs::write(class.join("ports/1/counters/port_rcv_data"), rx.to_string()).unwrap();
        fs::write(
            class.join("ports/1/counters/port_xmit_data"),
            tx.to_string(),
        )
        .unwrap();
    }
    root
}

#[test]
fn the_shared_sample_reads_as_the_shared_deltas() {
    let vector: serde_json::Value = serde_json::from_str(VECTOR).unwrap();
    let sample = &vector["agent_sample"];
    let meter = Meter::default();
    let start = Instant::now();
    let first = sysfs(&sample["devices"], "first");
    assert!(
        meter
            .observe_at(&first.path().join("class/infiniband"), start)
            .is_empty()
    );
    let second = sysfs(&sample["devices"], "second");
    let window = Duration::from_secs(sample["window_seconds"].as_u64().unwrap());
    let observed = meter.observe_at(&second.path().join("class/infiniband"), start + window);
    assert_eq!(serde_json::to_value(&observed).unwrap(), sample["expected"]);
}

#[test]
fn the_shared_cases_are_the_contract_the_agent_reports() {
    let vector: serde_json::Value = serde_json::from_str(VECTOR).unwrap();
    for case in vector["cases"].as_array().unwrap() {
        let devices: Vec<FabricDevice> = serde_json::from_value(case["fabric_devices"].clone())
            .unwrap_or_else(|error| panic!("{}: {error}", case["name"]));
        assert_eq!(
            serde_json::to_value(&devices).unwrap(),
            case["fabric_devices"]
        );
    }
}

#[test]
fn a_counter_that_went_backwards_is_skipped() {
    let vector: serde_json::Value = serde_json::from_str(VECTOR).unwrap();
    let devices = &vector["agent_sample"]["devices"];
    let meter = Meter::default();
    let start = Instant::now();
    // The "second" reading first, then the "first": every counter drops.
    let high = sysfs(devices, "second");
    meter.observe_at(&high.path().join("class/infiniband"), start);
    let low = sysfs(devices, "first");
    let observed = meter.observe_at(
        &low.path().join("class/infiniband"),
        start + Duration::from_secs(120),
    );
    // Only the device whose counters stayed (at zero) has a delta.
    assert_eq!(observed.len(), 1);
    assert_eq!(observed[0].port, "01:00.0");
}
