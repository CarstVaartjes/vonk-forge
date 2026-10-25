#![cfg(test)]
//! Wrong implementation: a claim whose deadline passed is selected and probed
//! on every pass forever, so stale rows cost a probe each and starve live ones.
use super::operation_observation::{
    OperationObservation, PendingObservation, reobserve_operations,
};
use super::test_support::*;
use super::*;
use std::sync::atomic::AtomicUsize;

#[derive(Default)]
struct CountingProbe {
    probes: AtomicUsize,
}

impl CountingProbe {
    fn probes(&self) -> usize {
        self.probes.load(Ordering::SeqCst)
    }
}

#[async_trait(?Send)]
impl Executor for CountingProbe {
    async fn observe(&self, claim: &AgentClaim) -> Option<OperationObservation> {
        self.probes.fetch_add(1, Ordering::SeqCst);
        Some(OperationObservation::Unknown(PendingObservation::new(
            claim,
            WaitReason::RuntimeEffectUnconfirmed,
            "no retained export or live owned unit",
        )))
    }

    async fn execute(
        &self,
        _claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        _cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        panic!("observation never executes an effect")
    }
}

fn pending_unknown(store: &StateStore, fence: Uuid) -> bool {
    store.pending_results().unwrap().iter().any(|(_, result)| {
        result.fence == fence && matches!(result.result, AgentResultResult::OutcomeUnknown(_))
    })
}

#[tokio::test]
async fn expired_claim_is_probed_once_then_leaves_observation() {
    let root = tempdir().unwrap();
    let mut store = StateStore::open(&root.path().join("state.sqlite"), NODE_ID).unwrap();
    let accepted = claim();
    store.begin(&accepted, Utc::now()).unwrap();
    let probe = CountingProbe::default();
    let deadline = accepted.deadline.with_timezone(&Utc);

    reobserve_operations(&mut store, &probe, None, deadline)
        .await
        .unwrap();
    assert_eq!(probe.probes(), 1, "the deadline pass probes once");
    assert!(
        pending_unknown(&store, accepted.fence),
        "the final unknown is durable for delivery"
    );

    for later in 1..=3 {
        let now = deadline + ChronoDuration::seconds(later);
        assert!(store.observation_claims(now).unwrap().is_empty());
        reobserve_operations(&mut store, &probe, None, now)
            .await
            .unwrap();
    }
    assert_eq!(
        probe.probes(),
        1,
        "an expired unknown is never probed again"
    );
    assert!(
        pending_unknown(&store, accepted.fence),
        "ending observation keeps the result in the delivery spool"
    );
}

#[tokio::test]
async fn live_unknown_stays_observed_until_its_deadline() {
    let root = tempdir().unwrap();
    let mut store = StateStore::open(&root.path().join("state.sqlite"), NODE_ID).unwrap();
    let mut accepted = claim();
    // The deadline instant is ahead, but written at an offset whose text sorts
    // before the current UTC time: the cut compares instants, not strings.
    accepted.deadline = (Utc::now() + ChronoDuration::minutes(5))
        .with_timezone(&FixedOffset::west_opt(5 * 3600).unwrap());
    store.begin(&accepted, Utc::now()).unwrap();
    store
        .finish(
            &accepted,
            ExecutionResult::unknown(
                WaitReason::RuntimeEffectUnconfirmed,
                "effect outcome unconfirmed",
                UnknownEvidence::at(FailureStage::AgentRestart),
            ),
        )
        .unwrap();
    let probe = CountingProbe::default();
    let now = Utc::now();
    assert_eq!(store.observation_claims(now).unwrap().len(), 1);
    for _ in 0..2 {
        reobserve_operations(&mut store, &probe, None, now)
            .await
            .unwrap();
    }
    assert_eq!(
        probe.probes(),
        2,
        "a live unknown is observed on every pass"
    );
    let expired = accepted.deadline.with_timezone(&Utc);
    assert!(store.observation_claims(expired).unwrap().is_empty());
    assert!(pending_unknown(&store, accepted.fence));
}
