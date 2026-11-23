#![cfg(test)]
use super::*;
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
    time::Instant,
};

#[test]
fn only_the_first_ask_after_a_quiet_window_starts_a_load_phase() {
    // Break caught: compacting on every drop of a load's cadence, under the
    // workloads already serving.
    use vonk_agent_protocol::host_memory_guard_policy::PAGE_CACHE_DROP_INTERVAL;
    let (mut last, ask) = (None, Instant::now());
    assert!(enter(&mut last, ask));
    assert!(!enter(&mut last, ask + PAGE_CACHE_DROP_INTERVAL));
    assert!(!enter(&mut last, ask + 2 * PAGE_CACHE_DROP_INTERVAL));
    let quiet = ask + 2 * PAGE_CACHE_DROP_INTERVAL + LOADING_PHASE_WINDOW;
    assert!(enter(&mut last, quiet + Duration::from_millis(1)));
}

#[test]
fn hygiene_drops_never_mark_a_load_and_load_drops_do() {
    let (mut last, ask) = (None, Instant::now());
    assert!(!record(&mut last, PageCacheDropPurpose::Hygiene, ask));
    assert!(!loading_since(last, ask));
    assert!(record(&mut last, PageCacheDropPurpose::Load, ask));
    assert!(loading_since(last, ask));
    // A hygiene ask inside a load's window neither extends nor ends it.
    let late = ask + LOADING_PHASE_WINDOW + Duration::from_millis(1);
    assert!(!record(&mut last, PageCacheDropPurpose::Hygiene, late));
    assert!(!loading_since(last, late));
}

#[test]
fn the_drop_cadence_marks_a_load_until_it_falls_silent() {
    let ask = Instant::now();
    assert!(!loading_since(None, ask));
    assert!(loading_since(Some(ask), ask + LOADING_PHASE_WINDOW));
    assert!(!loading_since(
        Some(ask),
        ask + LOADING_PHASE_WINDOW + Duration::from_millis(1)
    ));
    // The agent's longest healthy gap (its interval after an answer that
    // took the whole reply bound) stays inside the window.
    use vonk_agent_protocol::host_memory_guard_policy::{
        PAGE_CACHE_DROP_INTERVAL, PAGE_CACHE_DROP_REPLY_TIMEOUT,
    };
    assert!(LOADING_PHASE_WINDOW > PAGE_CACHE_DROP_INTERVAL + PAGE_CACHE_DROP_REPLY_TIMEOUT);
    // The phase's first ask (sync, drop, compaction) answers before the
    // agent stops waiting: the bounded parts take at most half the reply
    // bound, the rest is headroom for the unbounded drop write and reads.
    assert!(
        (PAGE_CACHE_SYNC_TIMEOUT + MEMORY_COMPACTION_TIMEOUT) * 2 <= PAGE_CACHE_DROP_REPLY_TIMEOUT
    );
}

const MEMINFO: &str = "MemTotal: 129170372 kB\nMemAvailable: 110000000 kB\nCached: 13493384 kB\n";

fn host() -> tempfile::TempDir {
    let root = tempfile::tempdir().unwrap();
    fs::create_dir_all(root.path().join("proc/sys/vm")).unwrap();
    fs::write(root.path().join("proc/meminfo"), MEMINFO).unwrap();
    fs::write(root.path().join(DROP_CACHES), b"").unwrap();
    root
}

#[test]
fn a_drop_syncs_writes_the_page_cache_level_and_reports_cached_bytes() {
    static SYNCS: AtomicUsize = AtomicUsize::new(0);
    let lane = SyncLane::new(|| {
        SYNCS.fetch_add(1, Ordering::SeqCst);
    });
    let root = host();
    let id = uuid::Uuid::new_v4();
    let report = drop_page_cache(root.path(), &lane, Duration::from_secs(5), id);
    assert_eq!(fs::read(root.path().join(DROP_CACHES)).unwrap(), b"1");
    assert_eq!(SYNCS.load(Ordering::SeqCst), 1);
    assert_eq!(report.request_id, id);
    assert_eq!(report.sync, Some(PageCacheSync::Completed));
    assert_eq!(report.problem, None);
    assert_eq!(report.cached_before_bytes, Some(13_493_384 * 1024));
    assert_eq!(report.mem_available_after_bytes, Some(110_000_000 * 1024));
}

