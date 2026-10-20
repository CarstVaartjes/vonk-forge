#![cfg(test)]
//! What a returning helper does with job containers it no longer owns.
use super::*;
use crate::operations::test_support::*;

const RUN_A: &str = "a0000000-0000-4000-8000-00000000000a";
const RUN_B: &str = "b0000000-0000-4000-8000-00000000000b";
const JOB_A: &str = "a1000000-0000-4000-8000-0000000000a1";
const JOB_B: &str = "b1000000-0000-4000-8000-0000000000b1";
const SERVING: &str = "c0000000-0000-4000-8000-00000000000c";

fn executor(host: &Host) -> (TempDir, OperationExecutor<Host>) {
    host_executor(host)
}

/// `count` exited job containers, each of its own run, as leaked Starts leave
/// them.
fn leaked_jobs(count: u32) -> Vec<Container> {
    (0..count)
        .map(|n| {
            job(
                &format!("e{n:07x}-0000-4000-8000-{n:012x}"),
                &format!("d{n:07x}-0000-4000-8000-{n:012x}"),
                "exited",
            )
        })
        .collect()
}

fn identity(target: &str) -> RuntimeEffectIdentity {
    RuntimeEffectIdentity {
        runtime_id: target.parse().unwrap(),
        installation_id: INSTALLATION.parse().unwrap(),
        run_generation: 1,
    }
}

#[test]
fn a_returning_helper_removes_exited_jobs_and_never_a_serving_run() {
    let host = Host::with(&[
        job(RUN_B, JOB_B, "exited"),
        job(SERVING, SERVING, "running"),
    ]);
    let (_temp, executor) = executor(&host);
    // The exited job's completion was recorded before the helper went away.
    let completion = executor.roots.agent_data.join("runs").join(JOB_B);
    fs::create_dir_all(&completion).unwrap();
    fs::write(completion.join("job-exit.json"), b"{}").unwrap();

    executor.sweep_stray_jobs();

    assert!(
        !host.present(JOB_B),
        "an exited job container was left behind"
    );
    assert!(host.present(SERVING));
    assert_eq!(host.called("stop", SERVING), None);
    assert_eq!(host.called("rm", SERVING), None);
    // Its output is read before the container that holds it is removed.
    assert!(host.called("logs", JOB_B).unwrap() < host.called("rm", JOB_B).unwrap());
    // The agent's completion record is kept for the same job's replay; the
    // sweep removes only the container.
    assert!(
        completion.join("job-exit.json").is_file(),
        "the sweep removed the agent's completion record"
    );
}

/// A helper restart or package upgrade ends the helper's wait, never the job:
/// the running job survives the sweep, and a later wait of the same identity
/// reports its own exit and removes it.
#[test]
fn a_running_job_survives_a_helper_restart_and_is_adopted_by_its_identity() {
    let host = Host::with(&[Container {
        exit: 3,
        ..job(RUN_A, JOB_A, "running")
    }]);
    let (_temp, executor) = executor(&host);

    executor.sweep_stray_jobs();
    assert!(
        host.present(JOB_A),
        "a running job was removed at helper start"
    );
    assert_eq!(host.called("stop", JOB_A), None);

    let outcome = executor
        .runtime_wait_for_job(
            identity(JOB_A),
            RUN_A.parse().unwrap(),
            PLAN_DIGEST,
            60,
            Instant::now(),
        )
        .unwrap();
    assert_eq!(outcome.exit_code, Some(3));
    assert!(!outcome.stop_uncertain);
    assert!(
        outcome.evidence.is_some(),
        "its failed exit kept no evidence"
    );
    assert!(!host.present(JOB_A), "the adopted job was not removed");
}

#[test]
fn a_job_a_live_start_still_owns_is_left_to_that_start() {
    let host = Host::with(&[job(RUN_A, JOB_A, "running")]);
    let (_temp, executor) = executor(&host);
    let _owner = executor.job_cancellation.begin(identity(JOB_A)).unwrap();

    executor.sweep_stray_jobs();
    assert!(host.present(JOB_A));
    assert_eq!(host.called("stop", JOB_A), None);

    // Its run's next job waits for it instead of running beside it.
    assert!(matches!(
        executor.clear_stray_jobs_for_run(identity(JOB_B), RUN_A.parse().unwrap()),
        Err(OperationError::StopUncertain)
    ));
    assert!(host.present(JOB_A));
}

