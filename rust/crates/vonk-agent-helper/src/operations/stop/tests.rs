#![cfg(test)]

use super::super::test_support::*;
use super::*;

const RUN: &str = "a0000000-0000-4000-8000-00000000000a";
const JOB: &str = "a1000000-0000-4000-8000-0000000000a1";
const OTHER: &str = "b0000000-0000-4000-8000-00000000000b";

fn grant(run_generation: u64) -> RuntimeEffectIdentity {
    RuntimeEffectIdentity {
        runtime_id: JOB.parse().unwrap(),
        installation_id: INSTALLATION.parse().unwrap(),
        run_generation,
    }
}

fn labels(managed: &str, run: &str, target: &str, installation: &str, generation: &str) -> String {
    format!("{managed}\t{run}\t{target}\t{installation}\t{generation}\t{PLAN_DIGEST}\n")
}

/// The container is the grant's own when it is managed, of the same run,
/// target and installation, at the grant's generation or an older one. A
/// newer generation, another identity or an unmanaged container is refused;
/// the plan digest is not compared.
#[test]
fn may_stop_own_identity_at_or_below_the_grant_generation_only() {
    let run = RUN.parse().unwrap();
    let own = |generation| labels("true", RUN, JOB, INSTALLATION, generation);
    assert!(may_stop(&own("1"), grant(2), run), "older generation");
    assert!(may_stop(&own("2"), grant(2), run), "equal generation");
    assert!(!may_stop(&own("3"), grant(2), run), "newer generation");
    let other_digest = format!("true\t{RUN}\t{JOB}\t{INSTALLATION}\t2\t{}", "d".repeat(64));
    assert!(
        may_stop(&other_digest, grant(2), run),
        "plan digest differs"
    );

    let refused = [
        labels("true", OTHER, JOB, INSTALLATION, "2"),
        labels("true", RUN, OTHER, INSTALLATION, "2"),
        labels("true", RUN, JOB, OTHER, "2"),
        labels("", RUN, JOB, INSTALLATION, "2"),
        labels("false", RUN, JOB, INSTALLATION, "2"),
        own("0"),
        own("02"),
        own("+1"),
        own("one"),
        own(""),
        format!("true\t{RUN}\t{JOB}\t{INSTALLATION}\t2"),
        format!("{}\textra", own("2").trim()),
        String::new(),
    ];
    for fields in refused {
        assert!(!may_stop(&fields, grant(2), run), "{fields:?}");
    }
}

/// On a Docker host: a leftover of an older or equal generation of the run is
/// stopped and removed; one of a newer generation or of another run is left
/// untouched and the Stop is refused.
#[test]
fn stop_removes_only_containers_the_grant_owns() {
    let stop = |container_run: &str, container_generation, grant_generation| {
        let host = Host::with(&[Container {
            generation: container_generation,
            ..job(container_run, JOB, "created")
        }]);
        let (_temp, executor) = host_executor(&host);
        let result = executor.runtime_stop_authorized(
            grant(grant_generation),
            RUN.parse().unwrap(),
            1,
            false,
        );
        (result, host)
    };
    for (container_generation, grant_generation) in [(1, 2), (2, 2)] {
        let (result, host) = stop(RUN, container_generation, grant_generation);
        result.unwrap();
        assert!(host.called("stop", JOB).is_some());
        assert!(!host.present(JOB));
    }
    for (container_run, container_generation) in [(RUN, 3), (OTHER, 2)] {
        let (result, host) = stop(container_run, container_generation, 2);
        assert!(matches!(result, Err(OperationError::InvalidArtifact)));
        assert!(host.called("stop", JOB).is_none());
        assert!(host.present(JOB));
    }
}