#[test]
fn a_sync_that_outlives_its_bound_never_holds_the_drop_or_a_second_thread() {
    // A disk that never finishes writing back: the drop still happens at
    // the bound, and the next drop waits on the same sync, not a new one.
    static RELEASE: AtomicBool = AtomicBool::new(false);
    static STARTED: AtomicUsize = AtomicUsize::new(0);
    let lane = SyncLane::new(|| {
        STARTED.fetch_add(1, Ordering::SeqCst);
        let deadline = Instant::now() + Duration::from_secs(30);
        while !RELEASE.load(Ordering::SeqCst) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
    });
    let root = host();
    let begun = Instant::now();
    let first = drop_page_cache(
        root.path(),
        &lane,
        Duration::from_millis(50),
        uuid::Uuid::new_v4(),
    );
    assert!(begun.elapsed() < Duration::from_secs(2));
    assert_eq!(first.sync, Some(PageCacheSync::TimedOut));
    assert_eq!(first.problem, None);
    assert_eq!(fs::read(root.path().join(DROP_CACHES)).unwrap(), b"1");
    let second = drop_page_cache(
        root.path(),
        &lane,
        Duration::from_millis(50),
        uuid::Uuid::new_v4(),
    );
    assert_eq!(second.sync, Some(PageCacheSync::TimedOut));
    assert_eq!(STARTED.load(Ordering::SeqCst), 1);
    RELEASE.store(true, Ordering::SeqCst);
    let third = drop_page_cache(
        root.path(),
        &lane,
        Duration::from_secs(5),
        uuid::Uuid::new_v4(),
    );
    assert_eq!(third.sync, Some(PageCacheSync::Completed));
    assert_eq!(STARTED.load(Ordering::SeqCst), 1);
}

#[test]
fn an_ask_behind_a_drop_that_never_returns_is_answered_at_once() {
    // Fault: a stuck mount holds the kernel's drop walk. The next ask must
    // not take a helper request slot until that drop returns.
    static LANE: SyncLane = SyncLane::new(|| {});
    let lane = &LANE;
    let root = host();
    let (held, release, returned) = (mpsc::channel(), mpsc::channel::<()>(), mpsc::channel());
    let (holding, waiting, returning) = (held.0, release.1, returned.0);
    thread::spawn(move || {
        {
            let _drop = LANE.drops.lock().unwrap();
            holding.send(()).unwrap();
            waiting.recv_timeout(Duration::from_secs(30)).unwrap();
        }
        returning.send(()).unwrap();
    });
    held.1.recv_timeout(Duration::from_secs(5)).unwrap();
    let begun = Instant::now();
    let report = drop_page_cache(
        root.path(),
        lane,
        Duration::from_secs(5),
        uuid::Uuid::new_v4(),
    );
    assert!(begun.elapsed() < Duration::from_secs(1));
    assert_eq!(report.problem, Some(PageCacheDropProblem::DropInProgress));
    assert_eq!(report.sync, None);
    assert_eq!(fs::read(root.path().join(DROP_CACHES)).unwrap(), b"");
    release.0.send(()).unwrap();
    returned.1.recv_timeout(Duration::from_secs(5)).unwrap();
    let next = drop_page_cache(
        root.path(),
        lane,
        Duration::from_secs(5),
        uuid::Uuid::new_v4(),
    );
    assert_eq!(next.problem, None);
}

#[test]
fn a_drop_the_host_refuses_is_a_typed_note_not_an_error() {
    // Fault: the unit's /proc/sys is read-only or the write is refused.
    let lane = SyncLane::new(|| {});
    let root = host();
    let target = root.path().join(DROP_CACHES);
    fs::set_permissions(&target, fs::Permissions::from_mode(0o000)).unwrap();
    let refused = OpenOptions::new().write(true).open(&target).is_err();
    let report = drop_page_cache(
        root.path(),
        &lane,
        Duration::from_secs(1),
        uuid::Uuid::new_v4(),
    );
    if refused {
        assert_eq!(report.problem, Some(PageCacheDropProblem::NotPermitted));
    }
    fs::remove_file(&target).unwrap();
    let missing = drop_page_cache(
        root.path(),
        &lane,
        Duration::from_secs(1),
        uuid::Uuid::new_v4(),
    );
    assert_eq!(missing.problem, Some(PageCacheDropProblem::DropFailed));
    assert_eq!(missing.cached_after_bytes, Some(13_493_384 * 1024));
}

#[test]
fn only_a_drop_frame_from_the_agent_group_is_answered() {
    let verifier = GrantVerifier::new(&[7; 32], 4242).unwrap();
    let agent = PeerIdentity {
        uid: 1000,
        primary_gid: 4242,
        supplementary_gids: vec![],
    };
    let stranger = PeerIdentity {
        primary_gid: 1,
        ..agent.clone()
    };
    let other_frame = br#"{"kind":"display-memory"}"#;
    assert!(handle(other_frame, &verifier, &agent).unwrap().is_none());
    let frame = vonk_agent_protocol::canonical_generated_json(&PageCacheDropRequest {
        kind: vonk_agent_protocol::generated::PageCacheDropKind::PageCacheDrop,
        purpose: PageCacheDropPurpose::Load,
        request_id: uuid::Uuid::new_v4(),
    })
    .unwrap();
    assert!(matches!(
        handle(&frame, &verifier, &stranger),
        Err(HelperError::InvalidPeer)
    ));
}
