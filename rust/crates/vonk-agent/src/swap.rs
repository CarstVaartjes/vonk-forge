//! Active swap as the kernel reports it, with the helper's last swap receipt.
use std::{fs::File, io::Read, path::Path};

use vonk_agent_protocol::generated::{SwapCheckpoint, SwapObservation};

const RECEIPT_LIMIT: u64 = 16 * 1024;

fn meminfo_bytes(text: &str, key: &str) -> Option<u64> {
    text.lines().find_map(|line| {
        let mut fields = line.split_ascii_whitespace();
        match (fields.next(), fields.next(), fields.next(), fields.next()) {
            (Some(name), Some(amount), Some("kB"), None) if name == key => {
                amount.parse::<u64>().ok()?.checked_mul(1024)
            }
            _ => None,
        }
    })
}

/// None only when the kernel names no swap figures; a missing or unreadable
/// receipt is reported with its cause, never guessed.
pub fn observe(meminfo: &str, helper_root: &Path) -> Option<SwapObservation> {
    let total_bytes = meminfo_bytes(meminfo, "SwapTotal:")?;
    let free_bytes = meminfo_bytes(meminfo, "SwapFree:")?.min(total_bytes);
    let path = helper_root.join(vonk_agent_protocol::swap::OBSERVATION_FILE);
    let read = (|| -> std::io::Result<Vec<u8>> {
        let mut bytes = Vec::new();
        File::open(&path)?
            .take(RECEIPT_LIMIT + 1)
            .read_to_end(&mut bytes)?;
        Ok(bytes)
    })();
    let (receipt, receipt_detail) = match read {
        Ok(bytes) if bytes.len() as u64 > RECEIPT_LIMIT => (
            None,
            Some(format!("Helper swap receipt exceeds {RECEIPT_LIMIT} bytes")),
        ),
        Ok(bytes) => match vonk_agent_protocol::parse_strict::<SwapCheckpoint>(&bytes) {
            Ok(receipt) => (Some(receipt), None),
            Err(error) => (None, Some(format!("Invalid helper swap receipt: {error}"))),
        },
        Err(error) => (
            None,
            Some(format!(
                "Helper swap receipt unavailable: {}; inventory and hourly helper reconciliation retry",
                error.kind()
            )),
        ),
    };
    Some(SwapObservation {
        total_bytes,
        free_bytes,
        receipt,
        receipt_detail: receipt_detail.map(|detail| detail.chars().take(512).collect()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const MEMINFO: &str =
        "MemTotal:       129170372 kB\nSwapTotal:      16777212 kB\nSwapFree:       16636964 kB\n";

    #[test]
    fn kernel_figures_and_the_helper_receipt_are_reported_as_they_are() {
        let root = tempfile::tempdir().unwrap();
        let missing = observe(MEMINFO, root.path()).unwrap();
        assert_eq!(missing.total_bytes, 16_777_212 * 1024);
        assert_eq!(missing.free_bytes, 16_636_964 * 1024);
        assert!(missing.receipt.is_none());
        assert!(missing.receipt_detail.unwrap().contains("unavailable"));

        let vectors: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/swap-v1.json"
        ))
        .unwrap();
        let receipt = serde_json::to_vec(&vectors["observation"]["receipt"]).unwrap();
        std::fs::write(
            root.path()
                .join(vonk_agent_protocol::swap::OBSERVATION_FILE),
            &receipt,
        )
        .unwrap();
        let observed = observe(MEMINFO, root.path()).unwrap();
        assert_eq!(observed.receipt.unwrap().desired.size_gib, 64);
        assert!(observed.receipt_detail.is_none());

        std::fs::write(
            root.path()
                .join(vonk_agent_protocol::swap::OBSERVATION_FILE),
            b"{}",
        )
        .unwrap();
        let invalid = observe(MEMINFO, root.path()).unwrap();
        assert!(invalid.receipt_detail.unwrap().starts_with("Invalid"));
        assert!(observe("MemTotal: 1 kB\n", root.path()).is_none());
    }
}
