//! Memory compaction once at the start of a load phase (owner 2026-11-17).
//!
//! GB10's GPU driver takes unified memory in physically contiguous blocks of
//! 2 MiB or more; on a Spark up for weeks a third of free memory sat in smaller
//! blocks. Writing `1` to `/proc/sys/vm/compact_memory` regroups free memory.
//! The page-cache drop ask is the trigger: the first ask of a load phase
//! compacts, and every later ask in that phase (the other workloads of a
//! profile load, the drop cadence while they load) answers from that result.
//! Nothing here refuses a start: a compaction that cannot happen is reported.
use std::{
    fs::OpenOptions,
    io::{ErrorKind, Write},
    path::Path,
    sync::{Mutex, mpsc},
    thread,
    time::{Duration, Instant},
};

use vonk_agent_protocol::generated::{
    FreeMemory, MemoryCompaction, MemoryCompactionOutcome, ResourceTermProblem,
};

const COMPACT_MEMORY: &str = "proc/sys/vm/compact_memory";

/// One compaction at a time; the last result answers every ask until the
/// next load phase. A compaction that outlives its bound keeps running (the
/// kernel's write cannot be cancelled) and is never started twice.
pub struct CompactionLane {
    state: Mutex<State>,
    compact: fn(&Path) -> std::io::Result<()>,
}

struct State {
    last: Option<MemoryCompaction>,
    /// An ask is waiting on a compaction it started.
    waiting: bool,
    /// A compaction that outlived its bound and still runs.
    overdue: Option<mpsc::Receiver<MemoryCompaction>>,
}

impl CompactionLane {
    pub const fn new(compact: fn(&Path) -> std::io::Result<()>) -> Self {
        Self {
            state: Mutex::new(State {
                last: None,
                waiting: false,
                overdue: None,
            }),
            compact,
        }
    }

    /// Compact when `entering` a load phase and none still runs; otherwise
    /// the last result. The lane is never held while a compaction runs.
    pub fn for_load(
        &self,
        root: &Path,
        entering: bool,
        bound: Duration,
        page_size: u64,
    ) -> Option<MemoryCompaction> {
        let (receiver, provisional) = {
            let mut state = self.lock();
            if let Some(overdue) = state.overdue.take() {
                match overdue.try_recv() {
                    Ok(done) => {
                        log(&done);
                        state.last = Some(done);
                    }
                    Err(mpsc::TryRecvError::Empty) => state.overdue = Some(overdue),
                    Err(mpsc::TryRecvError::Disconnected) => {}
                }
            }
            if !entering || state.waiting || state.overdue.is_some() {
                return state.last.clone();
            }
            let started = Instant::now();
            let provisional = MemoryCompaction {
                started_at: chrono::Utc::now().fixed_offset(),
                outcome: MemoryCompactionOutcome::TimedOut,
                duration_ms: millis(bound),
                free_before: free_memory(root, page_size),
                free_after: None,
            };
            let (done, receiver) = mpsc::channel();
            let (compact, root_path, base) =
                (self.compact, root.to_path_buf(), provisional.clone());
            let spawned = thread::Builder::new()
                .name("vonk-helper-compact".to_owned())
                .spawn(move || {
                    let result = compacted(compact, &root_path, base, started, page_size);
                    if let Err(unread) = done.send(result) {
                        log(&unread.0);
                    }
                });
            if let Err(error) = spawned {
                eprintln!("vonk-agent-helper: memory compaction not started: {error}");
                let failed = MemoryCompaction {
                    outcome: MemoryCompactionOutcome::Failed,
                    duration_ms: 0,
                    ..provisional
                };
                state.last = Some(failed.clone());
                return Some(failed);
            }
            state.waiting = true;
            (receiver, provisional)
        };
        let answer = receiver.recv_timeout(bound);
        let mut state = self.lock();
        state.waiting = false;
        let result = match answer {
            Ok(done) => done,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                state.overdue = Some(receiver);
                provisional
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => MemoryCompaction {
                outcome: MemoryCompactionOutcome::Failed,
                ..provisional
            },
        };
        log(&result);
        state.last = Some(result.clone());
        Some(result)
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

fn compacted(
    compact: fn(&Path) -> std::io::Result<()>,
    root: &Path,
    base: MemoryCompaction,
    started: Instant,
    page_size: u64,
) -> MemoryCompaction {
    let outcome = match compact(root) {
        Ok(()) => MemoryCompactionOutcome::Completed,
        Err(error) => {
            eprintln!("vonk-agent-helper: memory compaction write failed: {error}");
            if matches!(
                error.kind(),
                ErrorKind::PermissionDenied | ErrorKind::ReadOnlyFilesystem
            ) {
                MemoryCompactionOutcome::NotPermitted
            } else {
                MemoryCompactionOutcome::Failed
            }
        }
    };
    MemoryCompaction {
        outcome,
        duration_ms: millis(started.elapsed()),
        free_after: free_memory(root, page_size),
        ..base
    }
}

/// The kernel's compaction of every zone.
pub fn write_compact_memory(root: &Path) -> std::io::Result<()> {
    OpenOptions::new()
        .write(true)
        .open(root.join(COMPACT_MEMORY))?
        .write_all(b"1")
}

fn free_memory(root: &Path, page_size: u64) -> Option<FreeMemory> {
    let text = std::fs::read_to_string(root.join("proc/buddyinfo")).ok()?;
    vonk_agent_protocol::buddyinfo::free_memory(&text, page_size)
}

fn millis(duration: Duration) -> u32 {
    u32::try_from(duration.as_millis()).unwrap_or(u32::MAX)
}

fn log(compaction: &MemoryCompaction) {
    let blocks = |free: &Option<FreeMemory>| {
        free.as_ref().map_or_else(
            || ResourceTermProblem::Unknown.as_str().to_owned(),
            |free| format!("{}/{}", free.free_2mib_block_bytes, free.free_bytes),
        )
    };
    eprintln!(
        "vonk-agent-helper: memory compaction outcome={} duration_ms={} free_2mib_bytes/free_bytes before={} after={}",
        compaction.outcome,
        compaction.duration_ms,
        blocks(&compaction.free_before),
        blocks(&compaction.free_after),
    );
}

#[cfg(test)]
mod tests;
