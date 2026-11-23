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
