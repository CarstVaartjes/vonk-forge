#![cfg(test)]
//! A Docker host for the helper's job-container paths: the containers it
//! holds and the commands it was given, answered under the production
//! runner's capture rules (what it captures, its output bound).

use super::*;
use crate::operations::commands::captures_stdout;
use crate::operations::reobserve::REOBSERVE_FORMAT;

pub(in crate::operations) const INSTALLATION: &str = "50000000-0000-4000-8000-000000000005";
pub(in crate::operations) const STOP_REFUSAL: &str =
    "Error response from daemon: cannot stop container\n";
/// When every exited container of this host exited, as Docker records it.
pub(in crate::operations) const EXITED_AT: &str = "2026-10-01T00:00:00.123456789Z";
/// Every container of this host carries this plan digest label.
pub(in crate::operations) const PLAN_DIGEST: &str =
    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";

#[derive(Clone)]
pub(in crate::operations) struct Container {
    pub(in crate::operations) run: String,
    pub(in crate::operations) target: String,
    /// Docker's `.State.Status`.
    pub(in crate::operations) state: &'static str,
    /// The process exit Docker reports once it has exited.
    pub(in crate::operations) exit: i32,
    /// Its `ai.vonkforge.run-generation` label.
    pub(in crate::operations) generation: u64,
}

impl Container {
    /// Docker's full container id, distinct per target.
    pub(in crate::operations) fn id(&self) -> String {
        format!("{:0>64}", self.target.replace('-', ""))
    }
}

pub(in crate::operations) fn job(run: &str, target: &str, state: &'static str) -> Container {
    Container {
        run: run.to_owned(),
        target: target.to_owned(),
        state,
        exit: 137,
        generation: 1,
    }
}

/// A Docker host: the containers it holds, the commands it was given, and
/// whether `docker stop`, the listing and `docker wait` answer.
#[derive(Clone, Default)]
pub(in crate::operations) struct Host {
    pub(in crate::operations) containers: Arc<Mutex<Vec<Container>>>,
    pub(in crate::operations) calls: Arc<Mutex<Vec<Vec<String>>>>,
    pub(in crate::operations) stop_fails: Arc<Mutex<bool>>,
    pub(in crate::operations) listing_fails: bool,
    /// How many `docker wait` answers are unreadable before Docker answers.
    pub(in crate::operations) unreadable_waits: Arc<Mutex<u32>>,
}

impl Host {
    pub(in crate::operations) fn with(containers: &[Container]) -> Self {
        let host = Self::default();
        *host.containers.lock().unwrap() = containers.to_vec();
        host
    }

    pub(in crate::operations) fn present(&self, target: &str) -> bool {
        self.containers
            .lock()
            .unwrap()
            .iter()
            .any(|container| container.target == target)
    }

