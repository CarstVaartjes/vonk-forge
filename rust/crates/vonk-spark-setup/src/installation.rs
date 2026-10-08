//! Installation.

use super::*;

pub(super) enum InstallState {
    Fresh,
    ConfiguredUnpaired,
    Recovering,
    Existing,
}

#[derive(Clone, Copy)]
pub(super) enum StateValidation {
    MetadataOnly,
    Complete,
}

pub(super) fn install_state(
    paths: &InstallPaths,
    validation: StateValidation,
) -> Result<InstallState, SetupError> {
    safe_existing_parent(&paths.config, paths.required_owner)?;
    safe_existing_parent(&paths.agent, paths.required_owner)?;
    let config = safe_existing_file(&paths.config, paths.required_owner)?;
    let ca = safe_existing_file(&paths.ca, paths.required_owner)?;
    let firewall = safe_existing_file(&paths.firewall_config, paths.required_owner)?;
    let helper_authority = safe_existing_file(&paths.helper_authority, paths.required_owner)?;
    let agent = safe_existing_file(&paths.agent, paths.required_owner)?;
    let state_path = setup_state_path(paths);
    let state = safe_existing_file(&state_path, paths.required_owner)?;
    match (config, ca, firewall, helper_authority, agent, state) {
        (false, false, false, false, false, false) => Ok(InstallState::Fresh),
        (true, true, true, true, true, true) => {
            if matches!(validation, StateValidation::Complete) {
                paired_configuration(&paths.config, paths)?;
                installed_firewall_configuration(paths)?;
                installed_helper_authority(paths)?;
            }
            let raw = fs::read(&state_path).map_err(|_| SetupError::ExistingInstall)?;
            match raw.as_slice() {
                b"unpaired-v1\n" => Ok(InstallState::ConfiguredUnpaired),
                b"recovering-v1\n" => Ok(InstallState::Recovering),
                b"paired-v1\n" => Ok(InstallState::Existing),
                _ => Err(SetupError::ExistingInstall),
            }
        }
        _ => Err(SetupError::ExistingInstall),
    }
}

pub(super) fn installed_helper_authority(paths: &InstallPaths) -> Result<Vec<u8>, SetupError> {
    let raw = fs::read(&paths.helper_authority).map_err(|_| SetupError::ExistingInstall)?;
    if raw.len() != 65 || raw.last() != Some(&b'\n') {
        return Err(SetupError::ExistingInstall);
    }
    let decoded = hex::decode(&raw[..64]).map_err(|_| SetupError::ExistingInstall)?;
    if !valid_helper_authority(&decoded) || hex::encode(&decoded).as_bytes() != &raw[..64] {
        return Err(SetupError::ExistingInstall);
    }
    Ok(decoded)
}

pub(super) fn valid_helper_authority(value: &[u8]) -> bool {
    value.len() == 32 && value.iter().any(|byte| *byte != 0)
}

