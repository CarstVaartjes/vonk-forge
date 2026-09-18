//! Bounded subprocess diagnostics with process-group termination on timeout.
use std::collections::VecDeque;
use std::io::{Read, Write};
use std::os::unix::process::CommandExt;
use std::process::{Command, ExitStatus, Stdio};
use std::thread;
use std::time::Duration;
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

fn drain(mut source: impl Read, stderr: bool, mirror: bool) -> std::io::Result<Vec<u8>> {
    let mut tail = VecDeque::with_capacity(STREAM_RETAINED_BYTES);
    let mut buffer = [0; 4096];
    loop {
        let size = source.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        // Journal output is streamed; diagnostic retention never limits execution.
        if mirror && stderr {
            let _ = std::io::stderr().write_all(&buffer[..size]);
        } else if mirror {
            let _ = std::io::stdout().write_all(&buffer[..size]);
        }
        for byte in &buffer[..size] {
            if tail.len() == STREAM_RETAINED_BYTES {
                tail.pop_front();
            }
            tail.push_back(*byte);
        }
    }
    Ok(tail.into_iter().collect())
}

pub fn run(command: &mut Command, timeout: Duration) -> Result<Output, String> {
    run_inner(command, timeout, true)
}

/// Retain both streams without copying raw runtime output to the helper journal.
pub fn run_quiet(command: &mut Command, timeout: Duration) -> Result<Output, String> {
    run_inner(command, timeout, false)
}

fn run_inner(command: &mut Command, timeout: Duration, mirror: bool) -> Result<Output, String> {
    command
        .process_group(0)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|e| format!("package command could not start: {e}"))?;
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let out_reader = thread::spawn(move || drain(stdout, false, mirror));
    let err_reader = thread::spawn(move || drain(stderr, true, mirror));
    let wait = child.wait_timeout(timeout);
    let timed_out = !matches!(wait, Ok(Some(_)));
    let status = match wait {
        Ok(Some(status)) => status,
        _ => {
            // Maintainer scripts inherit this group. Kill them before rollback
            // starts, rather than leaving scripts alive after killing only dpkg.
            let group =
                rustix::process::Pid::from_raw(child.id() as i32).ok_or("invalid package pid")?;
            rustix::process::kill_process_group(group, rustix::process::Signal::KILL)
                .or_else(|e| {
                    if e == rustix::io::Errno::SRCH {
                        Ok(())
                    } else {
                        Err(e)
                    }
                })
                .map_err(|e| format!("package process group could not stop: {e}"))?;
            child
                .wait()
                .map_err(|e| format!("package process could not be reaped: {e}"))?
        }
    };
    let stdout = out_reader
        .join()
        .map_err(|_| "package output reader failed")?
        .map_err(|e| e.to_string())?;
    let mut stderr = err_reader
        .join()
        .map_err(|_| "package error reader failed")?
        .map_err(|e| e.to_string())?;
    if timed_out {
        stderr
            .extend_from_slice(b"\nPackage command timed out; its process group was terminated.\n");
    }
    Ok(Output {
        status,
        timed_out,
        stdout,
        stderr,
    })
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
        let result = run(&mut command, Duration::from_millis(100)).unwrap();
        assert!(result.timed_out);
        thread::sleep(Duration::from_millis(1200));
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
