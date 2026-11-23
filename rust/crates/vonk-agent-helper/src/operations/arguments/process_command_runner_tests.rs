#![cfg(test)]
#[cfg(target_os = "linux")]
use std::path::Path;
#[cfg(target_os = "linux")]
use std::time::Duration;

use super::ROOT_COMMAND_PATH;
#[cfg(target_os = "linux")]
use super::{CommandRunner, ProcessCommandRunner};

#[test]
fn privileged_command_path_includes_debian_administrative_binaries() {
    let entries = ROOT_COMMAND_PATH.split(':').collect::<Vec<_>>();
    assert!(entries.contains(&"/usr/sbin"));
    assert!(entries.contains(&"/sbin"));
    assert!(entries.iter().all(|entry| entry.starts_with('/')));
}

#[cfg(target_os = "linux")]
#[test]
fn dpkg_diagnostics_use_service_output_without_bounded_capture() {
    let result = ProcessCommandRunner
        .run_with_timeout(
            Path::new("/usr/bin/dpkg"),
            &["--version".to_owned()],
            Duration::from_secs(5),
        )
        .expect("the Linux test host must provide dpkg");

    assert!(result.success);
    assert!(String::from_utf8_lossy(&result.stdout).contains("Debian"));
}