pub(super) fn installed_firewall_configuration(
    paths: &InstallPaths,
) -> Result<FirewallConfig, SetupError> {
    let raw =
        fs::read_to_string(&paths.firewall_config).map_err(|_| SetupError::ExistingInstall)?;
    if raw.len() > 16 * 1024 {
        return Err(SetupError::ExistingInstall);
    }
    let mut values = BTreeMap::new();
    for line in raw.lines() {
        let (name, value) = line.split_once('=').ok_or(SetupError::ExistingInstall)?;
        if value.is_empty() && name != "VONK_HOST_ENDPOINT_PORTS" {
            return Err(SetupError::ExistingInstall);
        }
        if values.insert(name, value).is_some() {
            return Err(SetupError::ExistingInstall);
        }
    }
    let mut parse_ip = |name| {
        values
            .remove(name)
            .ok_or(SetupError::ExistingInstall)?
            .parse::<Ipv4Addr>()
            .map_err(|_| SetupError::ExistingInstall)
    };
    let parse_ports = |value: &str, required: bool| {
        if value.is_empty() {
            return if required {
                Err(SetupError::ExistingInstall)
            } else {
                Ok(Vec::new())
            };
        }
        value
            .split(',')
            .map(|value| {
                value
                    .parse::<u16>()
                    .map_err(|_| SetupError::ExistingInstall)
                    .and_then(|port| {
                        if port.to_string() == value {
                            Ok(port)
                        } else {
                            Err(SetupError::ExistingInstall)
                        }
                    })
            })
            .collect::<Result<Vec<_>, _>>()
    };
    let agent_config = paired_configuration(&paths.config, paths)?;
    let config = FirewallConfig {
        nas_management_ip: parse_ip("VONK_NAS_MANAGEMENT_IP")?,
        node_management_ip: parse_ip("VONK_NODE_MANAGEMENT_IP")?,
        node_fabric_ip: parse_ip("VONK_NODE_FABRIC_IP")?,
        peer_fabric_ip: parse_ip("VONK_PEER_FABRIC_IP")?,
        endpoint_host_ports: parse_ports(
            values
                .remove("VONK_ENDPOINT_HOST_PORTS")
                .ok_or(SetupError::ExistingInstall)?,
            true,
        )?,
        host_endpoint_ports: parse_ports(
            values
                .remove("VONK_HOST_ENDPOINT_PORTS")
                .ok_or(SetupError::ExistingInstall)?,
            false,
        )?,
        rendezvous_port: values
            .remove("VONK_RENDEZVOUS_PORT")
            .ok_or(SetupError::ExistingInstall)?
            .parse::<u16>()
            .map_err(|_| SetupError::ExistingInstall)?,
        fabric_bandwidth_mbps: agent_config.fabric_bandwidth_mbps,
    };
    if !values.is_empty()
        || !config.valid()
        || config.node_fabric_ip != agent_config.fabric_address
        || config.render() != raw
    {
        return Err(SetupError::ExistingInstall);
    }
    Ok(config)
}

pub(super) fn safe_existing_parent(
    path: &Path,
    required_owner: Option<u32>,
) -> Result<bool, SetupError> {
    let parent = path.parent().ok_or(SetupError::ExistingInstall)?;
    match fs::symlink_metadata(parent) {
        Ok(metadata)
            if metadata.file_type().is_dir()
                && !metadata.file_type().is_symlink()
                && required_owner.is_none_or(|owner| metadata.uid() == owner)
                && metadata.permissions().mode() & 0o022 == 0 =>
        {
            Ok(true)
        }
        Ok(_) => Err(SetupError::ExistingInstall),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(SetupError::ExistingInstall),
    }
}

pub(super) fn paired_configuration(
    path: &Path,
    paths: &InstallPaths,
) -> Result<WrittenConfig, SetupError> {
    let raw = fs::read(path).map_err(|_| SetupError::ExistingInstall)?;
    if raw.len() > 64 * 1024 {
        return Err(SetupError::ExistingInstall);
    }
    let config: WrittenConfig = toml::from_slice(&raw).map_err(|_| SetupError::ExistingInstall)?;
    if !valid_written_config(&config, paths) {
        return Err(SetupError::ExistingInstall);
    }
    Ok(config)
}

pub(super) fn safe_existing_file(
    path: &Path,
    required_owner: Option<u32>,
) -> Result<bool, SetupError> {
    match fs::symlink_metadata(path) {
        Ok(metadata)
            if metadata.file_type().is_file()
                && !metadata.file_type().is_symlink()
                && required_owner.is_none_or(|owner| metadata.uid() == owner)
                && metadata.permissions().mode() & 0o022 == 0 =>
        {
            Ok(true)
        }
        Ok(_) => Err(SetupError::ExistingInstall),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(SetupError::ExistingInstall),
    }
}

#[cfg(test)]
mod tests {
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

        assert!(matches!(
            install_state(&paths, StateValidation::MetadataOnly),
            Ok(InstallState::Existing)
        ));
        assert!(matches!(
            install_state(&paths, StateValidation::Complete),
            Err(SetupError::ExistingInstall)
        ));
    }
}
