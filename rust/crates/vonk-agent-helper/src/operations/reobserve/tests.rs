#![cfg(test)]
//! What one read of a runtime container by its identity can find.
use super::*;
use crate::operations::test_support::*;

const RUN: &str = "a0000000-0000-4000-8000-00000000000a";
const JOB: &str = "a1000000-0000-4000-8000-0000000000a1";

fn reobserve(host: &Host, run_generation: u64) -> Reobserved {
    let (_temp, executor) = host_executor(host);
    executor.reobserve_by_identity(
        RuntimeEffectIdentity {
            runtime_id: JOB.parse().unwrap(),
            installation_id: INSTALLATION.parse().unwrap(),
            run_generation,
        },
        RUN.parse().unwrap(),
        PLAN_DIGEST,
    )
}

#[test]
fn a_container_is_running_exited_or_proven_absent_by_its_own_identity() {
    let with = |state, exit| {
        Host::with(&[Container {
            exit,
            ..job(RUN, JOB, state)
        }])
    };
    assert!(matches!(
        reobserve(&with("running", 0), 1),
        Reobserved::Running
    ));
    assert!(matches!(
        reobserve(&with("exited", 3), 1),
        Reobserved::Exited { code: 3 }
    ));
    assert!(matches!(reobserve(&Host::default(), 1), Reobserved::Absent));
}

#[test]
fn anything_unproven_stays_unknown_with_its_cause() {
    // An unreadable listing never proves absence.
    let unlisted = Host {
        listing_fails: true,
        ..Host::default()
    };
    assert!(matches!(
        reobserve(&unlisted, 1),
        Reobserved::Unknown(CommandFailure::Exit { code: Some(1), .. })
    ));
    // Created but never started: neither running nor an exit to report.
    assert!(matches!(
        reobserve(&Host::with(&[job(RUN, JOB, "created")]), 1),
        Reobserved::Unknown(CommandFailure::UnexpectedAnswer)
    ));
    // Another generation's container under the name is not this effect.
    assert!(matches!(
        reobserve(&Host::with(&[job(RUN, JOB, "running")]), 2),
        Reobserved::Unknown(CommandFailure::UnexpectedAnswer)
    ));
}
