#![cfg(test)]
use super::*;
fn put(root: &Path, path: &str, bytes: impl AsRef<[u8]>) {
    let path = root.join(path);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, bytes).unwrap();
}
fn platform(root: &Path) {
    put(root, "sys/class/dmi/id/bios_version", "measured-bios");
    put(root, "proc/sys/kernel/osrelease", "measured-kernel");
    put(root, "proc/meminfo", "MemTotal: 125000000 kB\n");
    put(
        root,
        "sys/firmware/efi/esrt/entries/entry0/fw_class",
        SOC_FW_CLASS,
    );
    put(
        root,
        "sys/firmware/efi/esrt/entries/entry0/fw_version",
        "0x02009e0f",
    );
    put(root, "sys/class/drm/card0-HDMI-A-1/status", "disconnected");
    put(
        root,
        "sys/firmware/devicetree/base/#address-cells",
        2u32.to_be_bytes(),
    );
    put(
        root,
        "sys/firmware/devicetree/base/#size-cells",
        2u32.to_be_bytes(),
    );
    let cells = [0u32, 0, 32, 0]
        .into_iter()
        .flat_map(u32::to_be_bytes)
        .collect::<Vec<_>>();
    put(root, "sys/firmware/devicetree/base/memory@0/reg", cells);
}
#[test]
fn uncalibrated_memtotal_never_becomes_a_carveout_and_reads_recover() {
    // Break caught: a failed read or old classification sticks until restart.
    let root = tempfile::tempdir().unwrap();
    assert!(
        observe(root.path(), Some(128_000_000_000))
            .soc_firmware_version
            .is_none()
    );
    platform(root.path());
    let observed = observe(root.path(), Some(128_000_000_000));
    assert_eq!(observed.soc_firmware_version, Some(0x0200_9e0f));
    assert_eq!(observed.display_connected, Some(false));
    assert_eq!(observed.device_tree_memory_bytes, Some(128 << 30));
    assert!(observed.carveout_bytes.is_none());
    assert_eq!(observed.signal, DisplayMemorySignal::Unknown);
    put(
        root.path(),
        "sys/class/drm/card0-HDMI-A-1/status",
        "connected",
    );
    assert_eq!(
        observe(root.path(), Some(128_000_000_000)).display_connected,
        Some(true)
    );
    fs::remove_file(root.path().join("proc/meminfo")).unwrap();
    assert!(observe(root.path(), None).memory_total_bytes.is_none());
    platform(root.path());
    assert!(
        observe(root.path(), Some(128_000_000_000))
            .memory_total_bytes
            .is_some()
    );
}
#[test]
fn esrt_identity_and_connector_read_failures_remain_unknown_until_reobserved() {
    // Break caught: using the first (EC) version or a missing connector as
    // proof of no display, and retaining a reading after permission loss.
    let root = tempfile::tempdir().unwrap();
    platform(root.path());
    put(
        root.path(),
        "sys/firmware/efi/esrt/entries/entry1/fw_class",
        "3d13c989-e6a8-4ead-95ee-921f09868f65",
    );
    put(
        root.path(),
        "sys/firmware/efi/esrt/entries/entry1/fw_version",
        "999999999",
    );
    assert_eq!(
        observe(root.path(), None).soc_firmware_version,
        Some(0x0200_9e0f)
    );
    put(
        root.path(),
        "sys/firmware/efi/esrt/entries/entry0/fw_version",
        "not-a-version",
    );
    assert!(observe(root.path(), None).soc_firmware_version.is_none());
    platform(root.path());
    fs::remove_file(root.path().join("sys/class/drm/card0-HDMI-A-1/status")).unwrap();
    assert!(observe(root.path(), None).display_connected.is_none());
    put(
        root.path(),
        "sys/class/drm/card0-HDMI-A-1/status",
        "disconnected",
    );
    assert_eq!(observe(root.path(), None).display_connected, Some(false));
}

