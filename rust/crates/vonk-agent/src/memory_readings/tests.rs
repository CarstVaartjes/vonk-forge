#![cfg(test)]

use super::*;

const BOOT: &str = "6f1c1d8e-0000-4000-8000-000000000001";
const OTHER_BOOT: &str = "6f1c1d8e-0000-4000-8000-000000000002";
const PAGE: u64 = 4096;

fn host(boot: &str, sec_kib: u64, free_pages: u64) -> tempfile::TempDir {
    let root = tempfile::tempdir().unwrap();
    set(root.path(), boot, sec_kib, free_pages);
    root
}

fn set(root: &Path, boot: &str, sec_kib: u64, free_pages: u64) {
    let proc = root.join("proc");
    fs::create_dir_all(proc.join("sys/kernel/random")).unwrap();
    fs::write(proc.join("sys/kernel/random/boot_id"), format!("{boot}\n")).unwrap();
    fs::write(
        proc.join("meminfo"),
        format!("MemTotal: 1 kB\nSecPageTables:  {sec_kib} kB\nPageTables: 5 kB\n"),
    )
    .unwrap();
    // Order 0 and order 9 (2 MiB at 4 KiB pages) blocks.
    fs::write(
        proc.join("buddyinfo"),
        format!("Node 0, zone Normal {free_pages} 0 0 0 0 0 0 0 0 {free_pages}\n"),
    )
    .unwrap();
}

fn sec(point: &Option<MemoryPoint>) -> Option<u64> {
    point.as_ref().and_then(|point| point.sec_page_tables_bytes)
}

#[test]
fn only_a_stop_with_nothing_else_resident_is_an_idle_reading() {
    let root = host(BOOT, 100, 10);
    let data = tempfile::tempdir().unwrap();
    let ledger = Ledger::open(root.path(), data.path(), PAGE);
    let (first, second) = (Uuid::new_v4(), Uuid::new_v4());
    for run in [first, second] {
        ledger.load_begins(run);
        ledger.load_ends();
    }
    set(root.path(), BOOT, 150, 10);
    ledger.stop_begins();
    ledger.stop_ends(first);
    let readings = ledger.snapshot().unwrap();
    assert_eq!(sec(&readings.stop_after), Some(150 * 1024));
    assert!(readings.idle_stop_first.is_none() && readings.idle_stop_latest.is_none());

    set(root.path(), BOOT, 120, 10);
    ledger.stop_ends(second);
    set(root.path(), BOOT, 130, 10);
    ledger.stop_ends(Uuid::new_v4());
    let readings = ledger.snapshot().unwrap();
    assert_eq!(sec(&readings.idle_stop_first), Some(120 * 1024));
    assert_eq!(sec(&readings.idle_stop_latest), Some(130 * 1024));
    assert_eq!(readings.page_size_bytes, 4096);
}

#[test]
fn a_running_job_keeps_a_stop_from_being_idle() {
    let root = host(BOOT, 100, 10);
    let data = tempfile::tempdir().unwrap();
    let ledger = Ledger::open(root.path(), data.path(), PAGE);
    let run = Uuid::new_v4();
    ledger.load_begins(run);
    ledger.job_begins();
    ledger.stop_ends(run);
    assert!(ledger.snapshot().unwrap().idle_stop_latest.is_none());
    ledger.job_ends();
    ledger.stop_ends(run);
    assert!(ledger.snapshot().unwrap().idle_stop_latest.is_some());
}

#[test]
fn the_readings_survive_a_restart_but_not_a_reboot() {
    let root = host(BOOT, 100, 10);
    let data = tempfile::tempdir().unwrap();
    let run = Uuid::new_v4();
    let ledger = Ledger::open(root.path(), data.path(), PAGE);
    ledger.load_begins(run);
    let restarted = Ledger::open(root.path(), data.path(), PAGE);
    assert_eq!(restarted.snapshot(), ledger.snapshot());
    // The restarted agent still knows the run is resident.
    restarted.stop_ends(Uuid::new_v4());
    assert!(restarted.snapshot().unwrap().idle_stop_latest.is_none());

    set(root.path(), OTHER_BOOT, 100, 10);
    assert!(
        Ledger::open(root.path(), data.path(), PAGE)
            .snapshot()
            .is_none()
    );
    let rebooted = Ledger::open(root.path(), data.path(), PAGE);
    rebooted.stop_ends(run);
    let readings = rebooted.snapshot().unwrap();
    assert_eq!(readings.boot_id, OTHER_BOOT);
    assert!(readings.idle_stop_latest.is_some());
}

#[test]
fn a_reading_that_cannot_be_taken_is_absent() {
    assert_eq!(
        sec_page_tables_bytes("SecPageTables: 12 kB\n"),
        Some(12 * 1024)
    );
    assert_eq!(sec_page_tables_bytes("MemTotal: 12 kB\n"), None);
    assert_eq!(sec_page_tables_bytes("SecPageTables: lots kB\n"), None);
    let root = tempfile::tempdir().unwrap();
    let data = tempfile::tempdir().unwrap();
    let ledger = Ledger::open(root.path(), data.path(), PAGE);
    ledger.load_begins(Uuid::new_v4());
    // Without a boot id nothing is recorded: readings of unknown boot would
    // be compared across a reboot.
    assert!(ledger.snapshot().is_none());
}

#[test]
fn the_shared_readings_are_the_contract_the_agent_reports() {
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/memory-fragmentation-v1.json"
    ))
    .unwrap();
    for case in vector["cases"].as_array().unwrap() {
        if case["readings"].is_null() {
            continue;
        }
        let readings: MemoryFragmentation = serde_json::from_value(case["readings"].clone())
            .unwrap_or_else(|error| panic!("{}: {error}", case["name"]));
        let again = serde_json::to_value(&readings).unwrap();
        assert_eq!(again, case["readings"], "{}", case["name"]);
    }
}
