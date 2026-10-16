#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn a_process_exit_always_carries_logs_or_a_typed_reason_none_were_read() {
    // The class guard: every way the exit can be observed yields a failure
    // with its logs, or the reason they are missing, plus the exit account.
    let output = |success: bool, stdout: &str, stderr: &str| CommandOutput {
        success,
        stdout: stdout.as_bytes().to_vec(),
        stderr: stderr.as_bytes().to_vec(),
        exit_code: None,
    };
    let state = crate::runtime_logs::ContainerExit {
        exit_code: Some(137),
        oom_killed: Some(true),
        error: String::new(),
        host_memory: None,
    };
    let logs: Vec<Result<CommandOutput, String>> = vec![
        Ok(output(true, "", "")),
        Ok(output(true, "a\n", "b\n")),
        Ok(output(false, "", "")),
        Err("spawn failed".to_owned()),
    ];
    for log in logs {
        for exit in [
            Ok(state.clone()),
            Err("the container exit inspection failed"),
        ] {
            let OperationError::RuntimeProcessExited {
                logs,
                capture_error,
                exit_summary,
            } = super::process_exited(log.clone(), exit)
            else {
                panic!("a process exit must stay a process exit");
            };
            assert!(
                logs.is_some() || capture_error.is_some(),
                "no logs and no reason: {exit_summary}"
            );
            assert!(exit_summary.contains("exit_cause="), "{exit_summary}");
        }
    }
}

#[test]
fn name_only_run_check_reports_absent_stopped_running_and_refuses_the_rest() {
    let run_id = "ab69f1ba-1fa2-4283-bdf1-a6d2ed59549c";
    let id = "a".repeat(64);
    let check = |inspect: CommandOutput, listing: CommandOutput, run: &str| {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        OperationExecutor::new(roots, &[0; 32], NameOnlyRunner { inspect, listing }, None)
            .unwrap()
            .runtime_run_container_running(run)
    };
    // Removed by hand: docker cannot inspect it and no listing has it.
    assert!(matches!(
        check(
            docker_output(false, "", 1),
            docker_output(true, "", 0),
            run_id
        ),
        Ok(false)
    ));
    // A failed listing never proves absence.
    assert!(
        check(
            docker_output(false, "", 1),
            docker_output(false, "", 1),
            run_id
        )
        .is_err()
    );
    let line = |running: &str, managed: &str, label: &str| {
        docker_output(true, &format!("{id}\t{running}\t{managed}\t{label}\n"), 0)
    };
    let none = docker_output(true, "", 0);
    assert!(matches!(
        check(line("true", "true", run_id), none.clone(), run_id),
        Ok(true)
    ));
    assert!(matches!(
        check(line("false", "true", run_id), none.clone(), run_id),
        Ok(false)
    ));
    // A foreign container under the name, or a non-canonical id, is refused.
    assert!(check(line("false", "false", run_id), none.clone(), run_id).is_err());
    assert!(check(line("false", "true", "other"), none.clone(), run_id).is_err());
    assert!(check(none.clone(), none, "AB69F1BA-1FA2-4283-BDF1-A6D2ED59549C").is_err());
}

#[test]
fn stop_rejects_unproven_container_absence_even_when_daemon_is_healthy() {
    let temp = tempfile::tempdir().unwrap();
    let executor = OperationExecutor::new(
        ManagedRoots::under(temp.path()),
        &[0; 32],
        DeniedContainerInspectRunner,
        None,
    )
    .unwrap();

    assert!(
        executor
            .runtime_stop_once(
                runtime_effect_identity(1),
                uuid::Uuid::parse_str(RUN_ID).unwrap(),
                &"a".repeat(64),
                5,
            )
            .is_err()
    );
    let fresh = OperationExecutor::new(
        ManagedRoots::under(temp.path()),
        &[0; 32],
        MissingContainerRunner,
        None,
    )
    .unwrap();
    fresh
        .runtime_stop_once(
            runtime_effect_identity(2),
            uuid::Uuid::parse_str(RUN_ID).unwrap(),
            &"b".repeat(64),
            5,
        )
        .unwrap();
}

#[test]
fn job_wait_preserves_only_bounded_container_exit_statuses() {
    assert_eq!(
        bounded_container_wait_exit_code(&CommandOutput {
            success: true,
            stdout: b"37\n".to_vec(),
            exit_code: Some(0),
            stderr: Vec::new(),
        }),
        Some(37)
    );
    for output in [b"".as_slice(), b"-1", b"256", b"invalid"] {
        assert_eq!(
            bounded_container_wait_exit_code(&CommandOutput {
                success: true,
                stdout: output.to_vec(),
                exit_code: Some(0),
                stderr: Vec::new(),
            }),
            None
        );
    }
}