#[test]
fn agent_unknown_observation_matches_the_controller_shared_vector() {
    // Break caught: an absent sysfs source silently gets the 2 GB default.
    use vonk_agent_protocol::{canonical_generated_json, generated::HostSettings};
    let root = tempfile::tempdir().unwrap();
    let host = HostSettings {
        store_retention_failure: None,
        display_manager_active: false,
        graphical_default_target: false,
        display_memory: Some(observe(root.path(), None)),
        gpu_clock_support: None,
        swap: None,
        tuning: None,
        memory_fragmentation: None,
        fabric_devices: Vec::new(),
    };
    let vectors: Vec<HostSettings> = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/display-memory.json"
    ))
    .unwrap();
    assert_eq!(
        canonical_generated_json(&host).unwrap(),
        canonical_generated_json(&vectors[2]).unwrap()
    );
}

#[test]
fn root_observation_keeps_the_peer_gate_and_rejects_client_paths() {
    // Break caught: exposing privileged reads to another group, accepting a
    // client-selected file, or bypassing the calibrated producer on dispatch.
    use crate::protocol::{GrantVerifier, PeerIdentity};
    use vonk_agent_protocol::{
        canonical_generated_json,
        generated::{DisplayMemoryInspectionKind, DisplayMemoryInspectionRequest},
    };
    let root = tempfile::tempdir().unwrap();
    platform(root.path());
    put(
        root.path(),
        "proc/iomem",
        include_str!("../../tests/fixtures/spark-2297-after.iomem"),
    );
    let verifier = GrantVerifier::new(&[0; 32], 42).unwrap();
    let mut peer = PeerIdentity {
        uid: 1000,
        primary_gid: 42,
        supplementary_gids: vec![],
    };
    let raw = canonical_generated_json(&DisplayMemoryInspectionRequest {
        kind: DisplayMemoryInspectionKind::DisplayMemory,
    })
    .unwrap();
    assert_eq!(
        inspect(&raw, &verifier, &peer, root.path())
            .unwrap()
            .unwrap()
            .carveout_bytes,
        Some(0x2000_0000)
    );
    peer.primary_gid = 43;
    assert!(inspect(&raw, &verifier, &peer, root.path()).is_err());
    assert!(
        inspect(
            br#"{"kind":"display-memory","path":"/etc/shadow"}"#,
            &verifier,
            &peer,
            root.path()
        )
        .unwrap()
        .is_none()
    );
}

#[test]
fn owner_iomem_dumps_calibrate_only_the_measured_display_reservation() {
    // Break caught: classifying the entire firmware block (or MemTotal) as
    // display RAM, or continuing to advise a fixed 0.5 GB Spark.
    let root = tempfile::tempdir().unwrap();
    platform(root.path());
    for (dump, expected) in [
        (
            include_str!("../../tests/fixtures/spark-2297-before.iomem"),
            0x8000_0000,
        ),
        (
            include_str!("../../tests/fixtures/spark-2297-after.iomem"),
            0x2000_0000,
        ),
    ] {
        put(root.path(), "proc/iomem", dump);
        let observed = observe(root.path(), Some(128_000_000_000));
        assert_eq!(observed.carveout_bytes, Some(expected));
        assert_eq!(observed.signal, DisplayMemorySignal::ReservedRange);
    }
    for dump in [
        "00000000-00000000 : reserved",
        "2c0000000-3237fffff : reserved",
        "2e0000000-3237ffffe : reserved",
        "  2e0000000-3237fffff : reserved",
        "2e0000000-3237fffff : System RAM",
        "280000000-3237fffff : reserved\n2e0000000-3237fffff : reserved",
    ] {
        put(root.path(), "proc/iomem", dump);
        let observed = observe(root.path(), Some(127_600_816 * 1024));
        assert!(observed.carveout_bytes.is_none(), "{dump}");
        assert_eq!(observed.signal, DisplayMemorySignal::Unknown);
    }
    fs::remove_file(root.path().join("proc/iomem")).unwrap();
    assert!(observe(root.path(), None).carveout_bytes.is_none());
}
