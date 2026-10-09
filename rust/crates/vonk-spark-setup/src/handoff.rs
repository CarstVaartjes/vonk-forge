//! Handoff.

use super::*;

pub fn handoff_to_root(
    prepared: &PreparedSetup,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    validate_native_architecture(std::env::consts::ARCH)?;
    handoff_to_root_with_authority(prepared, runner, &ReleaseAuthority::canonical())
}

pub(super) fn authenticate_sudo_foreground(sudo: &Path) -> Result<(), SetupError> {
    // The framed apply request owns stdin. Authenticate before starting it,
    // while sudo can still read the controlling terminal itself.
    let terminal = OpenOptions::new()
        .read(true)
        .write(true)
        .open("/dev/tty")
        .map_err(|_| SetupError::Command("sudo authentication requires a terminal".to_owned()))?;
    let mut child = ProcessCommand::new(sudo)
        .arg("-v")
        .stdin(terminal)
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        // Terminal reads require the caller's foreground process group.
        // Unlike the framed apply command, sudo -v performs authentication only.
        .spawn()
        .map_err(|_| SetupError::Command("could not start sudo authentication".to_owned()))?;
    let status = match child.wait_timeout(DEFAULT_COMMAND_TIMEOUT) {
        Ok(Some(status)) => status,
        _ => {
            // This child shares our foreground group: terminate only the child,
            // never the caller or the other processes attached to its terminal.
            let _ = child.kill();
            let _ = child.wait_timeout(TERMINATION_GRACE);
            return Err(SetupError::ObservationUnavailable(
                vonk_agent_protocol::generated::WaitReason::ObservationUnavailable,
            ));
        }
    };
    if !status.success() {
        return Err(SetupError::Command("sudo authentication failed".to_owned()));
    }
    Ok(())
}

pub fn handoff_to_root_with_authority(
    prepared: &PreparedSetup,
    runner: &mut dyn CommandRunner,
    authority: &ReleaseAuthority,
) -> Result<(), SetupError> {
    runner.authenticate_sudo(&prepared.sudo)?;
    let root_handoff = root_handoff_script(prepared, authority)?;
    let command = Command::new(
        &prepared.sudo,
        [
            "-n".to_owned(),
            "/bin/sh".to_owned(),
            "-ceu".to_owned(),
            root_handoff,
            "vonk-spark-root-handoff".to_owned(),
            prepared.executable.display().to_string(),
            prepared.staged.path().display().to_string(),
            prepared.setup_signature.display().to_string(),
        ],
    )
    .with_stdin(prepared.frame.clone());
    if run_checked(runner, command).is_ok() {
        return Ok(());
    }
    if run_checked(runner, Command::new(&prepared.sudo, ["-n", "-v"])).is_err() {
        return Err(SetupError::Command(
            "sudo authorization expired before privileged apply".to_owned(),
        ));
    }
    Err(SetupError::Command(
        "privileged installer handoff failed".to_owned(),
    ))
}

pub(super) fn root_handoff_script(
    prepared: &PreparedSetup,
    authority: &ReleaseAuthority,
) -> Result<String, SetupError> {
    let staging_root = prepared
        .staging_root
        .to_str()
        .filter(|value| value.starts_with('/') && !value.contains(['\n', '\r', '\0']))
        .ok_or(SetupError::PrivilegedInput)?;
    let staging_root = staging_root.replace('\'', "'\\''");
    let install_owner = if prepared.required_owner == Some(0) {
        "-o root -g root "
    } else {
        ""
    };
    let public_key =
        std::str::from_utf8(&authority.public_key_pem).map_err(|_| SetupError::ReleaseSignature)?;
    if public_key.contains("VONK_INSTALLER_RELEASE_PUBLIC_KEY") {
        return Err(SetupError::ReleaseSignature);
    }
    // The root-owned copy is the same authority that authenticated this setup.
    // Only a test-feature binary can consume it; production invokes canonical apply.
    let test_environment = if cfg!(feature = "acceptance-test-trust") {
        "VONK_ACCEPTANCE_TEST_MODE=1 VONK_ACCEPTANCE_RELEASE_PUBLIC_KEY=\"$public_key\" "
    } else {
        ""
    };
    Ok(format!(
        r#"umask 077
root=$(/usr/bin/mktemp -d '{staging_root}/vonk-spark-setup.XXXXXX')
trap '/bin/rm -rf -- "$root"' EXIT HUP INT TERM
setup=$root/vonk-spark-setup
package=$root/vonk-forge-agent.deb
encoded_signature=$root/vonk-spark-setup.sig
signature=$root/vonk-spark-setup.raw.sig
public_key=$root/installer-release-public.pem
/usr/bin/install {install_owner}-m 0700 -- "$1" "$setup"
/usr/bin/install {install_owner}-m 0600 -- "$3" "$encoded_signature"
/usr/bin/cat > "$public_key" <<'VONK_INSTALLER_RELEASE_PUBLIC_KEY'
{public_key}VONK_INSTALLER_RELEASE_PUBLIC_KEY
[ "$(/usr/bin/stat -c %s "$encoded_signature")" -le {MAX_RELEASE_SIGNATURE_BYTES} ]
/usr/bin/openssl base64 -d -A -in "$encoded_signature" -out "$signature" >/dev/null 2>&1
[ "$(/usr/bin/stat -c %s "$signature")" -gt 0 ]
[ "$(/usr/bin/stat -c %s "$signature")" -le 1024 ]
/usr/bin/openssl dgst -sha256 -verify "$public_key" -signature "$signature" "$setup" >/dev/null 2>&1
/usr/bin/install {install_owner}-m 0600 -- "$2" "$package"
{test_environment}"$setup" __apply
"#
    ))
}

#[cfg(test)]
mod tests {
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
}