    pub(in crate::operations) fn called(&self, verb: &str, target: &str) -> Option<usize> {
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
            containers.iter().position(|container| {
                format!("vonk-{}", container.target) == name || container.id() == name
            })
        };
        let name = args.last().cloned().unwrap_or_default();
        let labels = |container: &Container| {
            format!(
                "{}\t{}\t{INSTALLATION}\t{}\t{PLAN_DIGEST}",
                container.run, container.target, container.generation
            )
        };
        let mut output = match args
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
                let name_filter = args.iter().find_map(|arg| {
                    arg.strip_prefix("name=^/")
                        .and_then(|name| name.strip_suffix('$'))
                });
                let last = args
                    .iter()
                    .position(|arg| arg == "--last")
                    .map(|index| args[index + 1].parse::<usize>().unwrap());
                let before = args.iter().find_map(|arg| arg.strip_prefix("before="));
                let quiet = args.iter().any(|arg| arg == "--quiet");
                let identity_read = args.iter().any(|arg| arg == REOBSERVE_FORMAT);
                let id_filter = args.iter().find_map(|arg| arg.strip_prefix("id="));
                // Newest first, as Docker lists them; `--last` applies after
                // the filters.
                let lines: String = containers
                    .iter()
                    .rev()
                    .skip_while(|container| before.is_some_and(|id| id != container.id()))
                    .skip(usize::from(before.is_some()))
                    .filter(|container| run_filter.is_none_or(|run| run == container.run))
                    .filter(|container| {
                        name_filter.is_none_or(|name| name == format!("vonk-{}", container.target))
                    })
                    .filter(|container| id_filter.is_none_or(|id| id == container.id()))
                    .take(last.unwrap_or(usize::MAX))
                    .map(|container| {
                        if quiet {
                            format!("{}\n", container.id())
                        } else if identity_read {
                            let live =
                                matches!(container.state, "running" | "paused" | "restarting");
                            let ended = matches!(container.state, "exited" | "dead");
                            format!(
                                "{live}\t{ended}\t{}\ttrue\t{}\n",
                                container.id(),
                                labels(container)
                            )
                        } else {
                            format!(
                                "vonk-{}\t{}\t{}\n",
                                container.target,
                                container.state,
                                labels(container)
                            )
                        }
                    })
                    .collect();
                docker_output(true, &lines, 0)
            }
            ["logs", ..] => docker_output(true, "job output so far\n", 0),
            ["wait", _] if *self.unreadable_waits.lock().unwrap() > 0 => {
                *self.unreadable_waits.lock().unwrap() -= 1;
                docker_output(true, "unreadable peer answer", 0)
            }
            // Docker blocks until the process exits and prints its status.
            ["wait", _] => match find(&name) {
                Some(index) => {
                    let container = &mut containers[index];
                    container.state = "exited";
                    docker_output(true, &format!("{}\n", container.exit), 0)
                }
                None => docker_output(false, "", 1),
            },
            ["container", "inspect", "--format", format, _]
                if *format == crate::runtime_logs::EXIT_FORMAT =>
            {
                match find(&name) {
                    Some(index) => {
                        docker_output(true, &format!("{}\tfalse\t\n", containers[index].exit), 0)
                    }
                    None => docker_output(false, "", 1),
                }
            }
            ["container", "inspect", "--format", format, _]
                if *format == crate::operations::exited_serving::SERVING_FORMAT =>
            {
                match find(&name) {
                    Some(index) => {
                        let container = &containers[index];
                        docker_output(
                            true,
                            &format!(
                                "{}\t{EXITED_AT}\t/vonk-{}\ttrue\t{}\t{}\t{INSTALLATION}\n",
                                container.state, container.target, container.run, container.target
                            ),
                            0,
                        )
                    }
                    None => docker_output(false, "", 1),
                }
            }
            ["inspect", ..] => docker_output(true, &"f".repeat(64), 0),
            ["container", "inspect", ..] => match find(&name) {
                Some(index) => {
                    docker_output(true, &format!("true\t{}\n", labels(&containers[index])), 0)
                }
                None => docker_output(false, "", 1),
            },
            ["stop", ..] if *self.stop_fails.lock().unwrap() => CommandOutput {
                stderr: STOP_REFUSAL.as_bytes().to_vec(),
                ..docker_output(false, "", 1)
            },
            ["stop", ..] => {
                if let Some(index) = find(&name) {
                    containers[index].state = "exited";
                }
                docker_output(true, "", 0)
            }
            // Docker never removes a running container without `--force`.
            ["rm", _] if find(&name).is_some_and(|index| containers[index].state == "running") => {
                docker_output(false, "", 1)
            }
            ["rm", _] => {
                if let Some(index) = find(&name) {
                    containers.remove(index);
                }
                docker_output(true, "", 0)
            }
            _ => return Err(format!("unexpected command {args:?}")),
        };
        // The production runner discards what it does not capture and refuses
        // captured output over its bound; the caller never sees a partial
        // listing.
        if !captures_stdout(executable) {
            output.stdout.clear();
        }
        if output.stdout.len() > MAX_COMMAND_OUTPUT_BYTES as usize {
            return Err("compiled command output exceeded its bound".to_owned());
        }
        Ok(output)
    }
}

pub(in crate::operations) fn host_executor(host: &Host) -> (TempDir, OperationExecutor<Host>) {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(&temp.path().join("helper"))
        .with_agent_data(&temp.path().join("agent"));
    fs::create_dir_all(&roots.data).unwrap();
    let executor = OperationExecutor::new(roots, &[0; 32], host.clone(), None).unwrap();
    (temp, executor)
}
