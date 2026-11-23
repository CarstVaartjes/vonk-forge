//! Active swap as the kernel reports it, with the helper's last swap receipt.
use std::{fs::File, io::Read, path::Path};

use vonk_agent_protocol::generated::{SwapCheckpoint, SwapObservation};

use vonk_agent_protocol::store_retention::HOST_SETTING_RECEIPT_BYTES as RECEIPT_LIMIT;

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
pub(crate) fn observe(meminfo: &str, helper_root: &Path) -> Option<SwapObservation> {
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
mod tests;
