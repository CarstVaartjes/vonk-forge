#![cfg(test)]
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
