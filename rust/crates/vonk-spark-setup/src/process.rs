//! Process.

use super::*;

pub struct SystemCommandRunner;

impl CommandRunner for SystemCommandRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
        let timeout = command_timeout(&command.program);
        run_process(command, timeout)
    }

    fn authenticate_sudo(&mut self, sudo: &Path) -> Result<(), SetupError> {
        authenticate_sudo_foreground(sudo)
    }
}

pub(super) fn run_process(command: Command, timeout: Duration) -> Result<CommandOutput, String> {
    let deadline = Instant::now() + timeout;
    let program = command.program.clone();
    let mut process = ProcessCommand::new(&command.program);
    process
        .args(&command.args)
        .env_clear()
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .envs(command.env)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped());
    process.stderr(match command.stderr {
        CommandStderr::Inherit => Stdio::inherit(),
        CommandStderr::Suppress => Stdio::null(),
        CommandStderr::Capture => Stdio::piped(),
    });
    // Put every privileged command in its own process group.  A timed-out
    // sudo shell can otherwise leave apt/systemd descendants behind and
    // the next upgrade would race those stale processes.
    process.process_group(0);
    let mut child = process.spawn().map_err(|_| program.display().to_string())?;

    // Own nonblocking descriptors in this thread: inherited pipes cannot leave
    // detached readers/writers behind after the immutable command deadline.
    let settlement = TERMINATION_GRACE.min(timeout / 10);
    let result = capture_process(&mut child, &command.stdin, deadline - settlement);
    terminate_process_group_until(&mut child, deadline);
    result.map_err(|_| program.display().to_string())
}

/// Retry read-only process observations within one immutable budget. Effects
/// (APT, pairing and the root handoff) continue to use run_process directly.
pub(super) fn observe_process(
    command: Command,
    timeout: Duration,
) -> Result<CommandOutput, String> {
    let deadline = Instant::now() + timeout;
    let mut last = command.program.display().to_string();
    for attempt in 0..3 {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match run_process(command.clone(), remaining) {
            Ok(output) => return Ok(output),
            Err(error) => last = error,
        }
        if attempt < 2 {
            thread::sleep(
                Duration::from_millis(100).min(deadline.saturating_duration_since(Instant::now())),
            );
        }
    }
    Err(last)
}

const MAX_STDOUT_BYTES: usize = 2 * 1024 * 1024;
const MAX_STDERR_BYTES: usize = 64 * 1024;

fn nonblocking(pipe: &impl std::os::fd::AsFd) -> io::Result<()> {
    use rustix::fs::{OFlags, fcntl_getfl, fcntl_setfl};
    fcntl_setfl(pipe, fcntl_getfl(pipe)? | OFlags::NONBLOCK)?;
    Ok(())
}

fn drain_pipe(
    pipe: &mut Option<impl Read>,
    output: &mut Vec<u8>,
    maximum: usize,
) -> io::Result<()> {
    let Some(reader) = pipe else { return Ok(()) };
    let mut buffer = [0_u8; 65536];
    // A bounded number of chunks per tick keeps an endlessly writing peer from
    // holding this loop, while a large burst still drains well within the deadline.
    for _ in 0..16 {
        match reader.read(&mut buffer) {
            Ok(0) => {
                *pipe = None;
                break;
            }
            Ok(count) => {
                output.extend_from_slice(&buffer[..count]);
                if output.len() > maximum {
                    output.drain(..output.len() - maximum);
                }
            }
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                break;
            }
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

fn capture_process(
    child: &mut std::process::Child,
    input: &[u8],
    deadline: Instant,
) -> io::Result<CommandOutput> {
    let mut stdin = child.stdin.take();
    let mut stdout = child.stdout.take();
    let mut stderr = child.stderr.take();
    if let Some(pipe) = &stdin {
        nonblocking(pipe)?;
    }
    if let Some(pipe) = &stdout {
        nonblocking(pipe)?;
    }
    if let Some(pipe) = &stderr {
        nonblocking(pipe)?;
    }
    let mut written = 0;
    let mut captured_stdout = Vec::new();
    let mut captured_stderr = Vec::new();
    let mut status = None;
    loop {
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "command observation deadline",
            ));
        }
        if written == input.len() {
            stdin = None;
        } else if let Some(pipe) = &mut stdin {
            match pipe.write(&input[written..]) {
                Ok(0) => return Err(io::Error::from(io::ErrorKind::WriteZero)),
                Ok(count) => written += count,
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) => {}
                Err(error) => return Err(error),
            }
        }
        drain_pipe(&mut stdout, &mut captured_stdout, MAX_STDOUT_BYTES)?;
        drain_pipe(&mut stderr, &mut captured_stderr, MAX_STDERR_BYTES)?;
        if status.is_none() {
            status = child.try_wait()?;
        }
        if let Some(status) = status
            && stdin.is_none()
            && stdout.is_none()
            && stderr.is_none()
        {
            return Ok(CommandOutput {
                success: status.success(),
                stdout: captured_stdout,
                stderr: captured_stderr,
            });
        }
        thread::sleep(
            Duration::from_millis(1).min(deadline.saturating_duration_since(Instant::now())),
        );
    }
}

