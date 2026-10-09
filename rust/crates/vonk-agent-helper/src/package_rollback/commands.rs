use std::fs;
use std::io::Read;
use std::os::unix::fs::MetadataExt;
use std::process::{Command, Stdio};
use std::time::Duration;

use wait_timeout::ChildExt;

use super::PATH;

pub(super) fn command(path: &str, arguments: &[&str], offline: bool) -> Result<String, String> {
    command_with_nonce(path, arguments, offline, None)
}

pub(super) fn command_with_nonce(
    path: &str,
    arguments: &[&str],
    offline: bool,
    nonce: Option<&str>,
) -> Result<String, String> {
    let capture = path == "/usr/bin/dpkg-query"
        || (path == "/usr/bin/systemctl" && arguments.contains(&"show"))
        || (path == "/usr/bin/dpkg-deb" && arguments.first() == Some(&"--field"));
    let mut cmd = Command::new(path);
    cmd.args(arguments)
        .env_clear()
        .env("PATH", PATH)
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .current_dir("/")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(if capture {
            Stdio::piped()
        } else {
            Stdio::null()
        });
    if offline {
        cmd.env("SYSTEMD_OFFLINE", "1");
    }
    if let Some(nonce) = nonce {
        cmd.env("VONK_FORGE_PACKAGE_ROLLBACK_NONCE", nonce);
    }
    if path == "/usr/bin/dpkg" {
        let output = crate::package_command::run(&mut cmd, Duration::from_secs(180))?;
        if !output.status.success() || output.timed_out {
            return Err(format!(
                "package command failed: {path}: {}",
                String::from_utf8_lossy(&output.diagnostic())
            ));
        }
        return Ok(String::new());
    }
    let mut child = cmd
        .spawn()
        .map_err(|_| format!("required executable unavailable: {path}"))?;
    // Package metadata output is small. Mutating commands deliberately suppress
    // output so a verbose package cannot fill this pipe while its parent waits.

    let status = match child
        .wait_timeout(Duration::from_secs(180))
        .map_err(|e| e.to_string())?
    {
        Some(status) => status,
        None => {
            let _ = child.kill();
            let _ = child.wait_timeout(Duration::from_secs(5));
            return Err("package command timed out".into());
        }
    };
    if !status.success() {
        return Err(format!("package command failed: {path}"));
    }
    let mut result = String::new();
    if let Some(stdout) = child.stdout.take() {
        stdout
            .take(65536)
            .read_to_string(&mut result)
            .map_err(|e| e.to_string())?;
    }
    Ok(result.trim().to_owned())
}

pub fn prerequisites() -> Result<(), String> {
    // Probe before dpkg invokes prerm and stops the healthy source agent. This
    // set is the actual current maintainer-script and watchdog executable set.
    for executable in [
        "/bin/sh",
        "/usr/bin/awk",
        "/usr/bin/base64",
        "/usr/bin/cat",
        "/usr/bin/chmod",
        "/usr/bin/chown",
        "/usr/bin/cp",
        "/usr/bin/cmp",
        "/usr/bin/deb-systemd-helper",
        "/usr/bin/deb-systemd-invoke",
        "/usr/bin/dirname",
        "/usr/bin/getent",
        "/usr/bin/ln",
        "/usr/bin/loginctl",
        "/usr/bin/openssl",
        "/usr/bin/sleep",
        "/usr/bin/tail",
        "/usr/sbin/addgroup",
        "/usr/sbin/nologin",
        "/usr/bin/cut",
        "/usr/bin/date",
        "/usr/bin/dpkg",
        "/usr/bin/dpkg-deb",
        "/usr/bin/dpkg-query",
        "/usr/bin/find",
        "/usr/bin/flock",
        "/usr/bin/grep",
        "/usr/bin/id",
        "/usr/bin/install",
        "/usr/bin/logger",
        "/usr/bin/mkdir",
        "/usr/bin/mktemp",
        "/usr/bin/mv",
        "/usr/bin/readlink",
        "/usr/bin/rm",
        "/usr/bin/rmdir",
        "/usr/bin/sed",
        "/usr/bin/sha256sum",
        "/usr/bin/stat",
        "/usr/bin/sync",
        "/usr/bin/systemctl",
        "/usr/bin/systemd-run",
        "/usr/bin/tr",
        "/usr/bin/wc",
        "/usr/sbin/adduser",
        "/usr/sbin/ldconfig",
        "/usr/sbin/start-stop-daemon",
    ] {
        let m = fs::metadata(executable)
            .map_err(|_| format!("missing package prerequisite: {executable}"))?;
        if !m.is_file() || m.uid() != 0 || m.mode() & 0o111 == 0 || m.mode() & 0o022 != 0 {
            return Err(format!("unsafe package prerequisite: {executable}"));
        }
    }
    command(
        "/usr/bin/systemctl",
        &["--system", "show", "--property=Version", "--value"],
        false,
    )?;
    Ok(())
}
