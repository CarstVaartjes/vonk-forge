use std::process::Command;

#[test]
fn executable_rejects_secret_values_in_argv() {
    let output = Command::new(env!("CARGO_BIN_EXE_vonk-nas-setup"))
        .args([
            "--template",
            "payload.json",
            "--output",
            "bundle",
            "--database-password",
            "must-not-be-accepted",
        ])
        .output()
        .expect("run setup executable");

    assert!(!output.status.success());
}
