use super::*;
use tempfile::tempdir;

#[test]
fn unprivileged_upgrade_detection_defers_private_state_reads_to_root() {
    let temporary = tempdir().unwrap();
    let configuration = temporary.path().join("etc/vonk-forge-agent");
    let library = temporary.path().join("usr/lib/vonk-forge");
    fs::create_dir_all(&configuration).unwrap();
    fs::create_dir_all(&library).unwrap();
    let paths = InstallPaths {
        config: configuration.join("agent.toml"),
        ca: configuration.join("controller-ca.pem"),
        firewall_config: configuration.join("docker-firewall.conf"),
        helper_authority: configuration.join("host-helper-authority.pub"),
        hosts: temporary.path().join("etc/hosts"),
        agent: library.join("vonk-agent"),
        staging_root: temporary.path().join("var/tmp"),
        sudo: PathBuf::from("/usr/bin/sudo"),
        service: SERVICE.to_owned(),
        required_owner: None,
    };
    fs::write(
            &paths.config,
            format!(
                "enrollment_url = \"https://enroll.example.test/\"\ncontroller_url = \"https://controller.example.test/\"\nca_path = \"{}\"\nca_sha256 = \"{}\"\ndata_dir = \"{}\"\nnode_id = \"spk_0123456789abcdef0123456789abcdef\"\nfabric_address = \"192.168.100.10\"\nfabric_bandwidth_mbps = 200000\n",
                paths.ca.display(),
                "0".repeat(64),
                DATA_DIR,
            ),
        )
        .unwrap();
    fs::write(&paths.ca, b"controller CA\n").unwrap();
    fs::write(
            &paths.firewall_config,
            "VONK_NAS_MANAGEMENT_IP=192.168.1.231\nVONK_NODE_MANAGEMENT_IP=192.168.1.211\nVONK_NODE_FABRIC_IP=192.168.100.10\nVONK_PEER_FABRIC_IP=192.168.100.11\nVONK_ENDPOINT_HOST_PORTS=8000,8101\nVONK_HOST_ENDPOINT_PORTS=8888\nVONK_RENDEZVOUS_PORT=29500\n",
        )
        .unwrap();
    fs::write(&paths.helper_authority, format!("{}\n", "11".repeat(32))).unwrap();
    fs::write(&paths.agent, b"agent").unwrap();
    fs::write(setup_state_path(&paths), b"paired-v1\n").unwrap();
    fs::set_permissions(&paths.firewall_config, fs::Permissions::from_mode(0o000)).unwrap();

    assert!(matches!(install_state(&paths), Ok(InstallState::PairedV1)));
    assert!(matches!(install_state(&paths), Ok(InstallState::PairedV1)));
}
