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
        CommandStderr::CaptureAndForward => Stdio::piped(),
    });
    // Put every privileged command in its own process group.  A timed-out
    // sudo shell can otherwise leave apt/systemd descendants behind and
    // the next upgrade would race those stale processes.
    process.process_group(0);
    let mut child = process.spawn().map_err(|_| program.display().to_string())?;

    let writer = child.stdin.take().map(|mut stdin| {
        let input = command.stdin;
        thread::spawn(move || stdin.write_all(&input))
    });
    let mut stdout = child
        .stdout
        .take()
        .ok_or_else(|| program.display().to_string())?;
    let reader = thread::spawn(move || {
        let mut output = Vec::new();
        stdout.read_to_end(&mut output).map(|_| output)
    });
    let stderr_reader = child.stderr.take().map(|mut stderr| {
        thread::spawn(move || {
            let mut captured = Vec::new();
            let mut buffer = [0_u8; 4096];
            loop {
                let count = stderr.read(&mut buffer)?;
                if count == 0 {
                    break;
                }
                // Preserve the live diagnostic while keeping only a bounded
                // tail for the install retry decision.
                let _ = io::stderr().write_all(&buffer[..count]);
                captured.extend_from_slice(&buffer[..count]);
                if captured.len() > 64 * 1024 {
                    captured.drain(..captured.len() - 64 * 1024);
                }
            }
            Ok::<_, io::Error>(captured)
        })
    });
    let status = match child.wait_timeout(timeout) {
        Ok(Some(status)) => status,
        Ok(None) | Err(_) => {
            terminate_process_group(&mut child);
            // Do not join pipe threads on the deadline path.  A child that
            // escaped its process group can keep a pipe open indefinitely;
            // dropping these handles lets the setup process return its
            // bounded failure while the OS closes the descriptors on exit.
            drop(writer);
            drop(reader);
            drop(stderr_reader);
            return Err(format!(
                "{} exceeded its command deadline",
                program.display()
            ));
        }
    };
    if let Some(writer) = writer {
        writer
            .join()
            .map_err(|_| program.display().to_string())?
            .map_err(|_| program.display().to_string())?;
    }
    let stdout = reader
        .join()
        .map_err(|_| program.display().to_string())?
        .map_err(|_| program.display().to_string())?;
    let stderr = stderr_reader
        .map(|reader| {
            reader
                .join()
                .map_err(|_| program.display().to_string())?
                .map_err(|_| program.display().to_string())
        })
        .transpose()?
        .unwrap_or_default();
    Ok(CommandOutput {
        success: status.success(),
        stdout,
        stderr,
    })
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

pub(super) fn terminate_process_group(child: &mut std::process::Child) {
    let process_group = rustix::process::Pid::from_raw(child.id() as i32);
    if let Some(process_group) = process_group {
        let _ = rustix::process::kill_process_group(process_group, rustix::process::Signal::TERM);
    }
    if child
        .wait_timeout(TERMINATION_GRACE)
        .ok()
        .flatten()
        .is_none()
    {
        if let Some(process_group) = process_group {
            let _ =
                rustix::process::kill_process_group(process_group, rustix::process::Signal::KILL);
        }
        let _ = child.kill();
        let _ = child.wait();
    }
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
    fn system_commands_fail_closed_and_reap_descendants_at_the_deadline() {
        let mut runner = SystemCommandRunner;
        let started = Instant::now();
        let error = runner
            .run_with_deadline_for_test(
                Command::new("/bin/sh", ["-c", "sleep 30 & wait"]),
                Duration::from_millis(50),
            )
            .expect_err("stalled command must be bounded");
        assert!(error.contains("/bin/sh exceeded its command deadline"));
        assert!(started.elapsed() < Duration::from_secs(5));
    }
}
