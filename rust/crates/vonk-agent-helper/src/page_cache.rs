//! Page-cache drops a loading workload's agent asks for (owner 2026-10-27).
//!
//! GB10 memory is unified: the driver and vLLM's free-memory check count the
//! weights a load just read through the page cache as used, and a start dies
//! with NVRM allocation failures while gigabytes are still reclaimable. The
//! agent peer asks; the helper syncs within a bound and drops the clean page
//! cache. The first ask of a load phase also compacts memory once
//! (`crate::compaction`). Nothing here refuses a start: a drop that cannot
//! happen is reported.
use std::{
    fs::OpenOptions,
    io::{ErrorKind, Write},
    path::Path,
    sync::{Mutex, mpsc},
    thread,
    time::{Duration, Instant},
};

use vonk_agent_protocol::generated::{
    PageCacheDropProblem, PageCacheDropPurpose, PageCacheDropReport, PageCacheDropRequest,
    PageCacheSync, ResourceTermProblem,
};
use vonk_agent_protocol::host_memory_guard_policy::{
    LOADING_PHASE_WINDOW, MEMORY_COMPACTION_TIMEOUT, PAGE_CACHE_SYNC_TIMEOUT,
};

use crate::compaction::CompactionLane;

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
static COMPACTION: CompactionLane = CompactionLane::new(crate::compaction::write_compact_memory);

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
                compaction: None,
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
        compaction: None,
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

/// The last authorized load-purpose drop ask: the agent asks before each
/// serving start and every PAGE_CACHE_DROP_INTERVAL while any run loads, so
/// this cadence is the one owner of "a load is in progress" for the host memory
/// guard. Hygiene-purpose asks (a job's drops) never set it: R4 stays strict
/// while a job runs.
static LAST_LOAD_ASK: Mutex<Option<Instant>> = Mutex::new(None);

/// Records an ask; true when it starts a load phase. A hygiene ask is no load.
fn record_load_ask(purpose: PageCacheDropPurpose, now: Instant) -> bool {
    record(
        &mut LAST_LOAD_ASK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()),
        purpose,
        now,
    )
}

fn record(last: &mut Option<Instant>, purpose: PageCacheDropPurpose, now: Instant) -> bool {
    purpose == PageCacheDropPurpose::Load && enter(last, now)
}

/// An ask starts a load phase when none came within `LOADING_PHASE_WINDOW`
/// before it; deciding and recording under one lock lets only one of two
/// simultaneous first asks start it.
fn enter(last: &mut Option<Instant>, now: Instant) -> bool {
    let entering = !loading_since(*last, now);
    *last = Some(last.map_or(now, |previous| previous.max(now)));
    entering
}

/// Whether a load asked within `LOADING_PHASE_WINDOW`. A silent agent (dead,
/// restarted) reads as serving: the stricter backstop.
pub fn loading(now: Instant) -> bool {
    loading_since(
        *LAST_LOAD_ASK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()),
        now,
    )
}

fn loading_since(last: Option<Instant>, now: Instant) -> bool {
    last.is_some_and(|ask| now.saturating_duration_since(ask) <= LOADING_PHASE_WINDOW)
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
    // Heard at the ask and again at its answer: a slow drop never reads as a
    // finished load. Only the first ask after a quiet window compacts.
    let entering = record_load_ask(request.purpose, Instant::now());
    let mut report = drop_page_cache(
        Path::new("/"),
        &SYNC,
        PAGE_CACHE_SYNC_TIMEOUT,
        request.request_id,
    );
    // Drop first, then compact: the freed clean cache pages give compaction
    // more free memory to regroup, and pages about to be dropped are never
    // migrated first.
    if request.purpose == PageCacheDropPurpose::Load {
        report.compaction = COMPACTION.for_load(
            Path::new("/"),
            entering,
            MEMORY_COMPACTION_TIMEOUT,
            rustix::param::page_size() as u64,
        );
    }
    record_load_ask(request.purpose, Instant::now());
    Ok(Some(report))
}

#[cfg(test)]
mod tests;
