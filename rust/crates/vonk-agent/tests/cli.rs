use std::process::{Command, Output, Stdio};

fn run(arguments: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_vonk-agent"))
        .args(arguments)
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

#[test]
fn pair_is_the_only_first_enrollment_command() {
    let output = run(&["--help"]);
    assert!(output.status.success());
    let help = String::from_utf8(output.stdout).unwrap();
    assert!(help.contains("  pair"));
    assert!(help.contains("  run"));
    assert!(!help.contains("bootstrap"));
    assert!(!help.contains("migrate-python-state"));

    for removed in ["bootstrap", "migrate-python-state"] {
        let rejected = run(&[removed]);
        assert!(!rejected.status.success());
    }
}

#[test]
fn pair_tokens_are_rejected_from_process_arguments() {
    let secret = "this-token-must-never-be-an-argument";
    let output = run(&[
        "pair",
        "--enrollment",
        "https://enroll.example.test/",
        "--ca-sha256",
        &"a".repeat(64),
        "--token",
        secret,
    ]);

    assert!(!output.status.success());
    assert!(!String::from_utf8_lossy(&output.stdout).contains(secret));
    assert!(!String::from_utf8_lossy(&output.stderr).contains(secret));
}
