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
        if let Ok(current_pid) = current_pid.and_then(|output| {
            let value = String::from_utf8(output.stdout)
                .map_err(|_| SetupError::Command("systemctl".to_owned()))?;
            let value = value.trim();
            if value
                .parse::<u32>()
                .ok()
                .as_ref()
                .is_none_or(|value| *value == 0)
            {
                return Err(SetupError::Command("systemctl".to_owned()));
            }
            Ok(value.to_owned())
        }) {
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
        } else {
            healthy = 0;
            pid = None;
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
        #[expect(
            clippy::let_underscore_must_use,
            reason = "best-effort cleanup: the next reconciliation pass repeats it"
        )]
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
/// values (for example, systemd's `MainPID`).  Final failure diagnostics are
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
mod tests;
