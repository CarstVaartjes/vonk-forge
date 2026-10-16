//! Spark host settings.
//!
//! A Spark runs headless: a desktop session holds about 1 GB of the memory
//! recipe envelopes count on. Its GPU clock is capped (the packaged one-shot
//! `vonk-forge-gpu-clock-cap.service` runs `nvidia-smi -lgc 300,2200`) so
//! sustained load cannot hard power it off. At start, and at most hourly on a
//! helper request, the helper reads the default boot target, whether the
//! display manager runs and whether the clock cap unit is active this boot,
//! and corrects only what drifted with one exact command each:
//! `systemctl set-default multi-user.target`,
//! `systemctl stop display-manager.service` and
//! `systemctl start vonk-forge-gpu-clock-cap.service`. None touches a
//! container; the lock cannot be read back, so the unit state is the evidence.
//! Every correction is journaled with the state before and after it; a
//! setting that cannot be read is left alone until the next pass.

use super::*;

/// How often the helper checks the host settings.
pub const HOST_SETTINGS_INTERVAL: Duration = Duration::from_secs(60 * 60);
const SYSTEMCTL: &str = "/usr/bin/systemctl";
const HEADLESS_TARGET: &str = "multi-user.target";
// The exact command lines, compiled in: no argument comes from elsewhere.
const GET_DEFAULT: &str = "get-default";
const SET_HEADLESS: &str = "set-default multi-user.target";
const IS_DISPLAY_MANAGER_ACTIVE: &str = "is-active display-manager.service";
const STOP_DISPLAY_MANAGER: &str = "stop display-manager.service";
const IS_CLOCK_CAP_ACTIVE: &str = "is-active vonk-forge-gpu-clock-cap.service";
const START_CLOCK_CAP: &str = "start vonk-forge-gpu-clock-cap.service";
const QUERY_TIMEOUT: Duration = Duration::from_secs(10);
/// A stop waits for the desktop session to end.
const STOP_TIMEOUT: Duration = Duration::from_secs(90);
/// A start waits for the one-shot `nvidia-smi` to finish.
const START_TIMEOUT: Duration = Duration::from_secs(60);

impl<R: CommandRunner> OperationExecutor<R> {
    /// One pass; what it cannot correct now waits for the next one.
    pub fn keep_host_settings(&self) {
        match self.default_target() {
            None => eprintln!(
                "vonk-agent-helper: host settings: default target unreadable; the next pass tries again"
            ),
            Some(target) if target == HEADLESS_TARGET => {}
            Some(target) => {
                let cause = self.correct(SET_HEADLESS, QUERY_TIMEOUT);
                let after = self.default_target();
                eprintln!(
                    "vonk-agent-helper: host settings: default target {target} -> {}{cause}",
                    after.as_deref().unwrap_or("unreadable"),
                );
            }
        }
        match self.unit_state(IS_DISPLAY_MANAGER_ACTIVE) {
            None => eprintln!(
                "vonk-agent-helper: host settings: display manager state unreadable; the next pass tries again"
            ),
            Some((false, _)) => {}
            Some((true, state)) => {
                let cause = self.correct(STOP_DISPLAY_MANAGER, STOP_TIMEOUT);
                let after = self
                    .unit_state(IS_DISPLAY_MANAGER_ACTIVE)
                    .map_or_else(|| "unreadable".to_owned(), |(_, after)| after);
                eprintln!(
                    "vonk-agent-helper: host settings: display manager {state} -> {after}{cause}"
                );
            }
        }
        match self.unit_state(IS_CLOCK_CAP_ACTIVE) {
            None => eprintln!(
                "vonk-agent-helper: host settings: GPU clock cap state unreadable; the next pass tries again"
            ),
            Some((true, _)) => {}
            Some((false, state)) => {
                let cause = self.correct(START_CLOCK_CAP, START_TIMEOUT);
                let after = self
                    .unit_state(IS_CLOCK_CAP_ACTIVE)
                    .map_or_else(|| "unreadable".to_owned(), |(_, after)| after);
                eprintln!("vonk-agent-helper: host settings: GPU clock cap {state} -> {after}{cause}");
            }
        }
    }

    /// The default boot target, when systemd names one.
    fn default_target(&self) -> Option<String> {
        let output = self.systemctl(GET_DEFAULT, QUERY_TIMEOUT).ok()?;
        let target = std::str::from_utf8(&output.stdout).ok()?.trim();
        (output.success
            && target.len() <= 256
            && target.ends_with(".target")
            && target
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"@._:-".contains(&byte)))
        .then(|| target.to_owned())
    }

    /// Whether a unit is up, and its state, from one `is-active` command.
    /// `is-active` exits zero only for an active unit and prints the state
    /// either way; no state word means systemd gave no answer.
    fn unit_state(&self, query: &str) -> Option<(bool, String)> {
        let output = self.systemctl(query, QUERY_TIMEOUT).ok()?;
        let state = std::str::from_utf8(&output.stdout).ok()?.trim();
        (!state.is_empty()
            && state.len() <= 32
            && state.bytes().all(|byte| byte.is_ascii_lowercase()))
        .then(|| (output.success, state.to_owned()))
    }

    /// Run one correction; empty when it succeeded, else why it did not.
    fn correct(&self, command: &str, timeout: Duration) -> String {
        match self.systemctl(command, timeout) {
            Ok(output) if output.success => String::new(),
            Ok(output) => format!(
                "; systemctl {command} failed: {}; the next pass tries again",
                CommandFailure::exit(&output)
            ),
            Err(reason) => {
                format!("; systemctl {command} failed: {reason}; the next pass tries again")
            }
        }
    }

    fn systemctl(&self, command: &str, timeout: Duration) -> Result<CommandOutput, String> {
        let arguments: Vec<String> = command.split(' ').map(str::to_owned).collect();
        self.runner
            .run_with_timeout(Path::new(SYSTEMCTL), &arguments, timeout)
    }
}

#[cfg(test)]
mod tests;
