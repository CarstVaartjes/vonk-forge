#![cfg(test)]
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
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/swap-v1.json"
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
