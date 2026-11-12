//! The Stops this agent completed, for its next inventory to name.
//!
//! The claim lane records each Stop it completes; the inventory lane takes the
//! list before it begins a collection. A collection that names a Stop was taken
//! after that Stop, whatever either clock says, so the Controller admits the
//! replacement without waiting out the clock-skew allowance. Completing a Stop
//! also makes the next collection due at once.

use std::{
    collections::VecDeque,
    sync::{Arc, Mutex, PoisonError},
};

use vonk_agent_protocol::{
    AgentClaim, MAX_REPORTED_STOPPED_RUNS,
    generated::{AgentClaimPayload, AgentOperation, StoppedRun},
};

use crate::outcome::ExecutionResult;

/// Shared between the claim lane and the inventory lane; lives with the process.
#[derive(Clone, Debug, Default)]
pub struct CompletedStops(Arc<Mutex<Ledger>>);

#[derive(Debug, Default)]
struct Ledger {
    /// How many Stops were ever recorded: a change makes a collection due.
    recorded: u64,
    /// The newest Stops, oldest first.
    runs: VecDeque<StoppedRun>,
}

impl CompletedStops {
    /// Record `claim` when it is a Stop whose effect `result` established.
    pub fn observe(&self, claim: &AgentClaim, result: &ExecutionResult) {
        let (AgentOperation::RecipeStop, AgentClaimPayload::RecipeStopPayload(stop)) =
            (&claim.operation, &claim.payload)
        else {
            return;
        };
        if matches!(result, ExecutionResult::Done(_)) {
            self.record(StoppedRun {
                run_id: stop.run_id,
                run_generation: stop.run_generation,
            });
        }
    }

    /// Record one completed Stop, keeping the newest ones.
    pub fn record(&self, run: StoppedRun) {
        let mut ledger = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        ledger.recorded = ledger.recorded.wrapping_add(1);
        ledger.runs.push_back(run);
        while ledger.runs.len() > MAX_REPORTED_STOPPED_RUNS {
            ledger.runs.pop_front();
        }
    }

    /// The count of Stops ever recorded and the newest ones, taken together.
    pub fn snapshot(&self) -> (u64, Vec<StoppedRun>) {
        let ledger = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        (ledger.recorded, ledger.runs.iter().cloned().collect())
    }
}
