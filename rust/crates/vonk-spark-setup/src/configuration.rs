//! Configuration.

use super::*;

pub(super) fn install_configuration(
    paths: &InstallPaths,
    config: &GeneratedConfig,
    firewall: &FirewallConfig,
    helper_authority: &[u8],
    ca: &[u8],
    owner: u32,
) -> Result<(), SetupError> {
    let rendered = config.to_toml();
    let parsed: WrittenConfig =
        toml::from_str(&rendered).map_err(|_| SetupError::PrivilegedInput)?;
    if !valid_written_config(&parsed, paths) {
        return Err(SetupError::PrivilegedInput);
    }
    if !valid_helper_authority(helper_authority) {
        return Err(SetupError::PrivilegedInput);
    }
    atomic_root_write(&paths.ca, ca, owner, 0o644)?;
    atomic_root_write(&paths.config, rendered.as_bytes(), owner, 0o644)?;
    atomic_root_write(
        &paths.firewall_config,
        firewall.render().as_bytes(),
        owner,
        0o600,
    )?;
    atomic_root_write(
        &paths.helper_authority,
        format!("{}\n", hex::encode(helper_authority)).as_bytes(),
        owner,
        0o644,
    )
}

/// Rewrite the validated `agent.toml` in the current canonical form so keys
/// retired by newer releases disappear, and drop files left by removed features.
pub(super) fn refresh_configuration(
    paths: &InstallPaths,
    config: &WrittenConfig,
    owner: u32,
) -> Result<(), SetupError> {
    let canonical = GeneratedConfig {
        enrollment_url: config.enrollment_url.clone(),
        controller_url: config.controller_url.clone(),
        ca_path: config.ca_path.clone(),
        ca_sha256: config.ca_sha256.clone(),
        node_id: config.node_id.clone(),
        fabric_address: config.fabric_address,
        fabric_bandwidth_mbps: config.fabric_bandwidth_mbps,
    };
    atomic_root_write(&paths.config, canonical.to_toml().as_bytes(), owner, 0o644)?;
    if let Some(directory) = paths.config.parent() {
        match fs::remove_file(directory.join("observation-receipt.pub")) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(SetupError::PrivilegedWrite(error)),
        }
    }
    Ok(())
}

pub(super) const HOSTS_BEGIN: &str = "# BEGIN VONK FORGE MANAGED HOSTS";
pub(super) const HOSTS_END: &str = "# END VONK FORGE MANAGED HOSTS";
pub(super) const MAX_HOSTS_BYTES: u64 = 1024 * 1024;

pub(super) fn install_host_mapping(
    paths: &InstallPaths,
    mapping: &HostMapping,
    owner: u32,
) -> Result<(), SetupError> {
    if !valid_host_mapping(Some(mapping)) {
        return Err(SetupError::PrivilegedInput);
    }
    let metadata = fs::symlink_metadata(&paths.hosts).map_err(SetupError::PrivilegedWrite)?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.uid() != owner
        || metadata.len() > MAX_HOSTS_BYTES
        || metadata.permissions().mode() & 0o022 != 0
    {
        return Err(SetupError::PrivilegedInput);
    }
    let existing = fs::read_to_string(&paths.hosts).map_err(SetupError::PrivilegedWrite)?;
    let mut retained = Vec::new();
    let mut managed = false;
    for line in existing.lines() {
        if line == HOSTS_BEGIN {
            if managed {
                return Err(SetupError::PrivilegedInput);
            }
            managed = true;
        } else if line == HOSTS_END {
            if !managed {
                return Err(SetupError::PrivilegedInput);
            }
            managed = false;
        } else if !managed {
            retained.push(line);
        }
    }
    if managed {
        return Err(SetupError::PrivilegedInput);
    }
    let mut remaining = retained.len();
    while remaining > 0 && retained.last().is_some_and(|line| line.is_empty()) {
        retained.pop();
        remaining -= 1;
    }
    let mut rendered = retained.join("\n");
    if !rendered.is_empty() {
        rendered.push('\n');
    }
    rendered.push_str(HOSTS_BEGIN);
    rendered.push('\n');
    rendered.push_str(&mapping.address);
    rendered.push(' ');
    rendered.push_str(&mapping.hostnames.join(" "));
    rendered.push('\n');
    rendered.push_str(HOSTS_END);
    rendered.push('\n');
    atomic_root_write(&paths.hosts, rendered.as_bytes(), owner, 0o644)
}

pub(super) fn setup_state_path(paths: &InstallPaths) -> PathBuf {
    paths.config.with_file_name("setup-state")
}

pub(super) fn write_setup_state(
    paths: &InstallPaths,
    state: &[u8],
    owner: u32,
) -> Result<(), SetupError> {
    atomic_root_write(&setup_state_path(paths), state, owner, 0o644)
}

