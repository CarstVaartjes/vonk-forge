//! Readiness.

use super::*;

pub(super) fn run_checked(
    runner: &mut dyn CommandRunner,
    command: Command,
) -> Result<CommandOutput, SetupError> {
    let name = command.program.display().to_string();
    let output = runner.run(command).map_err(SetupError::Command)?;
    if output.success {
        Ok(output)
    } else {
        Err(SetupError::Command(name))
    }
}

pub(super) fn verify_sustained_readiness(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    let started = Instant::now();
    let mut healthy = 0_u8;
    let mut pid = None;
    let attempts = READINESS_MAX_WAIT
        .as_secs()
        .div_ceil(READINESS_SAMPLE_INTERVAL.as_secs());
    for attempt in 0..attempts {
        if attempt > 0 && attempt % 5 == 0 {
            eprintln!(
                "vonk-spark-setup: phase=readiness elapsed={}s",
                started.elapsed().as_secs()
            );
        }
        let active = run_checked(
            runner,
            Command::new(
                "/usr/bin/systemctl",
                ["is-active", "--quiet", &paths.service],
            ),
        );
        let current_pid = active.and_then(|_| {
            run_checked(
                runner,
                Command::new(
                    "/usr/bin/systemctl",
                    ["show", "--property", "MainPID", "--value", &paths.service],
                ),
            )
        });
        match current_pid.and_then(|output| {
            let value = String::from_utf8(output.stdout)
                .map_err(|_| SetupError::Command("systemctl".to_owned()))?;
            let value = value.trim();
            if value
                .parse::<u32>()
                .ok()
                .filter(|value| *value != 0)
                .is_none()
            {
                return Err(SetupError::Command("systemctl".to_owned()));
            }
            Ok(value.to_owned())
        }) {
            Ok(current_pid) => {
                if pid.as_ref() != Some(&current_pid) {
                    healthy = 0;
                }
                pid = Some(current_pid.clone());
                let ready = run_checked(runner, readiness_probe(paths, &current_pid, true));
                if ready.is_ok() {
                    healthy += 1;
                } else {
                    healthy = 0;
                }
            }
            Err(_) => {
                healthy = 0;
                pid = None;
            }
        }
        if healthy >= READINESS_REQUIRED_SAMPLES {
            return Ok(());
        }
        if attempt + 1 < attempts {
            runner.sleep(READINESS_SAMPLE_INTERVAL);
        }
    }
    // The repeated probes intentionally suppress their transient diagnostics,
    // but once the bounded readiness window is exhausted the operator needs
    // the exact fail-closed reason (missing receipt, stale process identity,
    // or an invalid self-test) to repair the host.  These final diagnostics do
    // not include any credentials and inherit stderr from the setup command.
    if let Some(current_pid) = pid {
        let _ = runner.run(readiness_probe(paths, &current_pid, false));
    } else {
        emit_diagnostic_stdout(runner.run(Command::new(
            "/usr/bin/systemctl",
            ["status", "--no-pager", "--full", &paths.service],
        )));
        emit_diagnostic_stdout(runner.run(Command::new(
            "/usr/bin/journalctl",
            [
                "--unit",
                &paths.service,
                "--lines",
                "40",
                "--no-pager",
                "--full",
            ],
        )));
    }
    Err(SetupError::Command(
        "controller readiness was not sustained".to_owned(),
    ))
}

/// The command runner captures stdout so setup can inspect machine-readable
/// values (for example, systemd's MainPID).  Final failure diagnostics are
/// human-facing, though, and systemctl/journalctl write their useful detail to
/// stdout.  Forward only this bounded, non-secret diagnostic output; transient
/// readiness probes and command failures remain fail-closed and quiet.
pub(super) fn emit_diagnostic_stdout(result: Result<CommandOutput, String>) {
    let Ok(output) = result else {
        return;
    };
    let Some(text) = diagnostic_stdout(&output) else {
        return;
    };
    eprint!("{text}");
    if !text.ends_with('\n') {
        eprintln!();
    }
}

pub(super) fn diagnostic_stdout(output: &CommandOutput) -> Option<String> {
    (!output.stdout.is_empty()).then(|| String::from_utf8_lossy(&output.stdout).into_owned())
}