impl SystemCommandRunner {
    #[cfg(test)]
    pub(super) fn run_with_deadline_for_test(
        &mut self,
        command: Command,
        timeout: Duration,
    ) -> Result<CommandOutput, String> {
        run_process(command, timeout)
    }
}

pub(super) fn command_timeout(program: &Path) -> Duration {
    match program.to_str() {
        Some("/usr/bin/apt-get") => PACKAGE_COMMAND_TIMEOUT,
        Some("/usr/bin/sudo") => ROOT_HANDOFF_TIMEOUT,
        _ => DEFAULT_COMMAND_TIMEOUT,
    }
}

fn terminate_process_group_until(child: &mut std::process::Child, deadline: Instant) {
    let process_group = rustix::process::Pid::from_raw(child.id() as i32);
    if let Some(process_group) = process_group {
        let _ = rustix::process::kill_process_group(process_group, rustix::process::Signal::TERM);
    }
    // The leader may already have exited while descendants still hold pipes.
    // Always escalate the owned group; never use an unbounded final wait.
    if let Some(process_group) = process_group {
        let _ = rustix::process::kill_process_group(process_group, rustix::process::Signal::KILL);
    }
    let _ = child.kill();
    let _ = child
        .wait_timeout(TERMINATION_GRACE.min(deadline.saturating_duration_since(Instant::now())));
}

pub struct TtyPrompt {
    pub(super) terminal: Option<TtyTerminal>,
}

pub(super) struct TtyTerminal {
    pub(super) reader: BufReader<File>,
    pub(super) tty: File,
}

impl TtyPrompt {
    pub fn new() -> Self {
        Self { terminal: None }
    }

    pub(super) fn terminal(&mut self) -> Result<&mut TtyTerminal, String> {
        if self.terminal.is_none() {
            self.terminal = Some(Self::open_terminal()?);
        }
        self.terminal
            .as_mut()
            .ok_or_else(|| "tty unavailable".to_owned())
    }

    pub(super) fn open_terminal() -> Result<TtyTerminal, String> {
        let tty = OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/tty")
            .map_err(|_| "tty unavailable".to_owned())?;
        let reader = BufReader::new(tty.try_clone().map_err(|_| "tty unavailable".to_owned())?);
        Ok(TtyTerminal { reader, tty })
    }
}

impl Default for TtyPrompt {
    fn default() -> Self {
        Self::new()
    }
}

impl Prompt for TtyPrompt {
    fn value(&mut self, label: &str) -> Result<String, String> {
        let terminal = self.terminal()?;
        write!(terminal.tty, "{label}: ").map_err(|_| "tty output failed".to_owned())?;
        terminal
            .tty
            .flush()
            .map_err(|_| "tty output failed".to_owned())?;
        let mut value = String::new();
        if terminal
            .reader
            .read_line(&mut value)
            .map_err(|_| "tty input failed".to_owned())?
            == 0
        {
            return Err("tty input ended".to_owned());
        }
        Ok(value.trim().to_owned())
    }

    fn secret(&mut self, label: &str) -> Result<String, String> {
        rpassword::prompt_password(format!("{label}: "))
            .map_err(|_| "tty secret input failed".to_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn system_commands_settle_at_the_deadline_and_admit_next_command() {
        let mut runner = SystemCommandRunner;
        let started = Instant::now();
        let result = runner.run_with_deadline_for_test(
            Command::new("/bin/sh", ["-c", "sleep 30 & wait"]),
            Duration::from_millis(50),
        );
        assert!(result.is_err());
        assert!(
            runner
                .run(Command::new("/bin/sh", ["-c", "exit 0"]))
                .unwrap()
                .success
        );
        assert!(started.elapsed() < Duration::from_secs(5));
    }
    #[test]
    fn exited_parent_with_inherited_pipes_ends_and_next_command_runs() {
        let directory = tempfile::tempdir().unwrap();
        let exited = directory.path().join("parent-exited");
        let escaped_effect = directory.path().join("descendant-effect");
        let script = format!(
            "(sleep 1; touch '{}') & printf probe-marker > '{}'",
            escaped_effect.display(),
            exited.display()
        );
        let started = Instant::now();
        let result = run_process(
            Command::new("/bin/sh", ["-c", &script]),
            Duration::from_millis(200),
        );
        assert!(result.is_err());
        assert_eq!(fs::read(exited).unwrap(), b"probe-marker");
        assert!(started.elapsed() < Duration::from_secs(5));
        assert!(
            run_process(
                Command::new("/bin/sh", ["-c", "printf probe-bytes"]),
                Duration::from_secs(1)
            )
            .unwrap()
            .stdout
                == b"probe-bytes"
        );
        thread::sleep(Duration::from_millis(1100));
        assert!(!escaped_effect.exists());
    }

    #[test]
    fn output_flood_is_bounded_and_next_command_runs() {
        let started = Instant::now();
        let output = run_process(
            Command::new("/bin/sh", ["-c", "head -c 3145728 /dev/zero"]),
            Duration::from_secs(2),
        )
        .unwrap();
        assert!(output.success);
        assert_eq!(output.stdout.len(), MAX_STDOUT_BYTES);
        assert!(started.elapsed() < Duration::from_secs(5));
        assert!(
            run_process(
                Command::new("/bin/sh", ["-c", "exit 0"]),
                Duration::from_secs(1)
            )
            .unwrap()
            .success
        );
    }
}
