//! Read-only evidence. No guessed subtraction of 128 GB minus MemTotal.
use std::{fs, io::Read, path::Path};
use vonk_agent_protocol::generated::{DisplayMemoryObservation, DisplayMemorySignal};

// The SoC ESRT identity, not the EC/USB-PD/UEFI capsule version.
const SOC_FW_CLASS: &str = "b488217b-3895-4fc0-b1bf-ab7005a2d45a";
const MAX_EVIDENCE_BYTES: u64 = 1024 * 1024;

struct Calibration {
    bios_version: &'static str,
    kernel_release: &'static str,
    soc_firmware_version: u32,
    device_tree_memory_bytes: Option<u64>,
    signal: DisplayMemorySignal,
    // MemTotal in bytes, exact reserved range line, or efivar filename.
    candidate: &'static str,
    // For an EFI variable, exact payload hex (excluding its 4 attribute bytes).
    expected: &'static str,
    carveout_bytes: u64,
}

// Intentionally empty until spark-2297 before/after measurements. An arbitrary
// reserved range, hidden /proc/iomem address, or MemTotal delta proves nothing.
const CALIBRATIONS: &[Calibration] = &[];

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

fn matches(root: &Path, observation: &DisplayMemoryObservation, row: &Calibration) -> bool {
    if observation.bios_version.as_deref() != Some(row.bios_version)
        || observation.soc_firmware_version != Some(row.soc_firmware_version)
        || observation.device_tree_memory_bytes != row.device_tree_memory_bytes
        || read(root, "proc/sys/kernel/osrelease").as_deref() != Some(row.kernel_release)
    {
        return false;
    }
    match row.signal {
        DisplayMemorySignal::MemoryTotal => observation
            .memory_total_bytes
            .is_some_and(|value| value.to_string() == row.candidate),
        DisplayMemorySignal::ReservedRange => read(root, "proc/iomem")
            .is_some_and(|text| text.lines().any(|line| line.trim() == row.candidate)),
        DisplayMemorySignal::EfiVariable => {
            // Only a measured variable may be read; never enumerate EFI secrets.
            if row.candidate.contains('/') || row.candidate.contains("..") {
                return false;
            }
            let path = root.join("sys/firmware/efi/efivars").join(row.candidate);
            let Some(bytes) = read_bytes(&path) else {
                return false;
            };
            bytes
                .get(4..)
                .is_some_and(|payload| hex::encode(payload) == row.expected)
        }
        DisplayMemorySignal::Unknown => false,
    }
}

fn classify(root: &Path, observation: &mut DisplayMemoryObservation, rows: &[Calibration]) {
    observation.carveout_bytes = None;
    observation.signal = DisplayMemorySignal::Unknown;
    let mut matched = rows.iter().filter(|row| matches(root, observation, row));
    // Ambiguous/conflicting rules remain unknown instead of first-match wins.
    if let Some(row) = matched.next()
        && matched.next().is_none()
    {
        observation.carveout_bytes = Some(row.carveout_bytes);
        observation.signal = row.signal;
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

pub(crate) fn observe(root: &Path, memory_total_bytes: Option<u64>) -> DisplayMemoryObservation {
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
    classify(root, &mut observation, CALIBRATIONS);
    observation
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
    fn row(signal: DisplayMemorySignal, candidate: &'static str) -> Calibration {
        Calibration {
            bios_version: "measured-bios",
            kernel_release: "measured-kernel",
            soc_firmware_version: 0x02009e0f,
            device_tree_memory_bytes: Some(128 << 30),
            signal,
            candidate,
            expected: "01",
            carveout_bytes: 512 << 20,
        }
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
    fn exact_calibration_rejects_ambiguity_missing_and_changed_platform_evidence() {
        // Break caught: subtracting physical total or treating any reserved range
        // as display memory, or first-match-wins after calibration drift.
        let root = tempfile::tempdir().unwrap();
        platform(root.path());
        for (signal, candidate) in [
            (DisplayMemorySignal::MemoryTotal, "128000000000"),
            (
                DisplayMemorySignal::ReservedRange,
                "1000-1fff : measured-display",
            ),
            (DisplayMemorySignal::EfiVariable, "MeasuredDisplay-guid"),
        ] {
            put(root.path(), "proc/iomem", "1000-1fff : measured-display");
            put(
                root.path(),
                "sys/firmware/efi/efivars/MeasuredDisplay-guid",
                [7, 0, 0, 0, 1],
            );
            let mut observed = observe(root.path(), Some(128_000_000_000));
            classify(root.path(), &mut observed, &[row(signal, candidate)]);
            assert_eq!(observed.carveout_bytes, Some(512 << 20));
            assert_eq!(observed.signal, signal);
            classify(root.path(), &mut observed, &[]);
            assert!(observed.carveout_bytes.is_none());
            assert_eq!(observed.signal, DisplayMemorySignal::Unknown);
            let mut ambiguous = observe(root.path(), Some(128_000_000_000));
            classify(
                root.path(),
                &mut ambiguous,
                &[row(signal, candidate), row(signal, candidate)],
            );
            assert!(ambiguous.carveout_bytes.is_none());
            let mut changed = observe(root.path(), Some(128_000_000_000));
            changed.soc_firmware_version = None;
            classify(root.path(), &mut changed, &[row(signal, candidate)]);
            assert!(changed.carveout_bytes.is_none());
        }
        put(root.path(), "proc/sys/kernel/osrelease", "different-kernel");
        let mut changed = observe(root.path(), Some(128_000_000_000));
        classify(
            root.path(),
            &mut changed,
            &[row(DisplayMemorySignal::MemoryTotal, "128000000000")],
        );
        assert!(changed.carveout_bytes.is_none());
    }
}
