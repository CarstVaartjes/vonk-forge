//! Stall window: an order ends only when its progress stops advancing.
//!
//! The window starts when the order is accepted and restarts each time the
//! order's progress snapshot advances (see [`OperationProgress::advanced_from`]).
//! Lease renewals, retries and elapsed time are not progress.

use super::*;

pub(super) struct StallWindow {
    window: Duration,
    anchor: tokio::time::Instant,
    last: Option<OperationProgress>,
}

impl StallWindow {
    pub(super) fn new(window: Duration) -> Self {
        Self {
            window,
            anchor: tokio::time::Instant::now(),
            last: None,
        }
    }

    /// Sample the order's progress; the window restarts when it advanced.
    pub(super) fn observe(&mut self, current: Option<OperationProgress>) {
        if let Some(current) = current
            && current.advanced_from(self.last.as_ref())
        {
            self.anchor = tokio::time::Instant::now();
            self.last = Some(current);
        }
    }

    /// The instant the order counts as stalled unless it progresses first.
    pub(super) fn deadline(&self) -> tokio::time::Instant {
        self.anchor + self.window
    }

    /// Resolve once a full window passes without progress.
    pub(super) async fn stalled<C: LoopClient>(&mut self, client: &C, fence: uuid::Uuid) {
        let sample = (self.window / 4).max(Duration::from_millis(10));
        loop {
            self.observe(client.progress_observation(fence));
            let now = tokio::time::Instant::now();
            if now >= self.deadline() {
                return;
            }
            tokio::time::sleep_until(self.deadline().min(now + sample)).await;
        }
    }
}
