//! Orders that run beside the claim lane.
//!
//! The lane claims one order at a time and runs it to its result. A build
//! compiles for hours inside its own units and storage, so it runs beside the
//! lane instead: the Spark keeps claiming probes, transfers, starts and
//! uninstalls meanwhile (owner decision 2026-10-26). The build keeps its own
//! fenced lease, heartbeat, progress and receipt custody, and its memory and
//! disk reservations stay with the Controller's admission. How many builds run
//! at once is the Controller's reservation decision, never a count here.
//!
//! Crash-only: nothing here is durable. After a restart nothing executes, so
//! the observation pass probes a build's journaled claim like any other.

use std::{cell::RefCell, collections::BTreeSet, path::PathBuf, pin::Pin, sync::Mutex, task::Poll};

use futures_util::{Stream, future::LocalBoxFuture, stream::FuturesUnordered};

use super::*;

/// Whether the agent runs `operation` beside its claim lane.
///
/// The Controller holds the same set (`OFF_LANE_OPERATIONS`); both are proven
/// against `agent_protocol/fixtures/off-lane-operations.json`.
pub fn runs_off_lane(operation: &AgentOperation) -> bool {
    matches!(operation, AgentOperation::RecipeBuildV1)
}

/// What one pass of the claim lane leaves its caller.
///
/// An [`OffLane`](LaneStep::OffLane) claim is already in custody: dropping it
/// leaves the order to the observation pass and the Controller's lease.
#[derive(Debug)]
pub enum LaneStep {
    /// Nothing claimed, or the claim ran to its delivered result on the lane.
    Settled,
    /// A claim begun in custody that runs beside the lane.
    OffLane(Box<OffLaneClaim>),
}

/// A claim the lane began in custody and handed off. It is executing from
/// custody on, so no observation pass probes it while it is handed over.
#[derive(Debug)]
pub struct OffLaneClaim {
    claim: AgentClaim,
    schedule: HeartbeatSchedule,
    _executing: Executing,
}

impl OffLaneClaim {
    pub(super) fn new(state: &StateStore, claim: AgentClaim, schedule: HeartbeatSchedule) -> Self {
        let executing = Executing::hold(state, claim.fence);
        Self {
            claim,
            schedule,
            _executing: executing,
        }
    }

    pub fn fence(&self) -> uuid::Uuid {
        self.claim.fence
    }

    /// Execute the claim to its delivered result, exactly as the lane would,
    /// on its own store connection. The lane keeps observing everything else.
    pub async fn execute<C: LoopClient, E: Executor>(
        self,
        client: C,
        mut state: StateStore,
        executor: &E,
    ) -> Result<(), LoopError> {
        execute_claimed(
            &client,
            &mut state,
            executor,
            &self.claim,
            self.schedule,
            false,
        )
        .await
    }
}

/// The off-lane executions in flight, driven alongside the lane in one task
/// (an executor is not `Send`).
#[derive(Default)]
pub struct Beside<'a>(RefCell<FuturesUnordered<LocalBoxFuture<'a, ()>>>);

impl<'a> Beside<'a> {
    /// Run `work` beside the lane; a delivery it could not finish stays in
    /// custody for the lane's retained-receipt pass.
    pub fn start(&self, fence: uuid::Uuid, work: impl Future<Output = Result<(), LoopError>> + 'a) {
        self.0.borrow_mut().push(Box::pin(async move {
            if let Err(error) = work.await {
                eprintln!(
                    "vonk-agent: off-lane order {fence} ended before delivery ({error}); \
                     its receipt stays in custody"
                );
            }
        }));
    }

    /// Drive `lane` to its output while every off-lane execution progresses,
    /// including those `lane` starts.
    pub async fn alongside<F: Future>(&self, lane: F) -> F::Output {
        let mut lane = std::pin::pin!(lane);
        std::future::poll_fn(|context| {
            if let Poll::Ready(output) = lane.as_mut().poll(context) {
                return Poll::Ready(output);
            }
            self.progress(context);
            Poll::Pending
        })
        .await
    }

    /// Wait until every off-lane execution has delivered.
    #[cfg(test)]
    async fn drain(&self) {
        std::future::poll_fn(|context| {
            if self.progress(context) {
                Poll::Ready(())
            } else {
                Poll::Pending
            }
        })
        .await;
    }

    /// Poll the executions once and say whether none is left. One that ended
    /// asks for another pass at once, so the others keep their wakers.
    fn progress(&self, context: &mut std::task::Context<'_>) -> bool {
        match Pin::new(&mut *self.0.borrow_mut()).poll_next(context) {
            Poll::Ready(Some(())) => {
                context.waker().wake_by_ref();
                false
            }
            Poll::Ready(None) => true,
            Poll::Pending => false,
        }
    }
}

/// The claims this process is executing, on the lane or beside it, by journal.
static EXECUTING: Mutex<BTreeSet<(PathBuf, uuid::Uuid)>> = Mutex::new(BTreeSet::new());

/// Holds one journaled claim in [`EXECUTING`] while its executor owns the result.
#[derive(Debug)]
pub(super) struct Executing((PathBuf, uuid::Uuid));

impl Executing {
    pub(super) fn hold(state: &StateStore, fence: uuid::Uuid) -> Self {
        let key = (state.journal().to_path_buf(), fence);
        executing().insert(key.clone());
        Self(key)
    }

    pub(super) fn contains(state: &StateStore, fence: uuid::Uuid) -> bool {
        executing().contains(&(state.journal().to_path_buf(), fence))
    }
}

impl Drop for Executing {
    fn drop(&mut self) {
        executing().remove(&self.0);
    }
}

fn executing() -> std::sync::MutexGuard<'static, BTreeSet<(PathBuf, uuid::Uuid)>> {
    EXECUTING
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

#[cfg(test)]
mod tests;
