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

/// One container's streams through the helper's real `docker logs` capture
/// (`package_command::run_quiet`, kept as `commands.rs` hands it on), so a
/// test sees exactly the bytes `process_exited` is given.
fn through_docker_logs_capture(stdout: &[u8], stderr: &[u8]) -> CommandOutput {
    let directory = tempfile::tempdir().unwrap();
    let (out, err) = (directory.path().join("out"), directory.path().join("err"));
    std::fs::write(&out, stdout).unwrap();
    std::fs::write(&err, stderr).unwrap();
    let mut command = std::process::Command::new("/bin/sh");
    command
        .args(["-c", "cat \"$1\"; cat \"$2\" >&2", "sh"])
        .arg(&out)
        .arg(&err);
    let result =
        crate::package_command::run_quiet(&mut command, std::time::Duration::from_secs(30))
            .unwrap();
    CommandOutput {
        success: result.status.success() && !result.timed_out,
        stdout: result.stdout,
        stderr: result.stderr,
        exit_code: result.status.code(),
    }
}

fn exited(code: i32) -> crate::runtime_logs::ContainerExit {
    crate::runtime_logs::ContainerExit {
        exit_code: Some(code),
        oom_killed: Some(false),
        error: String::new(),
        host_memory: None,
    }
}

fn summary_of(
    log: CommandOutput,
    code: i32,
) -> (
    vonk_agent_protocol::generated::HostHelperProcessLogs,
    String,
) {
    let OperationError::RuntimeProcessExited {
        logs, exit_summary, ..
    } = super::process_exited(Ok(log), Ok(exited(code)))
    else {
        panic!("a process exit must stay a process exit");
    };
    (*logs.expect("captured logs are carried"), exit_summary)
}

#[test]
fn a_cause_the_retained_window_dropped_still_classifies_the_exit() {
    // Shape of the two-Spark vLLM start on GB10 (qwen3-8-flash-next tonyd2wild
    // dual, operation 05f6227e, 11-02, vLLM 8a728663).  stdout: the worker's
    // `logger.exception("WorkerProc hit an exception.")` record
    // (multiproc_executor.py:1055) with hundreds of C++ frames, then the
    // engine's `logger.exception("EngineCore failed to start.")` record
    // (core.py:1385) closing in `RuntimeError: Worker failed with error
    // '<str(e)>', please check ...`, then the executor's "died unexpectedly"
    // error (multiproc_executor.py:314).  The capture keeps stdout's last
    // 32 KiB, which cuts the worker's record but keeps the engine's whole.
    // stderr is what the evidence shows of it: warnings, frames, the pointer.
    let worker = "(Worker_TP0 pid=250) ERROR 11-02 23:40:35 [multiproc_executor.py:1055] ";
    let engine = "(EngineCore pid=176) ERROR 11-02 23:40:35 [core.py:1385] ";
    let cuda = [
        "CUDA kernel errors might be asynchronously reported at some other API call, so the stacktrace below might be incorrect.",
        "For debugging consider passing CUDA_LAUNCH_BLOCKING=1",
        "Compile with `TORCH_USE_CUDA_DSA` to enable device-side assertions.",
        "",
        "Exception raised from c10_cuda_check_implementation at /pytorch/c10/cuda/CUDAException.cpp:44 (most recent call first):",
    ];
    let mut stdout = String::new();
    for line in [
        "WorkerProc hit an exception.",
        "Traceback (most recent call last):",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/worker/workspace.py\", line 207, in _ensure_workspace_size",
        "    self._current_workspaces[workspace_id] = torch.empty(",
        "torch.AcceleratorError: CUDA error: no kernel image is available for execution on the device",
    ]
    .into_iter()
    .chain(cuda)
    {
        stdout.push_str(&format!("{worker}{line}\n"));
    }
    for frame in 0..300 {
        stdout.push_str(&format!(
            "{worker}frame #{frame}: <unknown function> + 0x1357680 (0xf07b08327680 in /usr/local/lib/python3.12/dist-packages/torch/lib/libtorch_cpu.so)\n"
        ));
    }
    for line in [
        "EngineCore failed to start.",
        "Traceback (most recent call last):",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/executor/multiproc_executor.py\", line 436, in get_response",
        "    raise RuntimeError(",
        "RuntimeError: Worker failed with error 'CUDA error: no kernel image is available for execution on the device",
    ]
    .into_iter()
    .chain(cuda)
    {
        stdout.push_str(&format!("{engine}{line}\n"));
    }
    for frame in 0..63 {
        stdout.push_str(&format!(
            "{engine}frame #{frame}: PyObject_Call + 0x118 (0x4c5108 in VLLM::Worker_TP0)\n"
        ));
    }
    stdout.push_str(&format!(
        "{engine}', please check the stack trace above for the root cause\n\
NCCL version 2.30.7+cuda12.9\n\
(EngineCore pid=176) ERROR 11-02 23:40:39 [multiproc_executor.py:314] Worker proc VllmWorker-0 died unexpectedly (exit code: None), shutting down executor.\n"
    ));
    let raw = "(EngineCore pid=176) ";
    let mut stderr = format!(
        "{raw}/usr/local/lib/python3.12/dist-packages/torch/cuda/__init__.py:63: FutureWarning: The pynvml package is deprecated.\n"
    )
    .repeat(110);
    for frame in 0..63 {
        stderr.push_str(&format!(
            "{raw}frame #{frame}: PyObject_Call + 0x118 (0x4c5108 in VLLM::Worker_TP0)\n"
        ));
    }
    stderr.push_str(&format!(
        "{raw}', please check the stack trace above for the root cause\n"
    ));
    let log = through_docker_logs_capture(stdout.as_bytes(), stderr.as_bytes());
    // Preconditions: the real bounds, the worker's record cut by the capture,
    // the engine's kept whole, and windows that hold no cause.
    let bound = crate::package_command::STREAM_RETAINED_BYTES;
    let captured = String::from_utf8_lossy(&log.stdout).into_owned();
    assert_eq!(
        log.stdout.len(),
        bound,
        "stdout is cut to the capture's tail"
    );
    assert!(!captured.contains("WorkerProc hit an exception"));
    assert!(!captured.contains("torch.AcceleratorError"));
    assert!(captured.contains("EngineCore failed to start."));
    assert!(log.stderr.len() <= bound, "stderr is captured whole");
    let (retained, summary) = summary_of(log, 1);
    assert!(retained.stdout.truncated);
    // As retained on hardware: the engine's opening line ahead of the
    // omission marker, the tail of its frames and the executor's last word.
    assert!(retained.stdout.text.contains("EngineCore failed to start."));
    assert!(retained.stdout.text.contains("bytes omitted]"));
    assert!(retained.stdout.text.ends_with("shutting down executor.\n"));
    for window in [&retained.stdout.text, &retained.stderr.text] {
        assert!(!window.contains("no kernel image"), "{window}");
    }
    assert!(
        summary.contains("exit_cause=unsupported_gpu_arch"),
        "{summary}"
    );
    assert!(
        summary.contains(
            "exception=\"RuntimeError: Worker failed with error 'CUDA error: no kernel image is available"
        ),
        "{summary}"
    );
}

