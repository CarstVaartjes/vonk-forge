use std::fs;
use std::io::Read;
use std::os::unix::fs::MetadataExt;
use std::process::{Command, Stdio};
use std::time::Duration;

use wait_timeout::ChildExt;

use super::PATH;

pub(super) fn command(path: &str, arguments: &[&str], offline: bool) -> Result<String, String> {
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
    // is what the maintainer shims, the package hooks, the watchdog and dpkg
    // itself run. dpkg's own set is its checkpath (dpkg 1.22 src/main/help.c,
    // called from archives.c): `--install` refuses to run without sh, rm, tar,
    // diff, dpkg-deb, ldconfig and start-stop-daemon on its PATH. perl runs
    // deb-systemd-helper, deb-systemd-invoke and adduser; adduser execs
    // useradd and groupadd.
    for executable in [
        "/bin/sh",
        "/usr/bin/deb-systemd-helper",
        "/usr/bin/deb-systemd-invoke",
        "/usr/bin/diff",
        "/usr/bin/dpkg",
        "/usr/bin/dpkg-deb",
        "/usr/bin/dpkg-query",
        "/usr/bin/loginctl",
        "/usr/bin/perl",
        "/usr/bin/rm",
        "/usr/bin/systemctl",
        "/usr/bin/systemd-run",
        "/usr/bin/tar",
        "/usr/sbin/addgroup",
        "/usr/sbin/adduser",
        "/usr/sbin/groupadd",
        "/usr/sbin/ldconfig",
        "/usr/sbin/nologin",
        "/usr/sbin/start-stop-daemon",
        "/usr/sbin/useradd",
        "/usr/sbin/usermod",
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
