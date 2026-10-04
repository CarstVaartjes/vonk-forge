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

#[derive(Debug)]
struct State {
    max: usize,
    limit: usize,
    active: usize,
    peak_active: usize,
    window_started: Instant,
    window_bytes: u64,
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
        state.window_bytes = state.window_bytes.saturating_add(count);
        if state.evaluate(Instant::now()) {
            let limit = state.limit;
            drop(state);
            eprintln!("vonk-agent: model transfer streams now {limit}");
            self.changed.notify_waiters();
        }
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
mod tests {
    use super::*;

    const MIB: u64 = 1024 * 1024;

    /// Run one measuring window at `rate` bytes per second with every stream busy.
    fn window(state: &mut State, now: &mut Instant, rate: u64) -> bool {
        state.active = state.limit;
        state.peak_active = state.limit;
        state.window_bytes += rate * WINDOW.as_secs();
        *now += WINDOW;
        state.evaluate(*now)
    }

    fn state() -> (State, Instant) {
        let now = Instant::now();
        (State::new(INITIAL_STREAMS, MAX_STREAMS, now), now)
    }

    #[test]
    fn climbs_while_throughput_grows_to_the_cap() {
        let (mut state, mut now) = state();
        assert_eq!(state.limit, 4);
        window(&mut state, &mut now, 100 * MIB);
        assert_eq!(state.limit, 6);
        window(&mut state, &mut now, 150 * MIB);
        assert_eq!(state.limit, 8);
        window(&mut state, &mut now, 200 * MIB);
        assert_eq!(state.limit, 8, "never above the cap");
    }

    #[test]
    fn returns_to_the_previous_limit_and_holds_when_a_step_did_not_pay() {
        let (mut state, mut now) = state();
        window(&mut state, &mut now, 100 * MIB);
        assert_eq!(state.limit, 6);
        // Only 5% more: not worth six streams.
        window(&mut state, &mut now, 105 * MIB);
        assert_eq!(state.limit, 4);
        // The ramp stays paused for the hold, however saturated the streams are.
        for _ in 0..HOLD_WINDOWS {
            window(&mut state, &mut now, 100 * MIB);
            assert_eq!(state.limit, 4);
        }
        window(&mut state, &mut now, 100 * MIB);
        assert_eq!(state.limit, 6, "the ramp resumes after the hold");
    }

    #[test]
    fn does_not_climb_while_streams_are_idle() {
        let (mut state, mut now) = state();
        state.active = 1;
        state.peak_active = 1;
        state.window_bytes = 100 * MIB * WINDOW.as_secs();
        now += WINDOW;
        assert!(!state.evaluate(now));
        assert_eq!(state.limit, 4);
    }

    #[test]
    fn a_window_is_evaluated_only_when_it_has_elapsed() {
        let (mut state, now) = state();
        state.active = 4;
        state.peak_active = 4;
        state.window_bytes = 100 * MIB;
        assert!(!state.evaluate(now + WINDOW / 2));
        assert_eq!(state.limit, 4);
    }

    #[test]
    fn transient_failures_halve_the_limit_down_to_the_floor() {
        let (mut state, mut now) = state();
        window(&mut state, &mut now, 100 * MIB);
        window(&mut state, &mut now, 150 * MIB);
        assert_eq!(state.limit, 8);
        assert!(state.throttled(now));
        assert_eq!(state.limit, 4);
        assert!(state.throttled(now));
        assert_eq!(state.limit, 2);
        assert!(!state.throttled(now));
        assert_eq!(state.limit, 2);
    }

    #[tokio::test]
    async fn waits_for_a_slot_and_wakes_when_one_is_released() {
        let governor = StreamGovernor::new(2, 2);
        let first = governor.acquire().await;
        let _second = governor.acquire().await;
        let waiting = tokio::time::timeout(Duration::from_millis(100), governor.acquire()).await;
        assert!(waiting.is_err(), "a third stream waits at a limit of two");
        drop(first);
        let third = tokio::time::timeout(Duration::from_secs(5), governor.acquire())
            .await
            .expect("a released slot wakes a waiting stream");
        drop(third);
        assert_eq!(governor.limit(), 2);
    }
}