/// An unread `docker wait` concludes nothing: the job is read again by its
/// identity and waited for while it runs; its own recorded exit is reported,
/// and a container proven gone leaves the exit unknown -- never a made-up one.
#[test]
fn an_unread_job_wait_is_reobserved_by_identity_before_anything_is_concluded() {
    const RUN: &str = "a0000000-0000-4000-8000-00000000000a";
    const JOB: &str = "a1000000-0000-4000-8000-0000000000a1";
    let identity = RuntimeEffectIdentity {
        runtime_id: JOB.parse().unwrap(),
        installation_id: INSTALLATION.parse().unwrap(),
        run_generation: 1,
    };
    let wait = |host: &Host| {
        let (_temp, executor) = host_executor(host);
        executor.runtime_wait_for_job(
            identity,
            RUN.parse().unwrap(),
            PLAN_DIGEST,
            60,
            Instant::now(),
        )
    };

    let running = Host::with(&[Container {
        exit: 37,
        ..job(RUN, JOB, "running")
    }]);
    *running.unreadable_waits.lock().unwrap() = 2;
    assert_eq!(wait(&running).unwrap().exit_code, Some(37));

    let exited = Host::with(&[Container {
        exit: 0,
        ..job(RUN, JOB, "exited")
    }]);
    *exited.unreadable_waits.lock().unwrap() = u32::MAX;
    let outcome = wait(&exited).unwrap();
    assert_eq!(outcome.exit_code, Some(0));
    assert!(outcome.evidence.is_none());
    assert!(!exited.present(JOB));

    assert!(matches!(
        wait(&Host::default()),
        Err(OperationError::RuntimeJobWaitFailed { evidence: None })
    ));
}

/// A measured exit is kept when the container cannot be removed afterwards;
/// the reply says the stop is uncertain instead of replacing the exit.
#[test]
fn a_measured_job_exit_survives_a_failed_removal() {
    const RUN: &str = "a0000000-0000-4000-8000-00000000000a";
    const JOB: &str = "a1000000-0000-4000-8000-0000000000a1";
    let host = Host::with(&[Container {
        exit: 5,
        ..job(RUN, JOB, "running")
    }]);
    *host.stop_fails.lock().unwrap() = true;
    let (_temp, executor) = host_executor(&host);
    let outcome = executor
        .runtime_wait_for_job(
            RuntimeEffectIdentity {
                runtime_id: JOB.parse().unwrap(),
                installation_id: INSTALLATION.parse().unwrap(),
                run_generation: 1,
            },
            RUN.parse().unwrap(),
            PLAN_DIGEST,
            60,
            Instant::now(),
        )
        .unwrap();
    assert_eq!(outcome.exit_code, Some(5));
    assert!(outcome.stop_uncertain);
    assert!(outcome.evidence.is_some());
}

/// At its deadline a job is ended by the helper: a running one reports the
/// helper-ended exit with its output; one never read stays unknown -- its
/// exit is not made up.
#[test]
fn a_job_at_its_deadline_is_ended_and_an_unread_one_keeps_its_exit_unknown() {
    const RUN: &str = "a0000000-0000-4000-8000-00000000000a";
    const JOB: &str = "a1000000-0000-4000-8000-0000000000a1";
    let expired = |host: &Host| {
        let (_temp, executor) = host_executor(host);
        executor.runtime_wait_for_job(
            RuntimeEffectIdentity {
                runtime_id: JOB.parse().unwrap(),
                installation_id: INSTALLATION.parse().unwrap(),
                run_generation: 1,
            },
            RUN.parse().unwrap(),
            PLAN_DIGEST,
            1,
            Instant::now() - Duration::from_secs(1),
        )
    };
    let running = Host::with(&[job(RUN, JOB, "running")]);
    let outcome = expired(&running).unwrap();
    assert_eq!(
        outcome.exit_code,
        Some(super::super::start::JOB_ENDED_BY_HELPER_EXIT_CODE)
    );
    assert!(outcome.evidence.is_some());
    assert!(!running.present(JOB));

    let unlisted = Host {
        listing_fails: true,
        ..Host::with(&[job(RUN, JOB, "running")])
    };
    assert!(matches!(
        expired(&unlisted),
        Err(OperationError::RuntimeJobWaitFailed { evidence: Some(_) })
    ));
    assert!(!unlisted.present(JOB));
}
