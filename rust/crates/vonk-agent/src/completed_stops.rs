//! The Stops this agent completed, for its next inventory to name.
//!
//! The claim lane records the claim fence of each Stop it completes; the
//! inventory lane takes the list before it begins a collection. A collection
//! that names a fence was taken after that one Stop attempt, whatever either
//! clock says, so the Controller admits the replacement without waiting out
//! the clock-skew allowance. A fence is unique to its attempt: an earlier Stop
//! of the same run never stands in for a later one. Completing a Stop also
//! makes the next collection due at once.

use std::{
    collections::VecDeque,
    sync::{Arc, Mutex, PoisonError},
};

use vonk_agent_protocol::{AgentClaim, MAX_REPORTED_STOP_FENCES, generated::AgentOperation};

use crate::outcome::ExecutionResult;

/// Shared between the claim lane and the inventory lane; lives with the process.
#[derive(Clone, Debug, Default)]
pub struct CompletedStops(Arc<Mutex<Ledger>>);

#[derive(Debug, Default)]
struct Ledger {
    /// How many Stops were ever recorded: a change makes a collection due.
    recorded: u64,
    /// The newest Stops' claim fences, oldest first.
    fences: VecDeque<uuid::Uuid>,
}

impl CompletedStops {
    /// Record `claim` when it is a Stop whose effect `result` established.
    pub fn observe(&self, claim: &AgentClaim, result: &ExecutionResult) {
        if claim.operation == AgentOperation::RecipeStop
            && matches!(result, ExecutionResult::Done(_))
        {
            self.record(claim.fence);
        }
    }

    /// Record one completed Stop, keeping the newest ones.
    pub fn record(&self, fence: uuid::Uuid) {
        let mut ledger = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        ledger.recorded = ledger.recorded.wrapping_add(1);
        ledger.fences.push_back(fence);
        // One in, at most one out: the list never exceeds the wire limit.
        if ledger.fences.len() > MAX_REPORTED_STOP_FENCES {
            ledger.fences.pop_front();
        }
    }

    /// The count of Stops ever recorded and the newest fences, taken together.
    pub fn snapshot(&self) -> (u64, Vec<uuid::Uuid>) {
        let ledger = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        (ledger.recorded, ledger.fences.iter().copied().collect())
    }
}

#[cfg(test)]
mod tests;
