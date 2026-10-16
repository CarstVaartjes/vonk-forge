//! Commands.

use super::*;

impl CommandRunner for ProcessCommandRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        let timeout = if executable == Path::new("/usr/bin/dpkg") {
            Duration::from_secs(120)
        } else if executable == Path::new("/usr/bin/docker") {
            Duration::from_secs(600)
        } else {
            Duration::from_secs(30)
        };
        self.run_with_timeout(executable, arguments, timeout)
    }

    fn run_with_timeout(
        &self,
        executable: &Path,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<CommandOutput, String> {
        if !matches!(
            executable.to_str(),
            Some(
                "/usr/bin/dpkg-deb"
                    | "/usr/bin/dpkg"
                    | DOCKER_FIREWALL
                    | "/usr/bin/docker"
                    | "/usr/bin/setfacl"
                    | "/usr/bin/systemctl"
            )
        ) {
            return Err("executable is not compiled into the helper".to_owned());
        }
        let capture_output = captures_stdout(executable);
        // dpkg maintainer scripts and package configuration emit the details
        // needed to diagnose activation failures. Inherit both streams so the
        // service manager records them in its journal. These streams are not
        // consumed by the helper, so they must not be piped into its bounded
        // command-output reader.
        let inherit_output = executable == Path::new("/usr/bin/dpkg");
        let mut command = Command::new(executable);
        command
            .args(arguments)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("PATH", ROOT_COMMAND_PATH)
            .current_dir("/")
            .stdin(Stdio::null())
            // Standard error is the command's own account of a failure; a
            // captured command retains its tail as `CommandFailure` evidence.
            .stderr(if inherit_output {
                Stdio::inherit()
            } else {
                Stdio::piped()
            })
            .stdout(if inherit_output {
                Stdio::inherit()
            } else if capture_output {
                Stdio::piped()
            } else {
                Stdio::null()
            });
        if inherit_output {
            let result = crate::package_command::run(&mut command, timeout)?;
            return Ok(CommandOutput {
                success: result.status.success() && !result.timed_out,
                stdout: result.diagnostic(),
                stderr: Vec::new(),
                exit_code: result.status.code(),
            });
        }
        if executable == Path::new("/usr/bin/docker")
            && arguments.first().is_some_and(|value| value == "logs")
        {
            let result = crate::package_command::run_quiet(&mut command, timeout)?;
            return Ok(CommandOutput {
                success: result.status.success() && !result.timed_out,
                stdout: result.stdout,
                stderr: result.stderr,
                exit_code: result.status.code(),
            });
        }
        run_captured_command(&mut command, timeout).map_err(|failure| failure.to_string())
    }
}

/// Whether the helper reads `executable`'s standard output. It depends on the
/// executable alone: a caller that parses the output of one of these commands
/// (a detached job's `docker run` prints its container id) always receives it.
pub(super) fn captures_stdout(executable: &Path) -> bool {
    matches!(
        executable.to_str(),
        Some("/usr/bin/dpkg-deb" | "/usr/bin/docker" | "/usr/bin/systemctl" | DOCKER_FIREWALL)
    )
}

/// Bytes of a failed command's standard error kept as its evidence: the end of
/// the stream, where a command states why it failed.
const COMMAND_STDERR_TAIL_BYTES: usize = 512;

/// Why a compiled command gave the helper no usable answer: the evidence of
/// `OperationError::CommandFailed`. It is journaled with the rejection; the
/// wire keeps carrying only the command-failed code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommandFailure {
    /// The runner refused or ended the command before it gave an exit status
    /// (not started, deadline, output bound, unreadable output); its reason.
    Runner(String),
    /// The command exited unsuccessfully (`None`: ended by a signal), with the
    /// bounded tail of its standard error.
    Exit { code: Option<i32>, stderr: String },
    /// The command succeeded but its answer was not the one required.
    UnexpectedAnswer,
    /// The native fabric address was not observed within the bounded pass.
    FabricUnobserved,
    /// This job's Start is already active or was cancelled.
    StartFenced,
}

impl CommandFailure {
    pub(super) fn exit(output: &CommandOutput) -> Self {
        let stderr = &output.stderr;
        let tail = &stderr[stderr.len().saturating_sub(COMMAND_STDERR_TAIL_BYTES)..];
        Self::Exit {
            code: output.exit_code,
            stderr: String::from_utf8_lossy(tail).into_owned(),
        }
    }
}

