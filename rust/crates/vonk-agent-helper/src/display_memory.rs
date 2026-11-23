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
    (total > 0 && total <= vonk_agent_protocol::MAX_TELEMETRY_CAPACITY_BYTES).then_some(total)
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
mod tests;
