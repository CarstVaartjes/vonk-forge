#![cfg(test)]
use super::*;

#[test]
fn system_commands_settle_at_the_deadline_and_admit_next_command() {
    let mut runner = SystemCommandRunner;
    let started = Instant::now();
    let result = runner.run_with_deadline_for_test(
        Command::new("/bin/sh", ["-c", "sleep 30 & wait"]),
        Duration::from_millis(50),
    );
    assert!(result.is_err());
    assert!(
        runner
            .run(Command::new("/bin/sh", ["-c", "exit 0"]))
            .unwrap()
            .success
    );
    assert!(started.elapsed() < Duration::from_secs(5));
}
#[test]
fn exited_parent_with_inherited_pipes_ends_and_next_command_runs() {
    let directory = tempfile::tempdir().unwrap();
    let exited = directory.path().join("parent-exited");
    let escaped_effect = directory.path().join("descendant-effect");
    let script = format!(
        "(sleep 1; touch '{}') & printf probe-marker > '{}'",
        escaped_effect.display(),
        exited.display()
    );
    let started = Instant::now();
    let result = run_process(
        Command::new("/bin/sh", ["-c", &script]),
        Duration::from_millis(200),
    );
    assert!(result.is_err());
    assert_eq!(fs::read(exited).unwrap(), b"probe-marker");
    assert!(started.elapsed() < Duration::from_secs(5));
    assert!(
        run_process(
            Command::new("/bin/sh", ["-c", "printf probe-bytes"]),
            Duration::from_secs(1)
        )
        .unwrap()
        .stdout
            == b"probe-bytes"
    );
    thread::sleep(Duration::from_millis(1100));
    assert!(!escaped_effect.exists());
}

#[test]
fn output_flood_is_bounded_and_next_command_runs() {
    let started = Instant::now();
    let output = run_process(
        Command::new("/bin/sh", ["-c", "head -c 3145728 /dev/zero"]),
        Duration::from_secs(2),
    )
    .unwrap();
    assert!(output.success);
    assert_eq!(output.stdout.len(), MAX_STDOUT_BYTES);
    assert!(started.elapsed() < Duration::from_secs(5));
    assert!(
        run_process(
            Command::new("/bin/sh", ["-c", "exit 0"]),
            Duration::from_secs(1)
        )
        .unwrap()
        .success
    );
}