impl std::fmt::Display for CommandFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Runner(reason) => formatter.write_str(reason),
            Self::Exit { code, stderr } => {
                match code {
                    Some(code) => write!(formatter, "exit_code={code}")?,
                    None => formatter.write_str("exit_code=signal")?,
                }
                write!(formatter, " stderr=\"{}\"", stderr.escape_debug())
            }
            Self::UnexpectedAnswer => formatter.write_str("answer unexpected"),
            Self::FabricUnobserved => formatter.write_str("native fabric unobserved"),
            Self::StartFenced => formatter.write_str("job start fenced"),
        }
    }
}

impl From<CommandFailure> for OperationError {
    fn from(cause: CommandFailure) -> Self {
        Self::CommandFailed { cause }
    }
}

/// Why `run_captured_command` gave no exit status. Its text is the runner's
/// reason across the `CommandRunner` boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RunnerFailure {
    Spawn,
    OutputUnreadable(std::io::ErrorKind),
    Deadline,
    OutputBound,
    Wait,
}

impl std::fmt::Display for RunnerFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Spawn => "compiled command could not start",
            Self::OutputUnreadable(kind) => {
                return write!(formatter, "compiled command output failed: {kind}");
            }
            Self::Deadline => "compiled command exceeded its deadline",
            Self::OutputBound => "compiled command output exceeded its bound",
            Self::Wait => "compiled command wait failed",
        })
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn run_docker(&self, arguments: &[String]) -> Result<CommandOutput, OperationError> {
        self.runner
            .run(Path::new("/usr/bin/docker"), arguments)
            .map_err(|reason| CommandFailure::Runner(reason).into())
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn run_docker_with_timeout(
        &self,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<CommandOutput, OperationError> {
        self.runner
            .run_with_timeout(Path::new("/usr/bin/docker"), arguments, timeout)
            .map_err(|reason| CommandFailure::Runner(reason).into())
    }
}

/// Read output and reap the process under the same command budget. A descendant
/// retaining stdout cannot keep the helper occupied after its parent exits.
/// Standard error is retained as a bounded tail and never counts toward the
/// output bound.
fn run_captured_command(
    command: &mut Command,
    timeout: Duration,
) -> Result<CommandOutput, RunnerFailure> {
    use std::os::unix::process::CommandExt;

    let deadline = Instant::now() + timeout;
    command.process_group(0);
    let mut child = command.spawn().map_err(|_| RunnerFailure::Spawn)?;
    let result = (|| {
        let mut output = child.stdout.take();
        let mut errors = child.stderr.take();
        for pipe in [
            output.as_ref().map(|pipe| pipe as &dyn std::os::fd::AsFd),
            errors.as_ref().map(|pipe| pipe as &dyn std::os::fd::AsFd),
        ]
        .into_iter()
        .flatten()
        {
            let unreadable = |error: rustix::io::Errno| {
                RunnerFailure::OutputUnreadable(std::io::Error::from(error).kind())
            };
            let flags = rustix::fs::fcntl_getfl(pipe).map_err(unreadable)?;
            rustix::fs::fcntl_setfl(pipe, flags | rustix::fs::OFlags::NONBLOCK)
                .map_err(unreadable)?;
        }
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let mut status = None;
        let mut output_closed = output.is_none();
        loop {
            if Instant::now() >= deadline {
                return Err(RunnerFailure::Deadline);
            }
            let mut buffer = [0_u8; 1024];
            if !output_closed {
                match output.as_mut().expect("piped output").read(&mut buffer) {
                    Ok(0) => output_closed = true,
                    Ok(count) => {
                        if stdout.len() + count > MAX_COMMAND_OUTPUT_BYTES as usize {
                            return Err(RunnerFailure::OutputBound);
                        }
                        stdout.extend_from_slice(&buffer[..count]);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                    Err(error) => return Err(RunnerFailure::OutputUnreadable(error.kind())),
                }
            }
            if let Some(pipe) = errors.as_mut() {
                // Evidence only: an ended or unreadable stream ends its tail,
                // never the command.
                match pipe.read(&mut buffer) {
                    Ok(count) if count > 0 => {
                        stderr.extend_from_slice(&buffer[..count]);
                        stderr.drain(..stderr.len().saturating_sub(COMMAND_STDERR_TAIL_BYTES));
                    }
                    Err(error)
                        if matches!(
                            error.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                        ) => {}
                    _ => errors = None,
                }
            }
            if status.is_none() {
                status = child.try_wait().map_err(|_| RunnerFailure::Wait)?;
            }
            if output_closed
                && errors.is_none()
                && let Some(status) = status
            {
                return Ok(CommandOutput {
                    success: status.success(),
                    stdout,
                    stderr,
                    exit_code: status.code(),
                });
            }
            thread::sleep(
                Duration::from_millis(1).min(deadline.saturating_duration_since(Instant::now())),
            );
        }
    })();
    if result.is_err() {
        if let Some(group) = rustix::process::Pid::from_raw(child.id() as i32) {
            let _ = rustix::process::kill_process_group(group, rustix::process::Signal::KILL);
        }
        let _ = child.kill();
        // Even uninterruptible kernel IO cannot turn cleanup into an infinite
        // wait. A new helper request is admitted after this cleanup allowance.
        let _ = child.wait_timeout(Duration::from_secs(1));
    }
    result
}

#[cfg(test)]
mod capture_tests {
    use super::*;

    #[test]
    fn inherited_stdout_expires_and_a_fresh_command_is_admitted() {
        let mut stalled = Command::new("/bin/sh");
        stalled
            .args(["-c", "sleep 10 & exit 0"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let began = Instant::now();
        assert_eq!(
            run_captured_command(&mut stalled, Duration::from_millis(50)),
            Err(RunnerFailure::Deadline)
        );
        assert!(began.elapsed() < Duration::from_secs(2));
        let mut fresh = Command::new("/bin/sh");
        fresh
            .args(["-c", "printf fresh"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let result = run_captured_command(&mut fresh, Duration::from_secs(1)).unwrap();
        assert!(result.success);
        assert_eq!(result.stdout, b"fresh");
    }

    fn shell(script: &str) -> Command {
        let mut command = Command::new("/bin/sh");
        command
            .args(["-c", script])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        command
    }

    #[test]
    fn a_command_that_cannot_start_is_typed() {
        let mut missing = Command::new("/nonexistent/vonk-command");
        assert_eq!(
            run_captured_command(&mut missing, Duration::from_secs(1)),
            Err(RunnerFailure::Spawn)
        );
    }

    #[test]
    fn output_over_its_bound_is_typed() {
        let mut flood = shell("head -c 8192 /dev/zero");
        assert_eq!(
            run_captured_command(&mut flood, Duration::from_secs(5)),
            Err(RunnerFailure::OutputBound)
        );
    }

    #[test]
    fn a_failed_exit_keeps_its_status_and_the_tail_of_its_stderr() {
        // Standard error never counts toward the output bound: the command's
        // own answer is kept, and only the end of its reason is retained.
        let mut failing = shell("head -c 8192 /dev/zero >&2; printf daemon-refused >&2; exit 3");
        let output = run_captured_command(&mut failing, Duration::from_secs(5)).unwrap();
        assert!(!output.success);
        assert_eq!(output.stderr.len(), COMMAND_STDERR_TAIL_BYTES);
        let CommandFailure::Exit { code, stderr } = CommandFailure::exit(&output) else {
            panic!("an exited command is an exit cause");
        };
        assert_eq!(code, Some(3));
        assert!(stderr.ends_with("daemon-refused"));
        assert_eq!(stderr.len(), COMMAND_STDERR_TAIL_BYTES);
    }

    #[test]
    fn a_runner_reason_survives_as_the_command_failure_cause() {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        fs::create_dir_all(&roots.data).unwrap();
        let executor = OperationExecutor::new(roots, &[0; 32], RefusingRunner, None).unwrap();
        let error = executor.run_docker(&[]).unwrap_err();
        assert!(matches!(
            error,
            OperationError::CommandFailed {
                cause: CommandFailure::Runner(ref reason),
            } if *reason == RunnerFailure::Deadline.to_string()
        ));
    }

    struct RefusingRunner;

    impl CommandRunner for RefusingRunner {
        fn run(&self, _: &Path, _: &[String]) -> Result<CommandOutput, String> {
            Err(RunnerFailure::Deadline.to_string())
        }
    }
}