#[test]
fn a_stray_that_cannot_be_removed_holds_only_its_own_runs_next_job_until_it_is() {
    let host = Host::with(&[
        job(RUN_A, JOB_A, "running"),
        job(SERVING, SERVING, "running"),
    ]);
    *host.stop_fails.lock().unwrap() = true;
    let (_temp, executor) = executor(&host);

    executor.sweep_stray_jobs();
    assert!(host.present(JOB_A));
    assert!(host.present(SERVING));

    let next = identity(JOB_B);
    // The refusal carries Docker's own answer: its status and its reason.
    let refused = executor
        .clear_stray_jobs_for_run(next, RUN_A.parse().unwrap())
        .unwrap_err();
    assert!(matches!(
        refused,
        OperationError::CommandFailed {
            cause: CommandFailure::Exit { code: Some(1), ref stderr },
        } if stderr == STOP_REFUSAL
    ));
    // Another run's job, and the serving run, are not held by it.
    assert!(
        executor
            .clear_stray_jobs_for_run(next, RUN_B.parse().unwrap())
            .is_ok()
    );
    assert_eq!(host.called("stop", SERVING), None);

    // Once Docker stops it, the run's next job proceeds without it.
    *host.stop_fails.lock().unwrap() = false;
    executor
        .clear_stray_jobs_for_run(next, RUN_A.parse().unwrap())
        .unwrap();
    assert!(!host.present(JOB_A));
    assert!(host.present(SERVING));
}

#[test]
fn an_unreadable_container_listing_is_never_taken_as_no_strays() {
    let host = Host {
        listing_fails: true,
        ..Host::with(&[job(RUN_A, JOB_A, "running")])
    };
    let (_temp, executor) = executor(&host);

    executor.sweep_stray_jobs();
    assert!(host.present(JOB_A));
    assert!(
        executor
            .clear_stray_jobs_for_run(identity(JOB_B), RUN_A.parse().unwrap())
            .is_err()
    );
    // The deferral carries Docker's answer, not a bare command failure.
    let failure = executor.list_job_containers(None, None).unwrap_err();
    assert!(matches!(
        failure,
        CommandFailure::Exit { code: Some(1), .. }
    ));
}

#[test]
fn a_returning_helper_clears_strays_however_many_managed_containers_the_host_holds() {
    // Forty leaked job containers beside a serving run: a listing line per
    // container no longer fits the helper's captured-output bound.
    let mut containers = vec![job(SERVING, SERVING, "running")];
    containers.extend(leaked_jobs(40));
    let host = Host::with(&containers);
    let (_temp, executor) = executor(&host);

    executor.sweep_stray_jobs();

    let remaining: Vec<_> = host
        .containers
        .lock()
        .unwrap()
        .iter()
        .map(|container| container.target.clone())
        .collect();
    assert_eq!(
        remaining,
        [SERVING],
        "leaked job containers were left behind"
    );
    assert_eq!(host.called("stop", SERVING), None);
}

#[test]
fn one_sweep_continues_until_all_pages_are_reached() {
    let mut containers = vec![job(SERVING, SERVING, "running")];
    containers.extend(leaked_jobs(100));
    let host = Host::with(&containers);
    let (_temp, executor) = executor(&host);

    executor.sweep_stray_jobs();
    assert_eq!(host.containers.lock().unwrap().len(), 1);
    assert!(host.present(SERVING));
}

#[test]
fn protected_front_page_does_not_starve_cleanup_of_the_tail() {
    let mut containers = leaked_jobs(1);
    let tail = containers[0].target.clone();
    containers.extend((0..MAX_LISTED).map(|n| {
        job(
            &format!("f{n:07x}-0000-4000-8000-{n:012x}"),
            &format!("f{n:07x}-0000-4000-8000-{n:012x}"),
            "running",
        )
    }));
    let host = Host::with(&containers);
    let (_temp, executor) = executor(&host);
    executor.sweep_stray_jobs();
    assert!(!host.present(&tail));
    assert_eq!(host.containers.lock().unwrap().len(), MAX_LISTED);
}

#[test]
fn owned_front_page_refuses_start_but_does_not_starve_removable_tail() {
    let mut containers: Vec<_> = leaked_jobs(MAX_LISTED as u32 + 1)
        .into_iter()
        .map(|mut job| {
            job.run = RUN_A.to_owned();
            job
        })
        .collect();
    let protected = containers.last_mut().unwrap();
    let target = protected.target.clone();
    let host = Host::with(&containers);
    let (_temp, executor) = executor(&host);
    let owner = executor.job_cancellation.begin(identity(&target)).unwrap();
    assert!(
        executor
            .clear_stray_jobs_for_run(identity(JOB_B), RUN_A.parse().unwrap())
            .is_err()
    );
    assert_eq!(host.containers.lock().unwrap().len(), 1);
    assert!(host.present(&target));
    drop(owner);
    executor
        .clear_stray_jobs_for_run(identity(JOB_B), RUN_A.parse().unwrap())
        .unwrap();
    assert!(host.containers.lock().unwrap().is_empty());
}