#[test]
fn an_incidental_line_far_above_never_names_the_exit() {
    // Wrong implementation this catches: matching the whole capture, so a
    // benign warning far above the failure outranked the closing traceback.
    let mut stderr = String::from(
        "WARNING 11-02 23:40:01 [nvme.py:41] could not read /sys/class/nvme/nvme0/serial: No such file or directory\n\
WARNING 11-02 23:40:01 [numa.py:12] set_mempolicy: invalid argument, keeping the default policy\n",
    );
    stderr.push_str(
        &"INFO 11-02 23:40:02 [loader.py:88] Loading safetensors checkpoint shard\n".repeat(150),
    );
    let mut traceback = stderr.clone();
    traceback.push_str(
        "Traceback (most recent call last):\n  File \"/app/serve.py\", line 9, in main\n    check(config)\nValueError: max_model_len 999999 exceeds the model's maximum of 262144\n",
    );
    let log = through_docker_logs_capture(b"", traceback.as_bytes());
    assert!(log.stderr.len() <= crate::package_command::STREAM_RETAINED_BYTES);
    let (retained, summary) = summary_of(log, 1);
    assert!(
        !retained.stderr.text.contains("No such file"),
        "precondition"
    );
    assert!(summary.contains("exit_cause=python_exception"), "{summary}");
    // With no traceback at all the warnings still name nothing.
    let (_, summary) = summary_of(through_docker_logs_capture(b"", stderr.as_bytes()), 1);
    assert!(summary.contains("exit_cause=unclassified"), "{summary}");
}