pub(super) fn readiness_probe(paths: &InstallPaths, pid: &str, suppress_stderr: bool) -> Command {
    let command = Command::new(
        &paths.agent,
        [
            "--config",
            paths.config.to_string_lossy().as_ref(),
            "verify-readiness",
            "--receipt",
            "/run/vonk-forge-agent/readiness.json",
            "--pid",
            pid,
            "--max-age-seconds",
            "90",
        ],
    );
    if suppress_stderr {
        command.suppress_stderr()
    } else {
        command
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn final_diagnostic_stdout_is_rendered_even_when_not_utf8() {
        let output = CommandOutput {
            success: false,
            stdout: b"ActiveState=failed\nreason=agent\xff\n".to_vec(),
            stderr: Vec::new(),
        };
        assert_eq!(
            diagnostic_stdout(&output).as_deref(),
            Some("ActiveState=failed\nreason=agent�\n")
        );
        assert!(diagnostic_stdout(&CommandOutput::success_empty()).is_none());
    }

    struct DelayedReadinessRunner {
        readiness_checks: u32,
    }

    impl CommandRunner for DelayedReadinessRunner {
        fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
            if command
                .args
                .iter()
                .any(|argument| argument == "verify-readiness")
            {
                self.readiness_checks += 1;
                return Ok(CommandOutput {
                    success: self.readiness_checks > 30,
                    stdout: Vec::new(),
                    stderr: Vec::new(),
                });
            }
            if command.program == Path::new("/usr/bin/systemctl")
                && command.args.first().map(String::as_str) == Some("show")
            {
                return Ok(CommandOutput::success(b"4242\n".to_vec()));
            }
            Ok(CommandOutput::success_empty())
        }

        fn authenticate_sudo(&mut self, _sudo: &Path) -> Result<(), SetupError> {
            Ok(())
        }

        fn sleep(&mut self, _duration: Duration) {}
    }

    #[test]
    fn readiness_allows_bounded_slow_start_but_still_requires_three_samples() {
        let paths = InstallPaths {
            config: PathBuf::from("/etc/vonk-forge-agent/agent.toml"),
            ca: PathBuf::from("/etc/vonk-forge-agent/controller-ca.pem"),
            firewall_config: PathBuf::from("/etc/vonk-forge-agent/docker-firewall.conf"),
            helper_authority: PathBuf::from("/etc/vonk-forge-agent/host-helper-authority.pub"),
            hosts: PathBuf::from("/etc/hosts"),
            agent: PathBuf::from("/usr/lib/vonk-forge/vonk-agent"),
            staging_root: PathBuf::from("/var/tmp/vonk-forge-agent"),
            sudo: PathBuf::from("/usr/bin/sudo"),
            service: SERVICE.to_owned(),
            required_owner: None,
        };
        let mut runner = DelayedReadinessRunner {
            readiness_checks: 0,
        };

        verify_sustained_readiness(&paths, &mut runner).unwrap();

        assert_eq!(runner.readiness_checks, 33);
    }

    #[derive(Default)]
    struct InactiveReadinessRunner {
        commands: Vec<Command>,
    }

    impl CommandRunner for InactiveReadinessRunner {
        fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
            let is_active = command.program == Path::new("/usr/bin/systemctl")
                && command.args.first().map(String::as_str) == Some("is-active");
            self.commands.push(command);
            if is_active {
                Ok(CommandOutput {
                    success: false,
                    stdout: Vec::new(),
                    stderr: Vec::new(),
                })
            } else {
                Ok(CommandOutput::success_empty())
            }
        }

        fn authenticate_sudo(&mut self, _sudo: &Path) -> Result<(), SetupError> {
            Ok(())
        }

        fn sleep(&mut self, _duration: Duration) {}
    }

    #[test]
    fn readiness_timeout_without_a_pid_emits_bounded_service_diagnostics() {
        let mut runner = InactiveReadinessRunner::default();
        let paths = InstallPaths {
            config: PathBuf::from("/etc/vonk-forge-agent/agent.toml"),
            ca: PathBuf::from("/etc/vonk-forge-agent/controller-ca.pem"),
            firewall_config: PathBuf::from("/etc/vonk-forge-agent/docker-firewall.conf"),
            helper_authority: PathBuf::from("/etc/vonk-forge-agent/host-helper-authority.pub"),
            hosts: PathBuf::from("/etc/hosts"),
            agent: PathBuf::from("/usr/lib/vonk-forge/vonk-agent"),
            staging_root: PathBuf::from("/var/tmp"),
            sudo: PathBuf::from("/usr/bin/sudo"),
            service: SERVICE.to_owned(),
            required_owner: Some(0),
        };

        assert!(verify_sustained_readiness(&paths, &mut runner).is_err());
        assert_eq!(
            runner
                .commands
                .iter()
                .filter(|command| command.program == Path::new("/usr/bin/systemctl")
                    && command.args.first().map(String::as_str) == Some("is-active"))
                .count(),
            90
        );
        assert!(runner.commands.iter().any(|command| {
            command.program == Path::new("/usr/bin/systemctl")
                && command.args == ["status", "--no-pager", "--full", SERVICE]
                && command.stderr == CommandStderr::Inherit
        }));
        assert!(runner.commands.iter().any(|command| {
            command.program == Path::new("/usr/bin/journalctl")
                && command.args == ["--unit", SERVICE, "--lines", "40", "--no-pager", "--full"]
                && command.stderr == CommandStderr::Inherit
        }));
        let mut recovered = DelayedReadinessRunner {
            readiness_checks: 30,
        };
        verify_sustained_readiness(&paths, &mut recovered).unwrap();
        assert_eq!(recovered.readiness_checks, 33);
    }
}
