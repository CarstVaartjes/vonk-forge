//! Bounded subprocess diagnostics with process-group termination on timeout.
use std::collections::VecDeque;
use std::io::Read;
use std::os::unix::process::CommandExt;
use std::process::{Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};
use wait_timeout::ChildExt;

/// Bytes retained per stream.  Wide enough that a runtime failure's own cause
/// is still in hand when the retention window is chosen, and bounded so a
/// command that never stops writing cannot grow the helper.
const STREAM_RETAINED_BYTES: usize = 32 * 1024;
/// Bytes of the two retained tails that a single-document caller receives.
const DIAGNOSTIC_BYTES: usize = 8192;
/// Bytes of that budget each stream keeps for itself.  A share rather than a
/// first-come tail, so a command that writes a lot to one stream cannot
/// displace the other stream's last words.
const DIAGNOSTIC_SHARE_BYTES: usize = DIAGNOSTIC_BYTES / 2;

pub struct Output {
    pub status: ExitStatus,
    pub timed_out: bool,
    /// The retained tail of standard output alone.
    pub stdout: Vec<u8>,
    /// The retained tail of standard error alone.
    pub stderr: Vec<u8>,
}

impl Output {
    /// Both retained tails as one bounded document, for the callers that only
    /// ever had one stream of interest -- package and rollback commands.  A
    /// container failure keeps its streams apart instead of merging them here.
    pub fn diagnostic(&self) -> Vec<u8> {
        let mut diagnostic = tail_of(&self.stdout, DIAGNOSTIC_SHARE_BYTES).to_vec();
        diagnostic.extend_from_slice(tail_of(&self.stderr, DIAGNOSTIC_SHARE_BYTES));
        diagnostic
    }
}

fn tail_of(stream: &[u8], limit: usize) -> &[u8] {
    &stream[stream.len().saturating_sub(limit)..]
}

// One nonblocking read per stream per poll keeps a noisy producer from
// starving the child/deadline observation. No reader thread survives a request.
fn drain_once(source: &mut impl Read, tail: &mut VecDeque<u8>) -> std::io::Result<bool> {
    let mut buffer = [0; 4096];
    match source.read(&mut buffer) {
        Ok(0) => Ok(true),
        Ok(size) => {
            for byte in &buffer[..size] {
                if tail.len() == STREAM_RETAINED_BYTES {
                    tail.pop_front();
                }
                tail.push_back(*byte);
            }
            Ok(false)
        }
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
            ) =>
        {
            Ok(false)
        }
        Err(error) => Err(error),
    }
}

pub fn run(command: &mut Command, timeout: Duration) -> Result<Output, String> {
    run_inner(command, timeout)
}

/// Retain both streams without copying raw runtime output to the helper journal.
pub fn run_quiet(command: &mut Command, timeout: Duration) -> Result<Output, String> {
    run_inner(command, timeout)
}

fn run_inner(command: &mut Command, timeout: Duration) -> Result<Output, String> {
    let deadline = Instant::now() + timeout;
    let settlement_deadline = deadline + Duration::from_secs(1);
    command
        .process_group(0)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|e| format!("package command could not start: {e}"))?;
    let group = rustix::process::Pid::from_raw(child.id() as i32);
    let result = (|| {
        let mut stdout = child
            .stdout
            .take()
            .ok_or("package output custody unavailable")?;
        let mut stderr = child
            .stderr
            .take()
            .ok_or("package error custody unavailable")?;
        for pipe in [
            &stdout as &dyn std::os::fd::AsFd,
            &stderr as &dyn std::os::fd::AsFd,
        ] {
            let flags = rustix::fs::fcntl_getfl(pipe).map_err(|e| e.to_string())?;
            rustix::fs::fcntl_setfl(pipe, flags | rustix::fs::OFlags::NONBLOCK)
                .map_err(|e| e.to_string())?;
        }
        let (mut out, mut err) = (VecDeque::new(), VecDeque::new());
        let (mut out_closed, mut err_closed) = (false, false);
        let mut status = None;
        loop {
            if !out_closed {
                out_closed = drain_once(&mut stdout, &mut out).map_err(|e| e.to_string())?;
            }
            if !err_closed {
                err_closed = drain_once(&mut stderr, &mut err).map_err(|e| e.to_string())?;
            }
            if status.is_none() {
                status = child.try_wait().map_err(|e| e.to_string())?;
            }
            if out_closed
                && err_closed
                && let Some(status) = status
            {
                return Ok(Output {
                    status,
                    timed_out: false,
                    stdout: out.into_iter().collect(),
                    stderr: err.into_iter().collect(),
                });
            }
            if Instant::now() >= deadline {
                stop_group(group)?;
                // Reap has its own finite settlement allowance, never wait().
                let status = match status {
                    Some(status) => status,
                    None => child
                        .wait_timeout(settlement_deadline.saturating_duration_since(Instant::now()))
                        .map_err(|e| e.to_string())?
                        .ok_or("package process settlement is unknown")?,
                };
                for byte in b"\nCommand settlement exceeded its deadline.\n" {
                    if err.len() == STREAM_RETAINED_BYTES {
                        err.pop_front();
                    }
                    err.push_back(*byte);
                }
                return Ok(Output {
                    status,
                    timed_out: true,
                    stdout: out.into_iter().collect(),
                    stderr: err.into_iter().collect(),
                });
            }
            std::thread::sleep(
                Duration::from_millis(1).min(deadline.saturating_duration_since(Instant::now())),
            );
        }
    })();
    // Also settle descendants of a successfully exited parent. Failed reads or
    // waits have exactly the same owned cleanup; no pipe joins can retain slots.
    let cleanup_deadline = settlement_deadline.min(Instant::now() + Duration::from_secs(1));
    let stopped = stop_group(group);
    if result.is_err() {
        let _ = child.kill();
        let _ = child.wait_timeout(cleanup_deadline.saturating_duration_since(Instant::now()));
    }
    stopped?;
    observe_group_settlement(group, cleanup_deadline)?;
    result
}

