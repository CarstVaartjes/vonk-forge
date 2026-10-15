#![cfg(test)]
//! What a returning helper does with job containers it no longer owns.
use super::*;
use crate::operations::test_support::*;

const INSTALLATION: &str = "50000000-0000-4000-8000-000000000005";
const RUN_A: &str = "a0000000-0000-4000-8000-00000000000a";
const RUN_B: &str = "b0000000-0000-4000-8000-00000000000b";
const JOB_A: &str = "a1000000-0000-4000-8000-0000000000a1";
const JOB_B: &str = "b1000000-0000-4000-8000-0000000000b1";
const SERVING: &str = "c0000000-0000-4000-8000-00000000000c";

#[derive(Clone)]
struct Container {
    run: &'static str,
    target: &'static str,
    state: &'static str,
}

/// A Docker host: the containers it holds, the commands it was given, and
/// whether `docker stop` works.
#[derive(Clone, Default)]
struct Host {
    containers: Arc<Mutex<Vec<Container>>>,
    calls: Arc<Mutex<Vec<Vec<String>>>>,
    stop_fails: Arc<Mutex<bool>>,
    listing_fails: bool,
}

impl Host {
    fn with(containers: &[Container]) -> Self {
        let host = Self::default();
        *host.containers.lock().unwrap() = containers.to_vec();
        host
    }

    fn present(&self, target: &str) -> bool {
        self.containers
            .lock()
            .unwrap()
            .iter()
            .any(|container| container.target == target)
    }

    fn called(&self, verb: &str, target: &str) -> Option<usize> {
        let name = format!("vonk-{target}");
        self.calls.lock().unwrap().iter().position(|call| {
            call.first().map(String::as_str) == Some(verb) && call.last() == Some(&name)
        })
    }
}

impl CommandRunner for Host {
    fn run(&self, executable: &Path, args: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new("/usr/bin/docker"));
        self.calls.lock().unwrap().push(args.to_vec());
        let mut containers = self.containers.lock().unwrap();
        let find = |name: &str| {
            containers
                .iter()
                .position(|container| format!("vonk-{}", container.target) == name)
        };
        let name = args.last().cloned().unwrap_or_default();
        let output = match args
            .iter()
            .map(String::as_str)
            .collect::<Vec<_>>()
            .as_slice()
        {
            ["container", "ls", ..] if self.listing_fails => docker_output(false, "", 1),
            ["container", "ls", ..] => {
                let run_filter = args
                    .iter()
                    .find_map(|arg| arg.strip_prefix("label=ai.vonkforge.run-id="));
                let lines: String = containers
                    .iter()
                    .filter(|container| run_filter.is_none_or(|run| run == container.run))
                    .map(|container| {
                        format!(
                            "vonk-{}\t{}\t{}\t{}\t{INSTALLATION}\t1\t{}\n",
                            container.target,
                            container.state,
                            container.run,
                            container.target,
                            "c".repeat(64)
                        )
                    })
                    .collect();
                docker_output(true, &lines, 0)
            }
            ["logs", ..] => docker_output(true, "job output so far\n", 0),
            ["container", "inspect", "--format", format, _]
                if *format == crate::runtime_logs::EXIT_FORMAT =>
            {
                docker_output(true, "137\tfalse\t\n", 0)
            }
            ["inspect", ..] => docker_output(true, &"f".repeat(64), 0),
            ["container", "inspect", ..] => match find(&name) {
                Some(index) => {
                    let container = &containers[index];
                    docker_output(
                        true,
                        &format!(
                            "true\t{}\t{}\t{INSTALLATION}\t1\t{}\n",
                            container.run,
                            container.target,
                            "c".repeat(64)
                        ),
                        0,
                    )
                }
                None => docker_output(false, "", 1),
            },
            ["stop", ..] => docker_output(!*self.stop_fails.lock().unwrap(), "", 0),
            ["rm", _] => {
                if let Some(index) = find(&name) {
                    containers.remove(index);
                }
                docker_output(true, "", 0)
            }
            _ => return Err(format!("unexpected command {args:?}")),
        };
        Ok(output)
    }
}

fn executor(host: &Host) -> (TempDir, OperationExecutor<Host>) {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(&temp.path().join("helper"))
        .with_agent_data(&temp.path().join("agent"));
    fs::create_dir_all(&roots.data).unwrap();
    let executor = OperationExecutor::new(roots, &[0; 32], host.clone(), None).unwrap();
    (temp, executor)
}

fn job(run: &'static str, target: &'static str, state: &'static str) -> Container {
    Container { run, target, state }
}

fn identity(target: &str) -> RuntimeEffectIdentity {
    RuntimeEffectIdentity {
        runtime_id: target.parse().unwrap(),
        installation_id: INSTALLATION.parse().unwrap(),
        run_generation: 1,
    }
}

#[test]
fn a_returning_helper_removes_every_job_it_does_not_own_and_never_a_serving_run() {
    let host = Host::with(&[
        job(RUN_A, JOB_A, "running"),
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
        !host.present(JOB_A),
        "a running job without an owner was left running"
    );
    assert!(
        !host.present(JOB_B),
        "an exited job container was left behind"
    );
    assert!(host.present(SERVING));
    assert_eq!(host.called("stop", SERVING), None);
    assert_eq!(host.called("rm", SERVING), None);
    // Its output is read before the container that holds it is removed.
    assert!(host.called("logs", JOB_A).unwrap() < host.called("rm", JOB_A).unwrap());
    // The job's own record is not the helper's to remove: it is still reported.
    assert!(completion.join("job-exit.json").is_file());
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
    assert!(
        executor
            .clear_stray_jobs_for_run(next, RUN_A.parse().unwrap())
            .is_err()
    );
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
}
