#![cfg(test)]
//! A job's exit outlives the container the helper removes: the agent's
//! re-observation by the job's identity reads it after the removal.
use super::*;
use crate::operations::test_support::*;
use std::os::unix::fs::PermissionsExt;

const RUN: &str = "a0000000-0000-4000-8000-00000000000a";
const JOB: &str = "a1000000-0000-4000-8000-0000000000a1";
const NEXT_JOB: &str = "a2000000-0000-4000-8000-0000000000a2";

fn identity(target: &str) -> RuntimeEffectIdentity {
    RuntimeEffectIdentity {
        runtime_id: target.parse().unwrap(),
        installation_id: INSTALLATION.parse().unwrap(),
        run_generation: 1,
    }
}

/// The agent's grant-free re-observation of `JOB` at `generation`.
fn reobserve(
    executor: &OperationExecutor<Host>,
    generation: &str,
) -> Result<RunInspection, OperationError> {
    reobserve_mode(
        executor,
        generation,
        vonk_agent_protocol::JOB_REOBSERVATION_ARGUMENT,
    )
}

fn reobserve_mode(
    executor: &OperationExecutor<Host>,
    generation: &str,
    marker: &str,
) -> Result<RunInspection, OperationError> {
    let request = HostRuntimeRequest {
        action: HostRuntimeAction::RunInspect,
        fence: uuid::Uuid::new_v4(),
        arguments: [marker, JOB, RUN, INSTALLATION, generation, PLAN_DIGEST]
            .map(str::to_owned)
            .to_vec(),
        job_plan: None,
        installation_id: None,
        reconciliation_identity: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    };
    let body = canonical_json(&request).unwrap();
    let digest = hex_sha256(&body);
    fs::create_dir_all(&executor.roots.runtime_requests).unwrap();
    let path = executor
        .roots
        .runtime_requests
        .join(format!("{digest}.json"));
    fs::write(&path, &body).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    executor.inspect_recipe_run_with_logs(&digest, true)
}

fn exited_job(exit: i32) -> Host {
    Host::with(&[Container {
        exit,
        ..job(RUN, JOB, "exited")
    }])
}

/// The helper restarted while the job ran and the agent never recorded its
/// exit: the sweep journals and removes the exited container, and the
/// agent's re-observation still reads the exit, not an absence.
#[test]
fn a_swept_job_exit_is_read_by_its_re_observation() {
    let host = exited_job(3);
    let (_temp, executor) = host_executor(&host);

    executor.sweep_stray_jobs();
    assert!(!host.present(JOB), "the exited job was not swept");

    let seen = reobserve(&executor, "1").expect("the retained exit, not an absence");
    assert!(!seen.running);
    assert_eq!(seen.exit_code, Some(3));
    assert!(seen.logs.is_some(), "a failed exit keeps its output");
    assert!(seen.log_error.is_some_and(|summary| summary.contains('3')));
    // Only exactly this job's identity reads it.
    assert!(matches!(
        reobserve(&executor, "2"),
        Err(OperationError::RuntimeRunMissing)
    ));
}

/// The helper's own wait measured the exit and removed the container, but
/// its answer never reached the agent (the agent restarted mid-job).
#[test]
fn an_exit_whose_answer_was_lost_is_read_by_its_re_observation() {
    let host = Host::with(&[Container {
        exit: 0,
        ..job(RUN, JOB, "running")
    }]);
    let (_temp, executor) = host_executor(&host);

    let answered = executor
        .runtime_wait_for_job(
            identity(JOB),
            RUN.parse().unwrap(),
            PLAN_DIGEST,
            60,
            Instant::now(),
        )
        .unwrap();
    assert_eq!(answered.exit_code, Some(0));
    assert!(!host.present(JOB));

    let seen = reobserve(&executor, "1").expect("the retained exit, not an absence");
    assert_eq!(seen.exit_code, Some(0));
    // A clean exit carries no evidence, as a live re-observation answers it.
    assert!(seen.logs.is_none() && seen.log_error.is_none());
}

/// A run's next job removes an earlier exited one; that one's exit stays
/// readable for its own owner.
#[test]
fn an_exit_removed_before_the_runs_next_job_is_read_by_its_re_observation() {
    let host = exited_job(7);
    let (_temp, executor) = host_executor(&host);

    executor
        .clear_stray_jobs_for_run(identity(NEXT_JOB), RUN.parse().unwrap())
        .unwrap();
    assert!(!host.present(JOB));

    assert_eq!(reobserve(&executor, "1").unwrap().exit_code, Some(7));
}

/// A running job stopped before its run's next job never ran to an exit of
/// its own: nothing is retained for it.
#[test]
fn a_stopped_running_job_retains_no_invented_exit() {
    let host = Host::with(&[job(RUN, JOB, "running")]);
    let (_temp, executor) = host_executor(&host);

    executor
        .clear_stray_jobs_for_run(identity(NEXT_JOB), RUN.parse().unwrap())
        .unwrap();
    assert!(!host.present(JOB));

    assert!(matches!(
        reobserve(&executor, "1"),
        Err(OperationError::RuntimeRunMissing)
    ));
}

/// Stop observes physical absence even when a JobRun's retained exit exists.
#[test]
fn runtime_absence_probe_never_substitutes_a_retained_exit_or_removes_a_container() {
    let host = exited_job(3);
    let (_temp, executor) = host_executor(&host);
    let observed = reobserve_mode(
        &executor,
        "1",
        vonk_agent_protocol::RUNTIME_REOBSERVATION_ARGUMENT,
    )
    .unwrap();
    assert_eq!(observed.exit_code, Some(3));
    assert!(host.present(JOB), "observation cannot stop or remove");
    executor.sweep_stray_jobs();
    assert!(
        reobserve(&executor, "1").is_ok(),
        "job retains its real exit"
    );
    assert!(matches!(
        reobserve_mode(
            &executor,
            "1",
            vonk_agent_protocol::RUNTIME_REOBSERVATION_ARGUMENT
        ),
        Err(OperationError::RuntimeRunMissing)
    ));
    assert!(matches!(
        reobserve_mode(
            &executor,
            "2",
            vonk_agent_protocol::RUNTIME_REOBSERVATION_ARGUMENT
        ),
        Err(OperationError::RuntimeRunMissing)
    ));
}
