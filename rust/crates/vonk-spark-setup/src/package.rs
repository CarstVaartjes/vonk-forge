//! Package.

use super::*;

pub(super) fn install_package(
    runner: &mut dyn CommandRunner,
    staged: &StagedPackage,
) -> Result<(), SetupError> {
    eprintln!("vonk-spark-setup: phase=package-install elapsed=0s");
    // APT's index is only needed when dependency resolution cannot complete
    // against the indexes already present. Retry once after refreshing it.
    let first = runner
        .run(apt_install_command(staged))
        .map_err(SetupError::Command)?;
    if first.success {
        return Ok(());
    }
    if !apt_needs_index_refresh(&first) {
        return Err(SetupError::Command("/usr/bin/apt-get install".to_owned()));
    }
    run_checked(
        runner,
        Command::new("/usr/bin/apt-get", ["update"]).with_env("DEBIAN_FRONTEND", "noninteractive"),
    )?;
    apt_install(runner, staged)
}

pub(super) fn apt_needs_index_refresh(output: &CommandOutput) -> bool {
    let error = String::from_utf8_lossy(&output.stderr);
    let detail = String::from_utf8_lossy(&output.stdout);
    error.contains("Unable to locate package")
        || error.contains("has no installation candidate")
        || (error.contains("Unable to correct problems")
            && detail.contains("packages have unmet dependencies"))
        || (error.contains("Failed to fetch")
            && error.contains("404")
            && error.contains("Not Found"))
}

pub(super) fn apt_install(
    runner: &mut dyn CommandRunner,
    staged: &StagedPackage,
) -> Result<(), SetupError> {
    run_checked(runner, apt_install_command(staged)).map(|_| ())
}

pub(super) fn apt_install_command(staged: &StagedPackage) -> Command {
    Command::new(
        "/usr/bin/apt-get",
        [
            "install".to_owned(),
            "--yes".to_owned(),
            "--no-install-recommends".to_owned(),
            "-o".to_owned(),
            "Dpkg::Options::=--force-confold".to_owned(),
            staged.path().display().to_string(),
        ],
    )
    .with_env("DEBIAN_FRONTEND", "noninteractive")
    .capture_and_forward_stderr()
}

pub(super) fn ensure_package_installed(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    staged: &StagedPackage,
    version: &str,
    architecture: &str,
) -> Result<(), SetupError> {
    if installed_package_matches(paths, runner, staged, version, architecture)? {
        eprintln!(
            "vonk-spark-setup: accepted package and agent binary already installed; resuming"
        );
        return Ok(());
    }
    install_package(runner, staged)
}

pub(super) fn installed_package_matches(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    staged: &StagedPackage,
    version: &str,
    architecture: &str,
) -> Result<bool, SetupError> {
    let query = runner
        .run(Command::new(
            "/usr/bin/dpkg-query",
            [
                "-W",
                "-f=${db:Status-Abbrev}|${Version}|${Architecture}",
                "vonk-forge-agent",
            ],
        ))
        .map_err(SetupError::Command)?;
    let expected = format!("ii |{version}|{architecture}");
    if !query.success || String::from_utf8_lossy(&query.stdout).trim_end() != expected {
        return Ok(false);
    }
    if !safe_existing_file(&paths.agent, paths.required_owner)? {
        return Ok(false);
    }
    let extracted = secure_tempdir("vonk-spark-package-check.")?;
    let status = ProcessCommand::new("/usr/bin/dpkg-deb")
        .arg("--extract")
        .arg(staged.path())
        .arg(extracted.path())
        .env_clear()
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|_| SetupError::PackageFormat)?;
    if !status.success() {
        return Err(SetupError::PackageFormat);
    }
    let relative_agent = Path::new(AGENT_PATH)
        .strip_prefix("/")
        .map_err(|_| SetupError::PrivilegedInput)?;
    let packaged_agent = extracted.path().join(relative_agent);
    if !matches!(fs::symlink_metadata(&packaged_agent), Ok(metadata) if metadata.file_type().is_file())
    {
        return Ok(false);
    }
    let expected_digest = file_digest(&packaged_agent)?;
    match verify_regular_file_digest(&paths.agent, &expected_digest, MAX_PACKAGE_BYTES) {
        Ok(()) => Ok(true),
        Err(SetupError::PackageDigest | SetupError::UnsafePackage) => Ok(false),
        Err(error) => Err(error),
    }
}

pub(super) fn file_digest(path: &Path) -> Result<String, SetupError> {
    let mut file = File::open(path).map_err(|_| SetupError::PackageFormat)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    let expected_bytes = file
        .metadata()
        .map_err(|_| SetupError::PackageFormat)?
        .len();
    let mut remaining = expected_bytes;
    while remaining > 0 {
        let count = file
            .read(&mut buffer)
            .map_err(|_| SetupError::PackageFormat)?;
        if count == 0 {
            break;
        }
        remaining = remaining
            .checked_sub(count as u64)
            .ok_or(SetupError::PackageFormat)?;
        digest.update(&buffer[..count]);
    }
    if remaining != 0
        || file
            .metadata()
            .map_err(|_| SetupError::PackageFormat)?
            .len()
            != expected_bytes
    {
        return Err(SetupError::PackageFormat);
    }
    Ok(hex::encode(digest.finalize()))
}

pub(super) fn upgrade_existing(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    staged: &StagedPackage,
    version: &str,
    architecture: &str,
) -> Result<(), SetupError> {
    ensure_package_installed(paths, runner, staged, version, architecture)?;
    reset_agent_failure(paths, runner)?;
    enable_runtime_units(paths, runner)?;
    run_checked(
        runner,
        Command::new("/usr/bin/systemctl", ["restart", &paths.service]),
    )?;
    run_checked(
        runner,
        Command::new("/usr/bin/systemctl", ["restart", MONITOR_SERVICE]),
    )?;
    verify_sustained_readiness(paths, runner)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use tempfile::tempdir;

    #[cfg(target_os = "linux")]
    #[test]
    fn real_apt_dependency_diagnostic_is_available_for_retry_decision() {
        let output = run_process(
            Command::new(
                "/usr/bin/apt-get",
                [
                    "-s",
                    "install",
                    "vonk-forge-nonexistent-test-dependency-98765",
                ],
            )
            .capture_and_forward_stderr(),
            // CI runners may need time to read their APT lists; keep this
            // diagnostic bounded without using the 180-second production limit.
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
}
