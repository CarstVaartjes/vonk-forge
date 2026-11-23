//! Process liveness: a subprocess deadline that only ends a stalled child.
//!
//! A child's window restarts whenever it shows progress: stdout/stderr bytes,
//! growth of its output file or bounded directory, or a new low of free space
//! on its reserved filesystem. Every such advance is also reported to the
//! activity sink of the calling thread, so the owning order counts a silent
//! compile as progress.

use std::{
    cell::RefCell,
    marker::PhantomData,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

/// Receives the amount of subprocess progress observed for one order.
pub type ActivitySink = Arc<dyn Fn(u64) + Send + Sync>;

thread_local! {
    static ACTIVITY: RefCell<Option<ActivitySink>> = const { RefCell::new(None) };
}

/// Reports this thread's subprocess progress to a sink until dropped.
///
/// Thread-scoped on purpose: concurrent lanes (inventory, rotation) run their
/// own processes on other threads and must not keep a stalled order alive.
#[must_use]
pub struct ActivityScope {
    previous: Option<ActivitySink>,
    _thread_bound: PhantomData<*const ()>,
}

pub fn report_activity_to(sink: ActivitySink) -> ActivityScope {
    ActivityScope {
        previous: ACTIVITY.with(|current| current.replace(Some(sink))),
        _thread_bound: PhantomData,
    }
}

impl Drop for ActivityScope {
    fn drop(&mut self) {
        let previous = self.previous.take();
        ACTIVITY.with(|current| *current.borrow_mut() = previous);
    }
}

/// Report progress made on this thread outside a subprocess (e.g. hashing).
pub fn note_activity(amount: u64) {
    if amount > 0 {
        ACTIVITY.with(|current| {
            if let Some(sink) = current.borrow().as_ref() {
                sink(amount);
            }
        });
    }
}

/// A deadline `window` after the child's latest progress.
pub(super) struct Liveness {
    started: Instant,
    window: Duration,
    last_progress_nanos: AtomicU64,
    progress: AtomicU64,
}

impl Liveness {
    pub(super) fn new(window: Duration) -> Arc<Self> {
        Arc::new(Self {
            started: Instant::now(),
            window,
            last_progress_nanos: AtomicU64::new(0),
            progress: AtomicU64::new(0),
        })
    }

    /// The child advanced by `amount` (bytes of output or growth).
    pub(super) fn advance(&self, amount: u64) {
        if amount == 0 {
            return;
        }
        self.progress.fetch_add(amount, Ordering::AcqRel);
        let elapsed = u64::try_from(self.started.elapsed().as_nanos()).unwrap_or(u64::MAX);
        self.last_progress_nanos
            .fetch_max(elapsed, Ordering::AcqRel);
    }

    pub(super) fn deadline(&self) -> Instant {
        self.started
            + Duration::from_nanos(self.last_progress_nanos.load(Ordering::Acquire))
            + self.window
    }

    pub(super) fn expired(&self) -> bool {
        Instant::now() >= self.deadline()
    }

    /// Forward progress seen since the previous call to the thread's sink.
    /// Called on the owning thread; capture threads only `advance`.
    pub(super) fn report(&self, reported: &mut u64) {
        let progress = self.progress.load(Ordering::Acquire);
        note_activity(progress.saturating_sub(*reported));
        *reported = progress;
    }
}

#[cfg(test)]
mod tests;