pub(super) fn pair_agent(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    enrollment_url: &Url,
    ca_sha256: &str,
    pairing_token: String,
) -> Result<(), SetupError> {
    if !valid_token(&pairing_token) {
        return Err(SetupError::UnsafeInput("pairing token"));
    }
    let pair = Command::new(
        "/usr/bin/setpriv",
        [
            "--reuid",
            "vonk-agent",
            "--regid",
            "vonk-agent",
            "--clear-groups",
            "--",
            paths.agent.to_string_lossy().as_ref(),
            "--config",
            paths.config.to_string_lossy().as_ref(),
            "pair",
            "--enrollment",
            enrollment_url.as_str(),
            "--ca-sha256",
            ca_sha256,
            "--token-stdin",
        ],
    )
    .with_stdin(format!("{pairing_token}\n").into_bytes());
    run_checked(runner, pair)?;
    Ok(())
}

pub(super) fn start_and_verify(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    eprintln!("vonk-spark-setup: phase=start-services elapsed=0s");
    reset_agent_failure(paths, runner)?;
    enable_runtime_units(paths, runner)?;
    eprintln!("vonk-spark-setup: phase=readiness elapsed=0s");
    verify_sustained_readiness(paths, runner).inspect_err(|_| {
        eprintln!("vonk-spark-setup: pairing is preserved; rerun setup without --enroll to resume readiness, or use --enroll only to replace this Spark identity");
    })
}

pub(super) fn stop_agent_for_identity_reload(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    run_checked(
        runner,
        Command::new("/usr/bin/systemctl", ["stop", &paths.service]),
    )
    .map(|_| ())
}

pub(super) fn reset_agent_failure(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    // Package installation normally asks systemd to reload units, but that
    // notification is not a reliable synchronization boundary across Debian
    // versions and containerized/systemd acceptance hosts. A fresh install can
    // otherwise reach reset-failed while systemd still considers the newly
    // installed agent unit unknown. Reload explicitly before touching unit
    // state so both fresh installs and upgrades converge on the package that
    // was just committed.
    run_checked(
        runner,
        Command::new("/usr/bin/systemctl", ["daemon-reload"]),
    )?;
    // `reset-failed` returns non-zero when the unit is not loaded yet. That is
    // a benign state on a fresh install and after a package was purged then
    // reinstalled quickly: the following `enable --now` is the authoritative
    // operation and readiness verification still fails closed if the unit is
    // actually unavailable. Do not turn the harmless negative lookup into an
    // unrecoverable installer failure.
    runner
        .run(Command::new(
            "/usr/bin/systemctl",
            ["reset-failed", &paths.service],
        ))
        .map_err(SetupError::Command)?;
    Ok(())
}

pub(super) fn enable_runtime_units(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    run_checked(
        runner,
        Command::new(
            "/usr/bin/systemctl",
            [
                "enable",
                "--now",
                FIREWALL_SERVICE,
                HELPER_SOCKET,
                &paths.service,
                MONITOR_SERVICE,
            ],
        ),
    )
    .map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn rerun_accepts_retired_config_keys_and_rewrites_canonical_config() {
        let temporary = tempdir().unwrap();
        let configuration = temporary.path().join("etc/vonk-forge-agent");
        fs::create_dir_all(&configuration).unwrap();
        let paths = InstallPaths {
            config: configuration.join("agent.toml"),
            ca: configuration.join("controller-ca.pem"),
            firewall_config: configuration.join("docker-firewall.conf"),
            helper_authority: configuration.join("host-helper-authority.pub"),
            hosts: temporary.path().join("etc/hosts"),
            agent: temporary.path().join("vonk-agent"),
            staging_root: temporary.path().join("var/tmp"),
            sudo: PathBuf::from("/usr/bin/sudo"),
            service: SERVICE.to_owned(),
            required_owner: None,
        };
        let legacy = format!(
            "enrollment_url = \"https://enroll.example.test/\"\ncontroller_url = \"https://controller.example.test/\"\nca_path = \"{}\"\nca_sha256 = \"{}\"\ndata_dir = \"{}\"\nnode_id = \"spk_0123456789abcdef0123456789abcdef\"\nfabric_address = \"192.168.100.10\"\nfabric_bandwidth_mbps = 200000\npoll_min_seconds = 2\npoll_max_seconds = 60\n",
            paths.ca.display(),
            "0".repeat(64),
            DATA_DIR,
        );
        fs::write(&paths.config, &legacy).unwrap();
        let receipt = configuration.join("observation-receipt.pub");
        fs::write(&receipt, b"stale").unwrap();

        let config = paired_configuration(&paths.config, &paths).unwrap();
        let owner = rustix::process::geteuid().as_raw();
        refresh_configuration(&paths, &config, owner).unwrap();

        let rewritten = fs::read_to_string(&paths.config).unwrap();
        assert!(!rewritten.contains("poll_"));
        assert_eq!(rewritten, legacy.split("poll_min_seconds").next().unwrap());
        assert!(paired_configuration(&paths.config, &paths).is_ok());
        assert!(!receipt.exists());
        // A second rerun with nothing stale left still succeeds.
        refresh_configuration(&paths, &config, owner).unwrap();
    }
}
