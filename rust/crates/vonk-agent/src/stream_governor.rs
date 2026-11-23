//! Adaptive bound on the HTTP range streams a model transfer keeps in flight.
//!
//! Every range request of a model or image object holds one permit while it is
//! in progress. The number of permits starts at what a lone transfer is known
//! to sustain, climbs while the aggregate throughput of this agent still grows
//! and returns to the previous value (and holds there for a while) when a
//! step up did not pay off or the Controller answered with a transient error.
//! The governor only counts bytes that are already being written: it never
//! reads, hashes or reorders file contents, and a stream that waits for a
//! permit leaves its partial file exactly as resumable as before.

use std::sync::Mutex;
use std::time::{Duration, Instant};
use tokio::sync::Notify;

/// Streams a lone transfer is known to sustain on a 2.5 GbE NAS.
pub(crate) const INITIAL_STREAMS: usize = 4;
/// Most streams one agent opens, however well throughput keeps growing.
pub(crate) const MAX_STREAMS: usize = 8;
const MIN_STREAMS: usize = 2;
const RAMP_STEP: usize = 2;
const WINDOW: Duration = Duration::from_secs(10);
/// Throughput must grow by this fraction for another step up to be worth it.
const GROWTH_FRACTION: f64 = 0.10;
const HOLD_WINDOWS: u32 = 6;
/// Longest a waiting stream sleeps before it re-reads the limit.
const WAIT_SLICE: Duration = Duration::from_secs(1);

/// What this governor's streams received over the network: every byte once,
/// and the span from the first received byte to the latest.
#[derive(Debug, Default, Clone, Copy)]
struct Fetched {
    bytes: u64,
    first: Option<Instant>,
    last: Option<Instant>,
}

#[derive(Debug)]
struct State {
    max: usize,
    limit: usize,
    active: usize,
    peak_active: usize,
    window_started: Instant,
    window_bytes: u64,
    fetched: Fetched,
    last_rate: Option<f64>,
    previous_limit: Option<usize>,
    hold_windows: u32,
}

impl State {
    fn new(initial: usize, max: usize, now: Instant) -> Self {
        Self {
            max,
            limit: initial.clamp(MIN_STREAMS.min(max), max),
            active: 0,
            peak_active: 0,
            window_started: now,
            window_bytes: 0,
            fetched: Fetched::default(),
            last_rate: None,
            previous_limit: None,
            hold_windows: 0,
        }
    }

    fn reset_window(&mut self, now: Instant) {
        self.window_started = now;
        self.window_bytes = 0;
        self.peak_active = self.active;
    }

    fn step_up(&mut self, rate: f64) {
        self.previous_limit = Some(self.limit);
        self.last_rate = Some(rate);
        self.limit = (self.limit + RAMP_STEP).min(self.max);
    }

    /// Close the measuring window once it has elapsed. Returns whether the
    /// limit changed, so waiting streams can be woken.
    fn evaluate(&mut self, now: Instant) -> bool {
        let elapsed = now.saturating_duration_since(self.window_started);
        if elapsed < WINDOW {
            return false;
        }
        let before = self.limit;
        let rate = self.window_bytes as f64 / elapsed.as_secs_f64();
        let saturated = self.peak_active >= self.limit;
        if self.hold_windows > 0 {
            self.hold_windows -= 1;
            if self.hold_windows == 0 {
                self.last_rate = None;
            }
        } else if let Some(previous) = self.previous_limit {
            // A step up was taken last window: keep it only if it paid off.
            let baseline = self.last_rate.unwrap_or(0.0);
            if rate < baseline * (1.0 + GROWTH_FRACTION) {
                self.limit = previous;
                self.previous_limit = None;
                self.hold_windows = HOLD_WINDOWS;
            } else if saturated && self.limit < self.max {
                self.step_up(rate);
            } else {
                self.last_rate = Some(rate);
                self.previous_limit = None;
            }
        } else if saturated && self.limit < self.max && rate > 0.0 {
            self.step_up(rate);
        }
        self.reset_window(now);
        self.limit != before
    }

    /// Back off after a transient Controller or network failure: halve the
    /// limit and keep the ramp from resuming at full width at once.
    fn throttled(&mut self, now: Instant) -> bool {
        let before = self.limit;
        self.limit = (self.limit / 2).max(MIN_STREAMS.min(self.max));
        self.previous_limit = None;
        self.last_rate = None;
        self.hold_windows = HOLD_WINDOWS;
        self.reset_window(now);
        self.limit != before
    }
}

#[derive(Debug)]
pub(crate) struct StreamGovernor {
    state: Mutex<State>,
    changed: Notify,
}

/// One stream in progress; dropping it frees the slot.
pub(crate) struct StreamPermit<'a> {
    governor: &'a StreamGovernor,
}

impl Drop for StreamPermit<'_> {
    fn drop(&mut self) {
        let mut state = self.governor.lock();
        state.active = state.active.saturating_sub(1);
        drop(state);
        self.governor.changed.notify_waiters();
    }
}

impl Default for StreamGovernor {
    fn default() -> Self {
        Self::new(INITIAL_STREAMS, MAX_STREAMS)
    }
}

impl StreamGovernor {
    pub(crate) fn new(initial: usize, max: usize) -> Self {
        Self {
            state: Mutex::new(State::new(initial, max.max(1), Instant::now())),
            changed: Notify::new(),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    #[cfg(test)]
    pub(crate) fn limit(&self) -> usize {
        self.lock().limit
    }

    /// Wait for a free stream slot.
    pub(crate) async fn acquire(&self) -> StreamPermit<'_> {
        loop {
            let notified = self.changed.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            {
                let mut state = self.lock();
                let changed = state.evaluate(Instant::now());
                if state.active < state.limit {
                    state.active += 1;
                    state.peak_active = state.peak_active.max(state.active);
                    return StreamPermit { governor: self };
                }
                if changed {
                    self.changed.notify_waiters();
                }
            }
            let _ = tokio::time::timeout(WAIT_SLICE, notified).await;
        }
    }

    /// Count bytes already accepted by the writer.
    pub(crate) fn record_bytes(&self, count: u64) {
        let mut state = self.lock();
        let now = Instant::now();
        state.window_bytes = state.window_bytes.saturating_add(count);
        state.fetched.bytes = state.fetched.bytes.saturating_add(count);
        state.fetched.first.get_or_insert(now);
        state.fetched.last = Some(now);
        if state.evaluate(Instant::now()) {
            let limit = state.limit;
            drop(state);
            eprintln!("vonk-agent: model transfer streams now {limit}");
            self.changed.notify_waiters();
        }
    }

    /// Bytes received over the network so far and the time they took.
    pub(crate) fn fetched(&self) -> (u64, Duration) {
        let fetched = self.lock().fetched;
        let span = match (fetched.first, fetched.last) {
            (Some(first), Some(last)) => last.saturating_duration_since(first),
            _ => Duration::ZERO,
        };
        (fetched.bytes, span)
    }

    /// A range failed in a way worth retrying: use fewer streams for a while.
    pub(crate) fn throttled(&self) {
        let mut state = self.lock();
        if state.throttled(Instant::now()) {
            let limit = state.limit;
            drop(state);
            eprintln!(
                "vonk-agent: model transfer streams reduced to {limit} after a transient failure"
            );
        }
    }
}

#[cfg(test)]
mod tests;
