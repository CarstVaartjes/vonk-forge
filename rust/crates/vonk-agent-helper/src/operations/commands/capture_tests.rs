#![cfg(test)]
use super::*;

#[test]
fn inherited_stdout_expires_and_a_fresh_command_is_admitted() {
    let mut stalled = Command::new("/bin/sh");
    stalled
        .args(["-c", "sleep 10 & exit 0"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let began = Instant::now();
    assert_eq!(
        run_captured_command(&mut stalled, Duration::from_millis(50)),
        Err(RunnerFailure::Deadline)
    );
    assert!(began.elapsed() < Duration::from_secs(2));
    let mut fresh = Command::new("/bin/sh");
    fresh
        .args(["-c", "printf fresh"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let result = run_captured_command(&mut fresh, Duration::from_secs(1)).unwrap();
    assert!(result.success);
    assert_eq!(result.stdout, b"fresh");
}

fn shell(script: &str) -> Command {
    let mut command = Command::new("/bin/sh");
    command
        .args(["-c", script])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

#[test]
fn a_command_that_cannot_start_is_typed() {
    let mut missing = Command::new("/nonexistent/vonk-command");
    assert_eq!(
        run_captured_command(&mut missing, Duration::from_secs(1)),
        Err(RunnerFailure::Spawn)
    );
}

#[test]
fn output_over_its_bound_is_typed() {
    let mut flood = shell("head -c 8192 /dev/zero");
    assert_eq!(
        run_captured_command(&mut flood, Duration::from_secs(5)),
        Err(RunnerFailure::OutputBound)
    );
}

fn lines_of(script: &str) -> (Result<CommandOutput, RunnerFailure>, Vec<String>) {
    let mut lines = Vec::new();
    let result = run_line_command(&mut shell(script), Duration::from_secs(5), &mut |line| {
        lines.push(line.to_owned());
    });
    (result, lines)
}

#[test]
fn a_listing_has_no_total_bound_only_a_line_bound() {
    // 20,000 lines is far over the single-answer bound; the last line has
    // no newline and is still delivered.
    let (result, lines) =
        lines_of("i=0; while [ $i -lt 20000 ]; do echo line-$i; i=$((i+1)); done; printf tail");
    assert!(result.unwrap().success);
    assert_eq!(lines.len(), 20001);
    assert_eq!(lines[19999], "line-19999");
    assert_eq!(lines[20000], "tail");
}

#[test]
fn a_line_over_its_bound_is_typed_and_a_failed_exit_keeps_its_status() {
    let (result, _) = lines_of("head -c 4096 /dev/zero | tr '\\0' x");
    assert_eq!(result, Err(RunnerFailure::OutputBound));
    let (result, lines) = lines_of("echo kept; exit 3");
    assert_eq!(result.unwrap().exit_code, Some(3));
    assert_eq!(lines, ["kept"]);
}

#[test]
fn a_failed_exit_keeps_its_status_and_the_tail_of_its_stderr() {
    // Standard error never counts toward the output bound: the command's
    // own answer is kept, and only the end of its reason is retained.
    let mut failing = shell("head -c 8192 /dev/zero >&2; printf daemon-refused >&2; exit 3");
    let output = run_captured_command(&mut failing, Duration::from_secs(5)).unwrap();
    assert!(!output.success);
    assert_eq!(output.stderr.len(), COMMAND_STDERR_TAIL_BYTES);
    let CommandFailure::Exit { code, stderr } = CommandFailure::exit(&output) else {
        panic!("an exited command is an exit cause");
    };
    assert_eq!(code, Some(3));
    assert!(stderr.ends_with("daemon-refused"));
    assert_eq!(stderr.len(), COMMAND_STDERR_TAIL_BYTES);
}

#[test]
fn a_runner_reason_survives_as_the_command_failure_cause() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    fs::create_dir_all(&roots.data).unwrap();
    let executor = OperationExecutor::new(roots, &[0; 32], RefusingRunner, None).unwrap();
    let error = executor.run_docker(&[]).unwrap_err();
    assert!(matches!(
        error,
        OperationError::CommandFailed {
            cause: CommandFailure::Runner(ref reason),
        } if *reason == RunnerFailure::Deadline.to_string()
    ));
}

struct RefusingRunner;

impl CommandRunner for RefusingRunner {
    fn run(&self, _: &Path, _: &[String]) -> Result<CommandOutput, String> {
        Err(RunnerFailure::Deadline.to_string())
    }
}
