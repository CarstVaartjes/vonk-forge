use std::{
    fs,
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
};

use super::*;

const FRAGMENTED: &str = "Node 0, zone Normal 512 0 0 0 0 0 0 0 0 1 0\n";
const COMPACTED: &str = "Node 0, zone Normal 0 0 0 0 0 0 0 0 0 2 0\n";

fn host() -> tempfile::TempDir {
    let root = tempfile::tempdir().unwrap();
    fs::create_dir_all(root.path().join("proc/sys/vm")).unwrap();
    fs::write(root.path().join("proc/buddyinfo"), FRAGMENTED).unwrap();
    fs::write(root.path().join(COMPACT_MEMORY), b"").unwrap();
    root
}

#[test]
fn a_load_phase_compacts_once_and_every_later_ask_answers_from_it() {
    // Break caught: compacting for each workload of a profile load or on every
    // drop of the load's cadence, or losing the before/after measurement.
    static COMPACTIONS: AtomicUsize = AtomicUsize::new(0);
    let lane = CompactionLane::new(|root| {
        COMPACTIONS.fetch_add(1, Ordering::SeqCst);
        fs::write(root.join("proc/buddyinfo"), COMPACTED)?;
        write_compact_memory(root)
    });
    let root = host();
    assert_eq!(
        lane.for_load(root.path(), false, Duration::from_secs(5), 4096),
        None
    );
    let first = lane
        .for_load(root.path(), true, Duration::from_secs(5), 4096)
        .unwrap();
    assert_eq!(first.outcome, MemoryCompactionOutcome::Completed);
    assert_eq!(fs::read(root.path().join(COMPACT_MEMORY)).unwrap(), b"1");
    assert_eq!(
        first.free_before.as_ref().unwrap().free_2mib_block_bytes,
        2 << 20
    );
    assert_eq!(
        first.free_after.as_ref().unwrap().free_2mib_block_bytes,
        4 << 20
    );
    let later = lane
        .for_load(root.path(), false, Duration::from_secs(5), 4096)
        .unwrap();
    assert_eq!(later, first);
    assert_eq!(COMPACTIONS.load(Ordering::SeqCst), 1);
    // The next load phase compacts again.
    lane.for_load(root.path(), true, Duration::from_secs(5), 4096);
    assert_eq!(COMPACTIONS.load(Ordering::SeqCst), 2);
}

#[test]
fn a_compaction_past_its_bound_answers_at_once_and_is_never_started_twice() {
    // Fault: the kernel's compaction takes longer than the agent may wait.
    static RELEASE: AtomicBool = AtomicBool::new(false);
    static STARTED: AtomicUsize = AtomicUsize::new(0);
    let lane = CompactionLane::new(|root| {
        STARTED.fetch_add(1, Ordering::SeqCst);
        let deadline = Instant::now() + Duration::from_secs(30);
        while !RELEASE.load(Ordering::SeqCst) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        write_compact_memory(root)
    });
    let root = host();
    let begun = Instant::now();
    let first = lane
        .for_load(root.path(), true, Duration::from_millis(50), 4096)
        .unwrap();
    assert!(begun.elapsed() < Duration::from_secs(2));
    assert_eq!(first.outcome, MemoryCompactionOutcome::TimedOut);
    assert_eq!(first.free_after, None);
    // A new load phase while it still runs starts nothing.
    let again = lane
        .for_load(root.path(), true, Duration::from_millis(50), 4096)
        .unwrap();
    assert_eq!(again.outcome, MemoryCompactionOutcome::TimedOut);
    assert_eq!(STARTED.load(Ordering::SeqCst), 1);
    RELEASE.store(true, Ordering::SeqCst);
    let deadline = Instant::now() + Duration::from_secs(5);
    let finished = loop {
        let answer = lane
            .for_load(root.path(), false, Duration::from_millis(50), 4096)
            .unwrap();
        if answer.outcome != MemoryCompactionOutcome::TimedOut || Instant::now() > deadline {
            break answer;
        }
        thread::sleep(Duration::from_millis(5));
    };
    assert_eq!(finished.outcome, MemoryCompactionOutcome::Completed);
    assert!(finished.free_after.is_some());
}

#[test]
fn a_host_without_the_file_is_a_typed_note_not_an_error() {
    // Fault: the unit's /proc/sys is not writable or the file is missing.
    let lane = CompactionLane::new(write_compact_memory);
    let root = tempfile::tempdir().unwrap();
    let missing = lane
        .for_load(root.path(), true, Duration::from_secs(5), 4096)
        .unwrap();
    assert_eq!(missing.outcome, MemoryCompactionOutcome::Failed);
    assert_eq!(missing.free_before, None);
}
