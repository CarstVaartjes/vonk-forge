#![cfg(test)]
//! Wrong implementations: a claim whose deadline passed with nothing live is
//! probed on every pass forever (stale rows cost a probe each and delay live
//! ones), or a claim whose unit is still live leaves observation at its lease
//! deadline so its real exit is never reported.
use super::operation_observation::{
    OperationObservation, PendingObservation, reobserve_operations,
};
use super::test_support::*;
use super::*;
use std::collections::VecDeque;
use std::sync::atomic::AtomicUsize;

#[derive(Clone, Copy)]
enum Seen {
    Live,
    Nothing,
    Exited,
}

/// Answers each probe from a script; its last answer repeats.
struct ScriptedProbe {
    script: Mutex<VecDeque<Seen>>,
    probes: AtomicUsize,
}

impl ScriptedProbe {
    fn new(script: &[Seen]) -> Self {
        Self {
            script: Mutex::new(script.iter().copied().collect()),
            probes: AtomicUsize::new(0),
        }
    }

    fn probes(&self) -> usize {
        self.probes.load(Ordering::SeqCst)
    }
}

#[async_trait(?Send)]
impl Executor for ScriptedProbe {
    async fn observe(&self, claim: &AgentClaim) -> Option<OperationObservation> {
        self.probes.fetch_add(1, Ordering::SeqCst);
        let seen = {
            let mut script = self.script.lock().unwrap();
            if script.len() > 1 {
                script.pop_front().unwrap()
            } else {
                *script.front().unwrap()
            }
        };
        let pending =
            |reason| PendingObservation::new(claim, WaitReason::RuntimeEffectUnconfirmed, reason);
        Some(match seen {
            Seen::Live => OperationObservation::Progressing(pending("a live owned unit")),
            Seen::Nothing => {
                OperationObservation::Unknown(pending("no retained export or live owned unit"))
            }
            Seen::Exited => OperationObservation::confirmed(recipe_install_success(1)),
        })
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

fn pending(store: &StateStore, fence: Uuid) -> Option<AgentResultResult> {
    store
        .pending_results()
        .unwrap()
        .into_iter()
        .find(|(_, result)| result.fence == fence)
        .map(|(_, result)| result.result)
}

async fn pass(store: &mut StateStore, probe: &ScriptedProbe, now: DateTime<Utc>) {
    reobserve_operations(store, probe, None, now).await.unwrap();
}

#[tokio::test]
async fn expired_claim_with_nothing_live_is_probed_once_then_leaves_observation() {
    let root = tempdir().unwrap();
    let mut store = StateStore::open(&root.path().join("state.sqlite"), NODE_ID).unwrap();
    let accepted = claim();
    store.begin(&accepted, Utc::now()).unwrap();
    let probe = ScriptedProbe::new(&[Seen::Nothing]);
    let deadline = accepted.deadline.with_timezone(&Utc);

    pass(&mut store, &probe, deadline - ChronoDuration::seconds(1)).await;
    assert_eq!(probe.probes(), 1, "a claim before its deadline is observed");
    assert!(pending(&store, accepted.fence).is_none());

    pass(&mut store, &probe, deadline).await;
    assert_eq!(probe.probes(), 2, "the deadline pass probes once");
    assert!(
        matches!(
            pending(&store, accepted.fence),
            Some(AgentResultResult::OutcomeUnknown(_))
        ),
        "the final unknown is durable for delivery"
    );

    for later in 1..=3 {
        assert!(store.observation_claims().unwrap().is_empty());
        pass(
            &mut store,
            &probe,
            deadline + ChronoDuration::seconds(later),
        )
        .await;
    }
    assert_eq!(
        probe.probes(),
        2,
        "an ended observation is never probed again"
    );
    assert!(
        matches!(
            pending(&store, accepted.fence),
            Some(AgentResultResult::OutcomeUnknown(_))
        ),
        "ending observation keeps the result in the delivery spool"
    );
}

#[tokio::test]
async fn live_unit_past_its_lease_stays_observed_until_its_exit_is_reported() {
    let root = tempdir().unwrap();
    let mut store = StateStore::open(&root.path().join("state.sqlite"), NODE_ID).unwrap();
    let accepted = claim();
    store.begin(&accepted, Utc::now()).unwrap();
    let probe = ScriptedProbe::new(&[Seen::Live, Seen::Live, Seen::Exited]);
    let deadline = accepted.deadline.with_timezone(&Utc);

    pass(&mut store, &probe, deadline).await;
    pass(&mut store, &probe, deadline + ChronoDuration::minutes(10)).await;
    assert!(
        pending(&store, accepted.fence).is_none(),
        "a live unit is not an unknown outcome"
    );
    assert_eq!(store.observation_claims().unwrap().len(), 1);
    pass(&mut store, &probe, deadline + ChronoDuration::minutes(20)).await;
    assert_eq!(probe.probes(), 3);
    assert!(matches!(
        pending(&store, accepted.fence),
        Some(AgentResultResult::OutcomeDone(_))
    ));
    assert!(store.observation_claims().unwrap().is_empty());
}

#[tokio::test]
async fn recorded_unknown_stays_observed_while_its_unit_is_live() {
    let root = tempdir().unwrap();
    let mut store = StateStore::open(&root.path().join("state.sqlite"), NODE_ID).unwrap();
    let accepted = claim();
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
    let probe = ScriptedProbe::new(&[Seen::Live, Seen::Nothing]);
    let expired = accepted.deadline.with_timezone(&Utc) + ChronoDuration::minutes(1);

    pass(&mut store, &probe, expired).await;
    assert_eq!(store.observation_claims().unwrap().len(), 1);
    pass(&mut store, &probe, expired).await;
    assert!(store.observation_claims().unwrap().is_empty());
    pass(&mut store, &probe, expired).await;
    assert_eq!(probe.probes(), 2);
    assert!(matches!(
        pending(&store, accepted.fence),
        Some(AgentResultResult::OutcomeUnknown(_))
    ));
}

#[test]
fn damaged_stored_deadline_observes_until_the_accepted_deadline() {
    let root = tempdir().unwrap();
    let database = root.path().join("state.sqlite");
    let mut store = StateStore::open(&database, NODE_ID).unwrap();
    let accepted = claim();
    store.begin(&accepted, Utc::now()).unwrap();
    rusqlite::Connection::open(&database)
        .unwrap()
        .execute("UPDATE operations SET deadline='damaged'", [])
        .unwrap();
    let claims = store.observation_claims().unwrap();
    assert_eq!(claims.len(), 1);
    let drift = claims[0].deadline.signed_duration_since(accepted.deadline);
    assert!(drift.num_milliseconds().abs() < 1, "{drift}");
}
