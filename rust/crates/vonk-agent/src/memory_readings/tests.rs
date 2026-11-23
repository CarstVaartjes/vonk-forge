#![cfg(test)]

use super::*;

const BOOT: &str = "6f1c1d8e-0000-4000-8000-000000000001";
const OTHER_BOOT: &str = "6f1c1d8e-0000-4000-8000-000000000002";
const PAGE: u32 = 4096;

fn set(root: &Path, boot: &str, sec_kib: u64) {
    let proc = root.join("proc");
    fs::create_dir_all(proc.join("sys/kernel/random")).unwrap();
    fs::write(proc.join("sys/kernel/random/boot_id"), format!("{boot}\n")).unwrap();
    fs::write(
        proc.join("meminfo"),
        format!("MemTotal: 1 kB\nSecPageTables:  {sec_kib} kB\nPageTables: 5 kB\n"),
    )
    .unwrap();
}

fn sec(point: &MemoryPoint) -> u64 {
    point.sec_page_tables_bytes
}

#[test]
fn the_first_idle_stop_is_kept_and_the_latest_follows() {
    let root = tempfile::tempdir().unwrap();
    let data = tempfile::tempdir().unwrap();
    set(root.path(), BOOT, 100);
    let ledger = Ledger::open(root.path(), data.path(), PAGE);
    assert!(ledger.snapshot().is_none());
    ledger.stop_ended(true);
    set(root.path(), BOOT, 130);
    // A stop with a GPU process still up is no reading.
    ledger.stop_ended(false);
    set(root.path(), BOOT, 120);
    ledger.stop_ended(true);
    let readings = ledger.snapshot().unwrap();
    assert_eq!(sec(&readings.idle_stop_first), 100 * 1024);
    assert_eq!(sec(&readings.idle_stop_latest), 120 * 1024);
    assert_eq!(readings.page_size_bytes, 4096);
}

#[test]
fn the_readings_survive_a_restart_but_not_a_reboot() {
    let root = tempfile::tempdir().unwrap();
    let data = tempfile::tempdir().unwrap();
    set(root.path(), BOOT, 100);
    let ledger = Ledger::open(root.path(), data.path(), PAGE);
    ledger.stop_ended(true);
    let restarted = Ledger::open(root.path(), data.path(), PAGE);
    assert_eq!(restarted.snapshot(), ledger.snapshot());

    set(root.path(), OTHER_BOOT, 90);
    let rebooted = Ledger::open(root.path(), data.path(), PAGE);
    assert!(rebooted.snapshot().is_none());
    rebooted.stop_ended(true);
    let readings = rebooted.snapshot().unwrap();
    assert_eq!(readings.boot_id, OTHER_BOOT);
    assert_eq!(sec(&readings.idle_stop_first), 90 * 1024);
}

#[test]
fn a_reading_that_cannot_be_taken_is_absent() {
    assert_eq!(
        sec_page_tables_bytes("SecPageTables: 12 kB\n"),
        Some(12 * 1024)
    );
    assert_eq!(sec_page_tables_bytes("MemTotal: 12 kB\n"), None);
    assert_eq!(sec_page_tables_bytes("SecPageTables: lots kB\n"), None);
    // Without a boot id nothing is recorded: readings of unknown boot would
    // be compared across a reboot.
    let root = tempfile::tempdir().unwrap();
    let data = tempfile::tempdir().unwrap();
    let ledger = Ledger::open(root.path(), data.path(), PAGE);
    ledger.stop_ended(true);
    assert!(ledger.snapshot().is_none());
}

#[test]
fn the_shared_readings_are_the_contract_the_agent_reports() {
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/memory-fragmentation-v1.json"
    ))
    .unwrap();
    assert_eq!(
        vector["large_block_bytes"].as_u64(),
        Some(vonk_agent_protocol::buddyinfo::LARGE_BLOCK_BYTES)
    );
    for case in vector["cases"].as_array().unwrap() {
        if case["readings"].is_null() {
            continue;
        }
        let readings: MemoryFragmentation = serde_json::from_value(case["readings"].clone())
            .unwrap_or_else(|error| panic!("{}: {error}", case["name"]));
        assert_eq!(
            serde_json::to_value(&readings).unwrap(),
            case["readings"],
            "{}",
            case["name"]
        );
    }
}
