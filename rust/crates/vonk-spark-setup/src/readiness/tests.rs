#![cfg(test)]
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