#[test]
fn a_recovered_traceback_far_above_never_names_the_exit() {
    // Wrong implementation this catches: reading the last traceback anywhere
    // in the capture.  A workload logs tracebacks it recovers from (a refused
    // rendezvous, a missing optional file), runs on, and later exits on a
    // one-line message with no traceback -- a partition, not the recipe.
    // With no pointer above in the window, the capture is never consulted.
    let mut stderr = String::new();
    for (opener, exception) in [
        (
            "WARNING 11-02 23:30:01 [rendezvous.py:88] connect to 10.0.0.2:29500 failed, retrying",
            "ConnectionRefusedError: [Errno 111] Connection refused",
        ),
        (
            "WARNING 11-02 23:30:02 [config.py:41] optional tuning file not loaded",
            "FileNotFoundError: [Errno 2] No such file or directory: '/models/tuning.json'",
        ),
    ] {
        stderr.push_str(&format!(
            "{opener}\nTraceback (most recent call last):\n  File \"/app/net.py\", line 4, in load\n    open_or_connect()\n{exception}\n"
        ));
    }
    stderr.push_str(
        &"INFO 11-02 23:31:00 [scheduler.py:120] Running: 0 reqs, Waiting: 0 reqs\n".repeat(300),
    );
    stderr.push_str(
        "ERROR 11-02 23:31:05 [rendezvous.py:120] lost contact with rank 1 (10.0.0.2): network is unreachable\n",
    );
    let log = through_docker_logs_capture(b"", stderr.as_bytes());
    assert!(log.stderr.len() <= crate::package_command::STREAM_RETAINED_BYTES);
    let (retained, summary) = summary_of(log, 1);
    assert!(!retained.stderr.text.contains("Traceback"), "precondition");
    assert!(summary.contains("exit_cause=unclassified"), "{summary}");
    assert!(!summary.contains("exception="), "{summary}");
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
            .runtime_run_container(run)
            .map(|named| named.running)
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
        docker_output(true, &managed_line(&id, running, managed, label), 0)
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

const LABELLED_TARGET: &str = "ab69f1ba-1fa2-4283-bdf1-a6d2ed59549c";
const LABELLED_INSTALLATION: &str = "c0000000-0000-4000-8000-00000000000c";

/// The docker answer of the name-only look, with every managed label set.
fn managed_line(id: &str, running: &str, managed: &str, run: &str) -> String {
    format!(
        "{id}\t{running}\t{managed}\t{run}\t{LABELLED_TARGET}\t{LABELLED_INSTALLATION}\t3\t{}\n",
        "d".repeat(64)
    )
}

#[test]
fn name_only_run_check_returns_the_container_labels_it_read() {
    // Catches a stop built from anything but the container's own labels: the
    // reply must carry exactly what was read, and nothing when a label is
    // damaged.
    let run_id = "ab69f1ba-1fa2-4283-bdf1-a6d2ed59549c";
    let id = "a".repeat(64);
    let named = |stdout: String| {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        OperationExecutor::new(
            roots,
            &[0; 32],
            NameOnlyRunner {
                inspect: docker_output(true, &stdout, 0),
                listing: docker_output(true, "", 0),
            },
            None,
        )
        .unwrap()
        .runtime_run_container(run_id)
        .unwrap()
    };
    let labels = named(managed_line(&id, "true", "true", run_id))
        .labels
        .expect("labels");
    assert_eq!(labels.run_id.to_string(), run_id);
    assert_eq!(labels.target_id.to_string(), LABELLED_TARGET);
    assert_eq!(labels.installation_id.to_string(), LABELLED_INSTALLATION);
    assert_eq!(labels.run_generation, 3);
    assert_eq!(labels.plan_digest, "d".repeat(64));
    // A container from before the labels existed has them empty: still known
    // to be running, but with no identity to stop it by.
    let bare = named(format!("{id}\ttrue\ttrue\t{run_id}\t\t\t\t\n"));
    assert!(bare.running && bare.labels.is_none());
    // A non-canonical generation is no identity either.
    let damaged = named(format!(
        "{id}\ttrue\ttrue\t{run_id}\t{LABELLED_TARGET}\t{LABELLED_INSTALLATION}\t03\t{}\n",
        "d".repeat(64)
    ));
    assert!(damaged.running && damaged.labels.is_none());
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

/// The agent's grant-free re-observation of one job by its whole identity:
/// what Docker holds under the job's name, never a stand-in.
#[test]
fn a_job_reobservation_answers_running_its_recorded_exit_or_proven_absence() {
    use std::os::unix::fs::PermissionsExt;
    const RUN: &str = "a0000000-0000-4000-8000-00000000000a";
    const JOB: &str = "a1000000-0000-4000-8000-0000000000a1";
    let inspect = |host: &Host, generation: &str| {
        let (_temp, executor) = host_executor(host);
        let request = HostRuntimeRequest {
            inspect_target: Some(RunInspectTarget::JobIdentityTarget(
                vonk_agent_protocol::generated::JobIdentityTarget {
                    kind: vonk_agent_protocol::generated::JobIdentityTargetKind::JobIdentity,
                    runtime_id: JOB.parse().unwrap(),
                    run_id: RUN.parse().unwrap(),
                    installation_id: INSTALLATION.parse().unwrap(),
                    run_generation: generation.parse().unwrap(),
                    plan_digest: PLAN_DIGEST.to_owned(),
                },
            )),
            action: HostRuntimeAction::RunInspect,
            fence: uuid::Uuid::new_v4(),
            arguments: Vec::new(),
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
    };
    let with = |state, exit| {
        Host::with(&[Container {
            exit,
            ..job(RUN, JOB, state)
        }])
    };
    let running = inspect(&with("running", 0), "1").unwrap();
    assert!(running.running && running.exit_code.is_none() && running.logs.is_none());
    let exited = inspect(&with("exited", 3), "1").unwrap();
    assert!(!exited.running);
    assert_eq!(exited.exit_code, Some(3));
    // A failure carries its output and exit account.
    assert!(exited.logs.is_some());
    assert!(
        exited
            .log_error
            .is_some_and(|summary| summary.contains('3'))
    );
    let clean = inspect(&with("exited", 0), "1").unwrap();
    assert_eq!(clean.exit_code, Some(0));
    assert!(clean.logs.is_none() && clean.log_error.is_none());
    assert!(matches!(
        inspect(&Host::default(), "1"),
        Err(OperationError::RuntimeRunMissing)
    ));
    // Another generation under the name is unread, never absent or ours.
    assert!(matches!(
        inspect(&with("running", 0), "2"),
        Err(OperationError::CommandFailed { .. })
    ));
    // A generation spelled "01" is unrepresentable: it is a typed integer now,
    // and the vectors refuse a text generation at the contract.
}

#[test]
fn installation_ownership_uses_running_containers_and_mounts_not_metadata() {
    // A corrupt plan/lifecycle cannot pin neighbours. Real runtime evidence,
    // including a mount with damaged labels, must still protect its owner.
    struct Runner {
        label: CommandOutput,
        mount: CommandOutput,
    }
    impl CommandRunner for Runner {
        fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(program, Path::new("/usr/bin/docker"));
            assert_eq!(&args[..2], &["container", "ls"]);
            assert!(
                !args.iter().any(|arg| arg == "--all"),
                "stopped containers are not live ownership"
            );
            Ok(if args.last().unwrap().starts_with("label=") {
                self.label.clone()
            } else {
                assert!(args.last().unwrap().ends_with("/runtime-cache"));
                self.mount.clone()
            })
        }
    }
    let check = |label, mount| {
        let temp = tempfile::tempdir().unwrap();
        let executor = OperationExecutor::new(
            ManagedRoots::under(temp.path()),
            &[0; 32],
            Runner { label, mount },
            None,
        )
        .unwrap();
        let request = HostRuntimeRequest {
            inspect_target: Some(RunInspectTarget::InstallationInUseTarget(
                vonk_agent_protocol::generated::InstallationInUseTarget {
                    kind: vonk_agent_protocol::generated::InstallationInUseTargetKind::InstallationInUse,
                    installation_id: "ab69f1ba-1fa2-4283-bdf1-a6d2ed59549c".parse().unwrap(),
                },
            )),
            action: HostRuntimeAction::RunInspect,
            fence: uuid::Uuid::new_v4(),
            arguments: Vec::new(),
            installation_id: None,
            reconciliation_identity: None,
            job_plan: None,
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
        fs::write(&path, body).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
        executor
            .inspect_recipe_run_with_logs(&digest, false)
            .map(|seen| seen.running)
    };
    let absent = docker_output(true, "", 0);
    let live = docker_output(true, &format!("{}\n", "a".repeat(64)), 0);
    assert!(matches!(check(absent.clone(), absent.clone()), Ok(false)));
    assert!(matches!(check(live.clone(), absent.clone()), Ok(true)));
    assert!(matches!(check(absent.clone(), live), Ok(true)));
    assert!(check(docker_output(false, "", 1), absent.clone()).is_err());
    assert!(check(absent.clone(), docker_output(false, "", 1)).is_err());
    assert!(check(docker_output(true, "invalid", 0), absent).is_err());
}
