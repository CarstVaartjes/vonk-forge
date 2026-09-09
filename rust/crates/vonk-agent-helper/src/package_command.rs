//! Package subprocesses retain diagnostics and are terminated as one process group.
use std::collections::VecDeque;
use std::io::{Read, Write};
use std::os::unix::process::CommandExt;
use std::process::{Command, ExitStatus, Stdio};
use std::thread;
use std::time::Duration;
use wait_timeout::ChildExt;

pub struct Output {
    pub status: ExitStatus,
    pub timed_out: bool,
    pub diagnostic: Vec<u8>,
}

fn drain(mut source: impl Read, stderr: bool) -> std::io::Result<Vec<u8>> {
    let mut tail = VecDeque::with_capacity(4096);
    let mut buffer = [0; 4096];
    loop {
        let size = source.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        // Journal output is streamed; diagnostic retention never limits execution.
        if stderr {
            let _ = std::io::stderr().write_all(&buffer[..size]);
        } else {
            let _ = std::io::stdout().write_all(&buffer[..size]);
        }
        for byte in &buffer[..size] {
            if tail.len() == 4096 {
                tail.pop_front();
            }
            tail.push_back(*byte);
        }
    }
    Ok(tail.into_iter().collect())
}

pub fn run(command: &mut Command, timeout: Duration) -> Result<Output, String> {
    command
        .process_group(0)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|e| format!("package command could not start: {e}"))?;
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let out_reader = thread::spawn(move || drain(stdout, false));
    let err_reader = thread::spawn(move || drain(stderr, true));
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
    let mut diagnostic = out_reader
        .join()
        .map_err(|_| "package output reader failed")?
        .map_err(|e| e.to_string())?;
    let stderr = err_reader
        .join()
        .map_err(|_| "package error reader failed")?
        .map_err(|e| e.to_string())?;
    diagnostic.extend_from_slice(&stderr);
    if timed_out {
        diagnostic
            .extend_from_slice(b"\nPackage command timed out; its process group was terminated.\n");
    }
    if diagnostic.len() > 8192 {
        diagnostic.drain(..diagnostic.len() - 8192);
    }
    Ok(Output {
        status,
        timed_out,
        diagnostic,
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
    fn failure_keeps_exit_status_and_stderr() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "echo configuration-failed >&2; exit 7"]);
        let result = run(&mut command, Duration::from_secs(5)).unwrap();
        assert_eq!(result.status.code(), Some(7));
        assert!(!result.timed_out);
        assert!(String::from_utf8_lossy(&result.diagnostic).contains("configuration-failed"));
    }
}
