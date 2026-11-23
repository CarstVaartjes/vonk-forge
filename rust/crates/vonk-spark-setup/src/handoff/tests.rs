use super::*;

#[cfg(target_os = "linux")]
#[test]
fn foreground_sudo_authentication_uses_controlling_terminal() {
    if let Ok(sudo) = std::env::var("VONK_SUDO_PTY_CHILD") {
        authenticate_sudo_foreground(Path::new(&sudo)).unwrap();
        assert!(
            process::run_process(
                Command::new("/usr/bin/true", std::iter::empty::<String>()),
                DEFAULT_COMMAND_TIMEOUT,
            )
            .unwrap()
            .success
        );
        if std::env::var_os("VONK_SUDO_PTY_VERIFY_NONINTERACTIVE").is_some() {
            assert!(
                ProcessCommand::new(&sudo)
                    .args(["-n", "/usr/bin/true"])
                    .stdin(Stdio::null())
                    .status()
                    .unwrap()
                    .success()
            );
        }
        return;
    }
    let directory = tempfile::tempdir().unwrap();
    let sudo = directory.path().join("sudo");
    let ticket = directory.path().join("ticket");
    fs::write(&sudo, format!("#!/bin/sh\n[ \"$1\" = -v ] || exit 20\n[ -t 0 ] || exit 21\nIFS= read -r password </dev/tty\n[ \"$password\" = test-password ] || exit 22\nprintf authenticated > '{}'\n", ticket.display())).unwrap();
    fs::set_permissions(&sudo, fs::Permissions::from_mode(0o700)).unwrap();
    let command = format!(
        "{} --exact handoff::tests::foreground_sudo_authentication_uses_controlling_terminal",
        std::env::current_exe().unwrap().display()
    );
    let mut child = ProcessCommand::new("/usr/bin/script")
        .args(["-q", "-e", "-c", &command, "/dev/null"])
        .env("VONK_SUDO_PTY_CHILD", &sudo)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"test-password\n")
        .unwrap();
    assert!(child.wait().unwrap().success());
    assert_eq!(fs::read(&ticket).unwrap(), b"authenticated");
}
