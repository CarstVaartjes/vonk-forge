//! Page-cache drops a loading workload's agent asks for (owner 2026-10-27).
//!
//! GB10 memory is unified: the driver and vLLM's free-memory check count the
//! weights a load just read through the page cache as used, and a start dies
//! with NVRM allocation failures while gigabytes are still reclaimable. The
//! agent peer asks; the helper syncs within a bound and drops the clean page
//! cache. Nothing here refuses a start: a drop that cannot happen is reported.
use std::{
    fs::OpenOptions,
    io::{ErrorKind, Write},
    path::Path,
    sync::{Mutex, mpsc},
    thread,
    time::Duration,
};

use vonk_agent_protocol::generated::{
    PageCacheDropProblem, PageCacheDropReport, PageCacheDropRequest, PageCacheSync,
    ResourceTermProblem,
};
use vonk_agent_protocol::host_memory_guard_policy::PAGE_CACHE_SYNC_TIMEOUT;

use crate::host_memory_guard::meminfo_bytes;
use crate::protocol::{GrantVerifier, HelperError, PeerIdentity};

/// `1`: the page cache only. Measured on both Sparks 2026-10-27 at idle:
/// Cached 6.5 and 13.5 GB against SReclaimable 0.4 GB, so dentries and inodes
/// (`3`) add little while the starting container needs them for its opens.
const DROP_PAGE_CACHE: &[u8] = b"1";
const DROP_CACHES: &str = "proc/sys/vm/drop_caches";

/// One drop and one `sync` at a time. A sync still writing back when its
/// bound passes is left to finish and the next drop waits on that same one,
/// so a slow disk never piles up blocked threads. The kernel's drop walk takes
/// each superblock's lock, so a stuck mount can hold one drop forever: a later
/// ask answers at once instead of holding a helper request slot behind it.
pub struct SyncLane {
    pending: Mutex<Option<mpsc::Receiver<()>>>,
    drops: Mutex<()>,
    sync: fn(),
}

impl SyncLane {
    pub const fn new(sync: fn()) -> Self {
        Self {
            pending: Mutex::new(None),
            drops: Mutex::new(()),
            sync,
        }
    }

    /// `None` when no sync could be started.
    pub fn run(&self, bound: Duration) -> Option<PageCacheSync> {
        let mut pending = self
            .pending
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let receiver = match pending.take() {
            Some(receiver) => receiver,
            None => {
                let (done, receiver) = mpsc::channel();
                let sync = self.sync;
                let spawned = thread::Builder::new()
                    .name("vonk-helper-sync".to_owned())
                    .spawn(move || {
                        sync();
                        // The lane keeps the receiver until this answer is read.
                        done.send(()).is_ok()
                    });
                if let Err(error) = spawned {
                    eprintln!(
                        "vonk-agent-helper: page cache sync not started: {error}; the drop proceeds"
                    );
                    return None;
                }
                receiver
            }
        };
        Some(match receiver.recv_timeout(bound) {
            Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => PageCacheSync::Completed,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                *pending = Some(receiver);
                PageCacheSync::TimedOut
            }
        })
    }
}

static SYNC: SyncLane = SyncLane::new(rustix::fs::sync);

/// Sync within `bound`, drop, and read `Cached` around it. Never fails: a drop
/// that could not happen is the report's problem.
pub fn drop_page_cache(
    root: &Path,
    sync: &SyncLane,
    bound: Duration,
    request_id: uuid::Uuid,
) -> PageCacheDropReport {
    let meminfo = || std::fs::read_to_string(root.join("proc/meminfo")).unwrap_or_default();
    let _serial = match sync.drops.try_lock() {
        Ok(guard) => guard,
        Err(std::sync::TryLockError::Poisoned(guard)) => guard.into_inner(),
        Err(std::sync::TryLockError::WouldBlock) => {
            let now = meminfo();
            eprintln!(
                "vonk-agent-helper: page cache drop request={request_id} answered at once: an earlier drop has not returned"
            );
            return PageCacheDropReport {
                request_id,
                dropped_at: chrono::Utc::now().fixed_offset(),
                cached_before_bytes: meminfo_bytes(&now, "Cached:"),
                cached_after_bytes: None,
                mem_available_after_bytes: meminfo_bytes(&now, "MemAvailable:"),
                sync: None,
                problem: Some(PageCacheDropProblem::DropInProgress),
            };
        }
    };
    let before = meminfo();
    let synced = sync.run(bound);
    let problem = OpenOptions::new()
        .write(true)
        .open(root.join(DROP_CACHES))
        .and_then(|mut file| file.write_all(DROP_PAGE_CACHE))
        .err()
        .map(|error| match error.kind() {
            ErrorKind::PermissionDenied | ErrorKind::ReadOnlyFilesystem => {
                PageCacheDropProblem::NotPermitted
            }
            _ => PageCacheDropProblem::DropFailed,
        });
    let after = meminfo();
    let report = PageCacheDropReport {
        request_id,
        dropped_at: chrono::Utc::now().fixed_offset(),
        cached_before_bytes: meminfo_bytes(&before, "Cached:"),
        cached_after_bytes: meminfo_bytes(&after, "Cached:"),
        mem_available_after_bytes: meminfo_bytes(&after, "MemAvailable:"),
        sync: synced,
        problem,
    };
    let known = |value: Option<u64>| {
        value.map_or_else(
            || ResourceTermProblem::Unknown.as_str().to_owned(),
            |bytes| bytes.to_string(),
        )
    };
    let synced = synced
        .map(|sync| format!(" sync={sync}"))
        .unwrap_or_default();
    let refused = report
        .problem
        .map(|problem| format!(" problem={problem}"))
        .unwrap_or_default();
    eprintln!(
        "vonk-agent-helper: page cache drop request={request_id} cached_before_bytes={} cached_after_bytes={} mem_available_after_bytes={}{synced}{refused}",
        known(report.cached_before_bytes),
        known(report.cached_after_bytes),
        known(report.mem_available_after_bytes),
    );
    report
}

/// The agent peer's drop frame, or `None` for any other frame.
pub fn handle(
    raw: &[u8],
    verifier: &GrantVerifier,
    peer: &PeerIdentity,
) -> Result<Option<PageCacheDropReport>, HelperError> {
    let Ok(request) = vonk_agent_protocol::parse_strict::<PageCacheDropRequest>(raw) else {
        return Ok(None);
    };
    verifier.authorize_peer(peer)?;
    Ok(Some(drop_page_cache(
        Path::new("/"),
        &SYNC,
        PAGE_CACHE_SYNC_TIMEOUT,
        request.request_id,
    )))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        os::unix::fs::PermissionsExt,
        sync::atomic::{AtomicBool, AtomicUsize, Ordering},
        time::Instant,
    };

    const MEMINFO: &str =
        "MemTotal: 129170372 kB\nMemAvailable: 110000000 kB\nCached: 13493384 kB\n";

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
        let refused = fs::OpenOptions::new().write(true).open(&target).is_err();
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
            request_id: uuid::Uuid::new_v4(),
        })
        .unwrap();
        assert!(matches!(
            handle(&frame, &verifier, &stranger),
            Err(HelperError::InvalidPeer)
        ));
    }
}
