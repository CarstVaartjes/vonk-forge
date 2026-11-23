#![cfg(test)]
use super::*;
use std::collections::VecDeque;
use tempfile::tempdir;

#[cfg(target_os = "linux")]
#[test]
fn real_apt_dependency_diagnostic_is_available_for_retry_decision() {
    // Exercise real APT stderr without reading the runner's potentially
    // large package database or inheriting its APT configuration.
    let apt = tempdir().unwrap();
    for directory in [
        "state/lists/partial",
        "cache/archives/partial",
        "etc/sources.list.d",
        "etc/apt.conf.d",
        "etc/preferences.d",
    ] {
        fs::create_dir_all(apt.path().join(directory)).unwrap();
    }
    for file in ["state/status", "etc/sources.list", "etc/apt.conf"] {
        fs::write(apt.path().join(file), "").unwrap();
    }
    let config = apt.path().join("apt.conf");
    fs::write(
        &config,
        format!(
            "Dir \"{}\";\nDir::State \"state\";\nDir::State::status \"status\";\n\
                 Dir::Cache \"cache\";\nDir::Etc \"etc\";\n",
            apt.path().display()
        ),
    )
    .unwrap();
    let output = run_process(
        Command::new(
            "/usr/bin/apt-get",
            [
                "-s",
                "install",
                "vonk-forge-nonexistent-test-dependency-98765",
            ],
        )
        .with_env("APT_CONFIG", config.to_str().unwrap())
        .capture_stderr(),
        Duration::from_secs(30),
    )
    .unwrap();
    assert!(!output.success);
    assert!(String::from_utf8_lossy(&output.stderr).contains("Unable to locate package"));
    assert!(apt_needs_index_refresh(&output));
}

struct AptRunner {
    outcomes: VecDeque<CommandOutput>,
    commands: Vec<Command>,
}

impl CommandRunner for AptRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
        self.commands.push(command);
        Ok(self.outcomes.pop_front().unwrap())
    }

    fn authenticate_sudo(&mut self, _sudo: &Path) -> Result<(), SetupError> {
        Ok(())
    }
}

#[test]
fn apt_refreshes_indexes_only_for_dependency_resolution_failure() {
    let staged = StagedPackage {
        _directory: tempdir().unwrap(),
        path: PathBuf::from("/tmp/accepted.deb"),
    };
    let failed = CommandOutput {
        success: false,
        stdout: Vec::new(),
        stderr: b"dpkg returned an error code".to_vec(),
    };
    let mut runner = AptRunner {
        outcomes: [failed].into(),
        commands: Vec::new(),
    };
    assert!(install_package(&mut runner, &staged).is_err());
    assert_eq!(runner.commands.len(), 1);

    let dependencies = CommandOutput {
        success: false,
        stdout: b"The following packages have unmet dependencies".to_vec(),
        stderr: b"E: Unable to correct problems, you have held broken packages.".to_vec(),
    };
    let mut runner = AptRunner {
        outcomes: [
            dependencies,
            CommandOutput::success_empty(),
            CommandOutput::success_empty(),
        ]
        .into(),
        commands: Vec::new(),
    };
    install_package(&mut runner, &staged).unwrap();
    assert_eq!(
        runner
            .commands
            .iter()
            .map(|command| command.args[0].as_str())
            .collect::<Vec<_>>(),
        ["install", "update", "install"]
    );
    let failed_fetch = |status| CommandOutput {
        success: false,
        stdout: Vec::new(),
        stderr: format!("E: Failed to fetch https://packages.example/dep.deb  {status}")
            .into_bytes(),
    };
    assert!(apt_needs_index_refresh(&failed_fetch("404  Not Found")));
    assert!(!apt_needs_index_refresh(&failed_fetch("401  Unauthorized")));
}
