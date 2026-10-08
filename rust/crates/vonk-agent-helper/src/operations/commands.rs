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
            )
        ) {
            return Err("executable is not compiled into the helper".to_owned());
        }
        let capture_output = matches!(
            executable.to_str(),
            Some("/usr/bin/dpkg-deb" | "/usr/bin/docker" | DOCKER_FIREWALL)
        ) && !(executable == Path::new("/usr/bin/docker")
            && arguments
                .iter()
                .any(|value| value.starts_with("VONK_JOB_TIMEOUT_SECONDS=")));
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
            .stderr(if inherit_output {
                Stdio::inherit()
            } else {
                Stdio::null()
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
        run_captured_command(&mut command, timeout)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn run_docker(&self, arguments: &[String]) -> Result<CommandOutput, OperationError> {
        self.runner
            .run(Path::new("/usr/bin/docker"), arguments)
            .map_err(|_| OperationError::CommandFailed)
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
            .map_err(|_| OperationError::CommandFailed)
    }
}

/// Read output and reap the process under the same command budget. A descendant
/// retaining stdout cannot keep the helper occupied after its parent exits.
fn run_captured_command(command: &mut Command, timeout: Duration) -> Result<CommandOutput, String> {
    use std::os::unix::process::CommandExt;

    let deadline = Instant::now() + timeout;
    command.process_group(0);
    let mut child = command
        .spawn()
        .map_err(|_| "compiled command could not start".to_owned())?;
    let result = (|| {
        let mut output = child.stdout.take();
        if let Some(output) = output.as_ref() {
            let flags = rustix::fs::fcntl_getfl(output)
                .map_err(|_| "compiled command output failed".to_owned())?;
            rustix::fs::fcntl_setfl(output, flags | rustix::fs::OFlags::NONBLOCK)
                .map_err(|_| "compiled command output failed".to_owned())?;
        }
        let mut stdout = Vec::new();
        let mut status = None;
        let mut output_closed = output.is_none();
        loop {
            if Instant::now() >= deadline {
                return Err("compiled command exceeded its deadline".to_owned());
            }
            if !output_closed {
                let mut buffer = [0_u8; 1024];
                match output.as_mut().expect("piped output").read(&mut buffer) {
                    Ok(0) => output_closed = true,
                    Ok(count) => {
                        if stdout.len() + count > MAX_COMMAND_OUTPUT_BYTES as usize {
                            return Err("compiled command output exceeded its bound".to_owned());
                        }
                        stdout.extend_from_slice(&buffer[..count]);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                    Err(_) => return Err("compiled command output failed".to_owned()),
                }
            }
            if status.is_none() {
                status = child
                    .try_wait()
                    .map_err(|_| "compiled command wait failed".to_owned())?;
            }
            if output_closed && let Some(status) = status {
                return Ok(CommandOutput {
                    success: status.success(),
                    stdout,
                    stderr: Vec::new(),
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
        assert!(run_captured_command(&mut stalled, Duration::from_millis(50)).is_err());
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
}
