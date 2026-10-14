//! Work ends only when its progress stops advancing, never on elapsed time.
//!
//! One rule at two scales:
//! - [`StallWindow`]: an order ends only when its reported progress (bytes,
//!   items, phase; see [`OperationProgress::advanced_from`]) stops advancing
//!   for a whole window. Lease renewals, retries and elapsed time are not
//!   progress.
//! - [`Inactivity`]: a transfer ends only when no bytes (or other progress)
//!   moved for a whole window. It is the transport's bound and never a total:
//!   its expiry is a retryable [`Stalled`] observation that names the window
//!   and the bytes that did move.
//!
//! A wait for a slot or for a peer is neither. It is waiting: start no window
//! until the slot is in hand, and run none while a live peer works on bytes
//! it already received ([`Inactivity::wait_on_peer`]).

use std::{
    future::Future,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    task::Poll,
    time::Duration,
};

use futures_util::{Stream, StreamExt};
use tokio::time::Instant;
use vonk_agent_protocol::OperationProgress;

pub(crate) struct StallWindow {
    window: Duration,
    anchor: Instant,
    last: Option<OperationProgress>,
    /// Whether the progress moved past an earlier snapshot, not merely appeared.
    progressed: bool,
}

impl StallWindow {
    pub(crate) fn new(window: Duration) -> Self {
        Self {
            window,
            anchor: Instant::now(),
            last: None,
            progressed: false,
        }
    }

    /// Sample the order's progress; the window restarts when it advanced.
    pub(crate) fn observe(&mut self, current: Option<OperationProgress>) {
        if let Some(current) = current
            && current.advanced_from(self.last.as_ref())
        {
            self.anchor = Instant::now();
            self.progressed |= self.last.is_some();
            self.last = Some(current);
        }
    }

    /// The stall instant once the order has progressed past its first
    /// snapshot; `None` while it has not.
    pub(crate) fn progressed_deadline(&self) -> Option<Instant> {
        self.progressed.then(|| self.deadline())
    }

    /// The instant the order counts as stalled unless it progresses first.
    pub(crate) fn deadline(&self) -> Instant {
        self.anchor + self.window
    }

    /// Resolve once a full window passes without progress.
    pub(crate) async fn stalled(&mut self, progress: impl Fn() -> Option<OperationProgress>) {
        let sample = (self.window / 4).max(Duration::from_millis(10));
        loop {
            self.observe(progress());
            let now = Instant::now();
            if now >= self.deadline() {
                return;
            }
            tokio::time::sleep_until(self.deadline().min(now + sample)).await;
        }
    }
}

/// A transfer made no progress for a whole inactivity window.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("no transfer progress for {} s after {bytes} bytes", window.as_secs())]
pub struct Stalled {
    pub window: Duration,
    pub bytes: u64,
}

/// The inactivity bound of one transfer: `window` after its latest progress.
///
/// Cheap to clone and shared across threads, so a body stream, a blocking
/// worker and the awaiting task observe and check the same window.
#[derive(Clone, Debug)]
pub struct Inactivity {
    window: Duration,
    origin: Instant,
    /// Nanoseconds after `origin` of the latest progress.
    last: Arc<AtomicU64>,
    bytes: Arc<AtomicU64>,
    /// A live peer holds every byte sent and owes the answer: no window runs.
    waiting: Arc<AtomicBool>,
}

