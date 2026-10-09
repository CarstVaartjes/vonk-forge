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

#[test]
fn malformed_job_wait_reobserves_then_ends_without_poisoning_the_next_job() {
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    #[derive(Clone, Default)]
    struct JobObserver {
        unavailable: Arc<AtomicBool>,
        waits: Arc<AtomicUsize>,
    }
    impl CommandRunner for JobObserver {
        fn run(&self, _: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            if arguments[0] == "wait" {
                let attempt = self.waits.fetch_add(1, Ordering::SeqCst);
                let output = if attempt == 0 || self.unavailable.load(Ordering::SeqCst) {
                    "unreadable peer answer"
                } else {
                    "37"
                };
                return Ok(docker_output(true, output, 0));
            }
            if arguments[0] == "container" && arguments[1] == "inspect" {
                return Ok(docker_output(false, "", 1));
            }
            Ok(docker_output(true, "", 0))
        }
    }
    let temp = tempfile::tempdir().unwrap();
    let observer = JobObserver::default();
    let executor = OperationExecutor::new(
        ManagedRoots::under(temp.path()),
        &[0; 32],
        observer.clone(),
        None,
    )
    .unwrap();
    let identity = runtime_effect_identity(1);
    let result = executor
        .runtime_wait_for_job(
            identity,
            identity.runtime_id,
            &"a".repeat(64),
            1,
            Instant::now(),
        )
        .unwrap();
    assert_eq!(result.0, Some(37));
    assert_eq!(observer.waits.load(Ordering::SeqCst), 2);
    observer.unavailable.store(true, Ordering::SeqCst);
    let before = observer.waits.load(Ordering::SeqCst);
    let began = Instant::now();
    assert!(
        executor
            .runtime_wait_for_job(identity, identity.runtime_id, &"a".repeat(64), 1, began)
            .is_err()
    );
    assert!(began.elapsed() < Duration::from_secs(1));
    assert_eq!(observer.waits.load(Ordering::SeqCst) - before, 3);
    observer.unavailable.store(false, Ordering::SeqCst);
    let fresh = RuntimeEffectIdentity {
        run_generation: 2,
        ..identity
    };
    let result = executor
        .runtime_wait_for_job(fresh, fresh.runtime_id, &"b".repeat(64), 1, Instant::now())
        .unwrap();
    assert_eq!(result.0, Some(37));
}