fn stop_group(group: Option<rustix::process::Pid>) -> Result<(), String> {
    let group = group.ok_or("invalid package pid")?;
    match rustix::process::kill_process_group(group, rustix::process::Signal::KILL) {
        Ok(()) | Err(rustix::io::Errno::SRCH) => Ok(()),
        Err(error) => Err(format!("package process settlement is unknown: {error}")),
    }
}

fn observe_group_settlement(
    group: Option<rustix::process::Pid>,
    deadline: Instant,
) -> Result<(), String> {
    let group = group.ok_or("invalid package pid")?;
    loop {
        match rustix::process::test_kill_process_group(group) {
            Err(rustix::io::Errno::SRCH) => return Ok(()),
            Ok(()) if Instant::now() < deadline => {
                std::thread::sleep(
                    Duration::from_millis(1)
                        .min(deadline.saturating_duration_since(Instant::now())),
                );
            }
            _ => return Err("package process settlement is unknown".to_owned()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn timeout_stops_child_script_before_it_can_mutate_state() {
        let dir = tempfile::tempdir().unwrap();
        let marker = dir.path().join("should-not-exist");
        let mut command = Command::new("/bin/sh");
        command
            .args(["-c", "(sleep 1; echo late > \"$1\") & wait", "test"])
            .arg(&marker);
        if let Ok(result) = run(&mut command, Duration::from_millis(100)) {
            assert!(result.timed_out);
        }
        let mut fresh = Command::new("/bin/sh");
        fresh.args(["-c", "exit 0"]);
        assert!(
            run(&mut fresh, Duration::from_secs(1))
                .unwrap()
                .status
                .success()
        );
        std::thread::sleep(Duration::from_millis(1200));
        assert!(!marker.exists());
    }
    #[test]
    fn parent_exit_does_not_escape_the_pipe_deadline_or_block_fresh_work() {
        let dir = tempfile::tempdir().unwrap();
        let marker = dir.path().join("late-effect");
        let mut command = Command::new("/bin/sh");
        command
            .args(["-c", "(sleep 1; echo late > \"$1\") & exit 0", "test"])
            .arg(&marker);
        let began = Instant::now();
        if let Ok(result) = run_quiet(&mut command, Duration::from_millis(50)) {
            assert!(result.timed_out);
        }
        assert!(began.elapsed() < Duration::from_secs(2));
        let mut fresh = Command::new("/bin/sh");
        fresh.args(["-c", "printf fresh; printf error >&2"]);
        let result = run_quiet(&mut fresh, Duration::from_secs(1)).unwrap();
        assert!(result.status.success());
        assert_eq!(result.stdout, b"fresh");
        assert_eq!(result.stderr, b"error");
        std::thread::sleep(Duration::from_millis(1100));
        assert!(!marker.exists());
    }
    #[test]
    fn quiet_capture_drains_large_output_and_keeps_both_tails() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "i=0; while [ $i -lt 2000 ]; do echo startup-line; echo error-line >&2; i=$((i+1)); done; echo final-stdout; echo final-stderr >&2"]);
        let result = run_quiet(&mut command, Duration::from_secs(5)).unwrap();
        assert!(result.status.success());
        assert!(!result.timed_out);
        let diagnostic = result.diagnostic();
        assert!(diagnostic.len() <= 8192);
        let text = String::from_utf8_lossy(&diagnostic);
        assert!(text.contains("final-stdout"));
        assert!(text.contains("final-stderr"));
    }
    #[test]
    fn failure_keeps_exit_status_and_stderr() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "echo configuration-failed >&2; exit 7"]);
        let result = run(&mut command, Duration::from_secs(5)).unwrap();
        assert_eq!(result.status.code(), Some(7));
        assert!(!result.timed_out);
        assert!(String::from_utf8_lossy(&result.diagnostic()).contains("configuration-failed"));
    }
}
