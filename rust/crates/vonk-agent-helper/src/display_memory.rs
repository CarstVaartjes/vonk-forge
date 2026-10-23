//! Read-only evidence. No guessed subtraction of 128 GB minus MemTotal.
use std::{fs, io::Read, path::Path};
use vonk_agent_protocol::generated::{DisplayMemoryObservation, DisplayMemorySignal};

// The SoC ESRT identity, not the EC/USB-PD/UEFI capsule version.
const SOC_FW_CLASS: &str = "b488217b-3895-4fc0-b1bf-ab7005a2d45a";
const MAX_EVIDENCE_BYTES: u64 = 1024 * 1024;

// Owner measurements, 2026-10-22, spark-2297 before/after and spark-3542.
// The block ending here contains display RAM plus unchanged firmware memory.
const RESERVED_END: u64 = 0x3237fffff;
const OTHER_FIRMWARE_BYTES: u64 = 0x23800000;
const CALIBRATIONS: &[(u64, u64)] = &[(0xA3800000, 0x80000000), (0x43800000, 0x20000000)];

fn read_bytes(path: &Path) -> Option<Vec<u8>> {
    let file = fs::File::open(path).ok()?;
    if file.metadata().ok()?.len() > MAX_EVIDENCE_BYTES {
        return None;
    }
    let mut bytes = Vec::new();
    file.take(MAX_EVIDENCE_BYTES + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    (bytes.len() as u64 <= MAX_EVIDENCE_BYTES).then_some(bytes)
}

fn read(root: &Path, path: impl AsRef<Path>) -> Option<String> {
    Some(
        String::from_utf8(read_bytes(&root.join(path))?)
            .ok()?
            .trim()
            .to_owned(),
    )
}

fn number(text: &str) -> Option<u32> {
    match text.strip_prefix("0x") {
        Some(hex) => u32::from_str_radix(hex, 16).ok(),
        None => text.parse().ok(),
    }
}

fn soc_version(root: &Path) -> Option<u32> {
    let entries = fs::read_dir(root.join("sys/firmware/efi/esrt/entries")).ok()?;
    let mut version = None;
    for entry in entries {
        let entry = entry.ok()?;
        let class = read(root, entry.path().join("fw_class"))?;
        if class.eq_ignore_ascii_case(SOC_FW_CLASS) {
            if version.is_some() {
                return None;
            }
            version = Some(number(&read(root, entry.path().join("fw_version"))?)?);
        }
    }
    version
}

fn display_connected(root: &Path) -> Option<bool> {
    let entries = fs::read_dir(root.join("sys/class/drm")).ok()?;
    let mut known = false;
    let mut unknown = false;
    for entry in entries {
        let Ok(entry) = entry else {
            unknown = true;
            continue;
        };
        // Cards/render nodes have no connector status; connectors contain '-'.
        if !entry.file_name().to_string_lossy().contains('-') {
            continue;
        }
        match read(
            root,
            format!(
                "sys/class/drm/{}/status",
                entry.file_name().to_string_lossy()
            ),
        )
        .as_deref()
        {
            Some("connected") => return Some(true),
            Some("disconnected") => known = true,
            _ => unknown = true,
        }
    }
    (known && !unknown).then_some(false)
}

fn classify(root: &Path, observation: &mut DisplayMemoryObservation) {
    // Only unindented, real reserved addresses count. Unprivileged iomem is
    // zero-masked. MemTotal and unrelated firmware reservations prove nothing.
    let Some(text) =
        read_bytes(&root.join("proc/iomem")).and_then(|bytes| String::from_utf8(bytes).ok())
    else {
        return;
    };
    let mut candidates = text.lines().filter_map(|line| {
        if line.starts_with(char::is_whitespace) {
            return None;
        }
        let (range, label) = line.split_once(" : ")?;
        if label != "reserved" {
            return None;
        }
        let (start, end) = range.split_once('-')?;
        let start = u64::from_str_radix(start, 16).ok()?;
        let end = u64::from_str_radix(end, 16).ok()?;
        (end == RESERVED_END).then_some((start, end))
    });
    let Some((start, end)) = candidates.next() else {
        return;
    };
    if candidates.next().is_some() {
        return;
    }
    let Some(size) = end.checked_sub(start).and_then(|size| size.checked_add(1)) else {
        return;
    };
    if let Some((_, display)) = CALIBRATIONS.iter().find(|(block, _)| *block == size) {
        observation.carveout_bytes = size
            .checked_sub(OTHER_FIRMWARE_BYTES)
            .filter(|bytes| bytes == display);
        if observation.carveout_bytes.is_some() {
            observation.signal = DisplayMemorySignal::ReservedRange;
        }
    }
}

fn device_tree_total(root: &Path) -> Option<u64> {
    // Device-tree memory reg uses the root's address/size cell widths. Do not
    // mistake /proc/iomem's usable System RAM for physical installed memory.
    let base = root.join("sys/firmware/devicetree/base");
    let cells = |name: &str| -> Option<usize> {
        let bytes = read_bytes(&base.join(name))?;
        let value = u32::from_be_bytes(bytes.try_into().ok()?);
        (1..=2).contains(&value).then_some(value as usize)
    };
    let address = cells("#address-cells")?;
    let size = cells("#size-cells")?;
    let mut total = 0u64;
    for entry in fs::read_dir(&base).ok()? {
        let entry = entry.ok()?;
        if !entry.file_name().to_string_lossy().starts_with("memory@") {
            continue;
        }
        let bytes = read_bytes(&entry.path().join("reg"))?;
        let stride = (address + size) * 4;
        if bytes.is_empty() || bytes.len() % stride != 0 {
            return None;
        }
        for region in bytes.chunks_exact(stride) {
            let mut amount = 0u64;
            for cell in region[address * 4..].as_chunks::<4>().0 {
                amount = (amount << 32) | u64::from(u32::from_be_bytes(*cell));
            }
            total = total.checked_add(amount)?;
        }
    }
    (total > 0 && total <= 16 * 1024u64.pow(4)).then_some(total)
}

pub fn observe(root: &Path, memory_total_bytes: Option<u64>) -> DisplayMemoryObservation {
    let mut observation = DisplayMemoryObservation {
        bios_version: read(root, "sys/class/dmi/id/bios_version")
            .filter(|text| !text.is_empty() && text.len() <= 256),
        soc_firmware_version: soc_version(root),
        display_connected: display_connected(root),
        memory_total_bytes,
        device_tree_memory_bytes: device_tree_total(root),
        carveout_bytes: None,
        signal: DisplayMemorySignal::Unknown,
    };
    classify(root, &mut observation);
    observation
}

/// Dispatch the bounded read-only observation through the existing peer gate.
pub fn inspect(
    raw: &[u8],
    verifier: &crate::protocol::GrantVerifier,
    peer: &crate::protocol::PeerIdentity,
    root: &Path,
) -> Result<Option<DisplayMemoryObservation>, crate::protocol::HelperError> {
    use crate::protocol::HelperError;
    let Ok(request) = serde_json::from_slice::<
        vonk_agent_protocol::generated::DisplayMemoryInspectionRequest,
    >(raw) else {
        return Ok(None);
    };
    verifier.authorize_peer(peer)?;
    if vonk_agent_protocol::canonical_generated_json(&request)
        .map_err(|_| HelperError::InvalidMessage)?
        != raw
    {
        return Err(HelperError::InvalidMessage);
    }
    Ok(Some(observe(root, None)))
}

#[cfg(test)]
mod tests {
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
        assert_eq!(observed.soc_firmware_version, Some(0x02009e0f));
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
            Some(0x02009e0f)
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
        };
        let vectors: Vec<HostSettings> = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/display-memory.json"
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
            include_str!("../tests/fixtures/spark-2297-after.iomem"),
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
            Some(0x20000000)
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
                include_str!("../tests/fixtures/spark-2297-before.iomem"),
                0x80000000,
            ),
            (
                include_str!("../tests/fixtures/spark-2297-after.iomem"),
                0x20000000,
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
}
