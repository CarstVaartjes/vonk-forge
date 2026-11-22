use super::*;

#[test]
fn inherited_pipes_end_within_the_command_budget_and_a_fresh_command_runs() {
    let started = Instant::now();
    let liveness = Liveness::new(Duration::from_millis(60));
    let mut child = Command::new("/bin/sh")
        .args(["-c", "sleep 30 & exit 0"])
        .process_group(0)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let capture = capture_diagnostics(child.stdout.take().unwrap(), 1024, liveness, stop).unwrap();
    assert!(join_diagnostics(capture).is_err());
    terminate_process(&mut child, Program::Curl, &[]).unwrap();
    assert!(started.elapsed() < Duration::from_secs(3));
    let directory = tempfile::tempdir().unwrap();
    let file = directory.path().join("fresh");
    fs::write(&file, b"fresh").unwrap();
    let output = SystemProcessRunner
        .run(
            Program::Curl,
            &["--silent".into(), format!("file://{}", file.display())],
            Duration::from_secs(2),
        )
        .unwrap();
    assert!(output.success);
    assert_eq!(output.stdout, b"fresh");
}

#[test]
fn failed_service_stop_submission_cannot_bypass_owned_child_reap() {
    // No user manager (or a failed stop reply) is not proof of quiescence;
    // nevertheless the exact local process group must always be killed.
    let mut child = Command::new("/bin/sleep")
        .arg("30")
        .process_group(0)
        .spawn()
        .unwrap();
    let arguments = vec![
        "--user".into(),
        format!("--unit=vonk-runtime-adapter-{}", uuid::Uuid::new_v4()),
    ];
    let started = Instant::now();
    let _ = terminate_process(&mut child, Program::SystemdRun, &arguments);
    assert!(started.elapsed() < Duration::from_secs(3));
    assert!(child.try_wait().unwrap().is_some());
    let mut fresh = Command::new("/bin/true").spawn().unwrap();
    reap_until(&mut fresh, Instant::now() + Duration::from_secs(1)).unwrap();
}