impl Inactivity {
    pub fn new(window: Duration) -> Self {
        Self {
            window,
            origin: Instant::now(),
            last: Arc::new(AtomicU64::new(0)),
            bytes: Arc::new(AtomicU64::new(0)),
            waiting: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn window(&self) -> Duration {
        self.window
    }

    /// Progress other than bytes (an item, a phase): the window restarts.
    pub fn observe(&self) {
        self.waiting.store(false, Ordering::Release);
        let elapsed = u64::try_from(self.origin.elapsed().as_nanos()).unwrap_or(u64::MAX);
        self.last.fetch_max(elapsed, Ordering::AcqRel);
    }

    /// `amount` bytes moved: the window restarts. Zero bytes are no progress.
    pub fn observe_bytes(&self, amount: u64) {
        if amount > 0 {
            self.bytes.fetch_add(amount, Ordering::AcqRel);
            self.observe();
        }
    }

    pub fn bytes(&self) -> u64 {
        self.bytes.load(Ordering::Acquire)
    }

    /// The instant the transfer counts as stalled unless it progresses first.
    pub fn deadline(&self) -> Instant {
        self.origin + Duration::from_nanos(self.last.load(Ordering::Acquire)) + self.window
    }

    /// The upload is complete and its peer is verifying it: the answer is
    /// waited for, not timed. Any later progress restarts the window.
    pub fn wait_on_peer(&self) {
        self.waiting.store(true, Ordering::Release);
    }

    pub fn expired(&self) -> bool {
        !self.waiting.load(Ordering::Acquire) && Instant::now() >= self.deadline()
    }

    /// The evidence of this window's expiry.
    pub fn stalled(&self) -> Stalled {
        Stalled {
            window: self.window,
            bytes: self.bytes(),
        }
    }

    /// Resolve once a whole window passes without progress.
    pub async fn idle(&self) {
        loop {
            let deadline = self.deadline();
            if self.waiting.load(Ordering::Acquire) {
                // Re-check after a window: progress may resume the clock.
                tokio::time::sleep(self.window).await;
            } else if Instant::now() >= deadline {
                return;
            } else {
                tokio::time::sleep_until(deadline).await;
            }
        }
    }

    /// Run `work` while it progresses; end it once it stalls for a window.
    pub async fn bound<F: Future>(&self, work: F) -> Result<F::Output, Stalled> {
        tokio::select! {
            biased;
            output = work => Ok(output),
            () = self.idle() => Err(self.stalled()),
        }
    }

    /// Count every chunk of this `length`-byte upload body as progress; once
    /// it is fully sent, wait on the peer that received it. (A transport that
    /// stops reading at the declared length never polls the stream's end.)
    pub fn observe_upload<S, B, E>(
        &self,
        body: S,
        length: u64,
    ) -> impl Stream<Item = Result<B, E>> + use<S, B, E>
    where
        S: Stream<Item = Result<B, E>>,
        B: AsRef<[u8]>,
    {
        let sent = self.clone();
        let complete = self.clone();
        let mut remaining = length;
        body.inspect(move |chunk| {
            if let Ok(chunk) = chunk {
                let bytes = chunk.as_ref().len() as u64;
                sent.observe_bytes(bytes);
                remaining = remaining.saturating_sub(bytes);
                if remaining == 0 {
                    sent.wait_on_peer();
                }
            }
        })
        .chain(futures_util::stream::poll_fn(move |_| {
            complete.wait_on_peer();
            Poll::Ready(None)
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test(start_paused = true)]
    async fn progress_restarts_the_window_and_silence_ends_it() {
        let inactivity = Inactivity::new(Duration::from_secs(10));
        let dripping = async {
            for _ in 0..5 {
                tokio::time::sleep(Duration::from_secs(8)).await;
                inactivity.observe_bytes(1);
            }
            "complete"
        };
        // Forty seconds of slow progress outlive four whole windows.
        assert_eq!(inactivity.bound(dripping).await, Ok("complete"));
        let silent = std::future::pending::<()>();
        let started = Instant::now();
        assert_eq!(
            inactivity.bound(silent).await,
            Err(Stalled {
                window: Duration::from_secs(10),
                bytes: 5
            })
        );
        // The window counts from the last byte, not from the start of the wait.
        assert_eq!(started.elapsed(), Duration::from_secs(10));
    }

    #[tokio::test(start_paused = true)]
    async fn a_peer_verifying_a_complete_upload_is_waited_for() {
        let inactivity = Inactivity::new(Duration::from_secs(1));
        let body = futures_util::stream::iter([Ok::<_, ()>(vec![0_u8; 4])]);
        let mut sent = Box::pin(inactivity.observe_upload(body, 4));
        // The transport stops at the declared length, before the stream ends.
        assert!(sent.next().await.is_some());
        let verifying = tokio::time::sleep(Duration::from_secs(30));
        assert_eq!(inactivity.bound(verifying).await, Ok(()));
        inactivity.observe_bytes(1);
        assert!(
            inactivity
                .bound(std::future::pending::<()>())
                .await
                .is_err()
        );
    }

    #[tokio::test(start_paused = true)]
    async fn zero_bytes_are_not_progress() {
        let inactivity = Inactivity::new(Duration::from_secs(1));
        tokio::time::sleep(Duration::from_millis(900)).await;
        inactivity.observe_bytes(0);
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(inactivity.expired());
    }
}
