//! Apply.

use super::*;

pub fn apply_setup_from(
    input: impl Read,
    executable_path: &Path,
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    caller: CallerIdentity,
) -> Result<(), SetupError> {
    caller.authenticate_for(paths)?;
    caller.require_sudo_root(caller.sudo_uid.ok_or(SetupError::CallerPhase)?)?;
    validate_native_architecture(std::env::consts::ARCH)?;
    apply_setup_from_with_authority(
        input,
        executable_path,
        paths,
        runner,
        caller,
        &ReleaseAuthority::canonical(),
    )
}

pub fn apply_setup_from_with_authority(
    input: impl Read,
    executable_path: &Path,
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    caller: CallerIdentity,
    authority: &ReleaseAuthority,
) -> Result<(), SetupError> {
    caller.authenticate_for(paths)?;
    let envelope = decode_apply_frame(input)?;
    caller.require_sudo_root(envelope.caller_uid)?;
    validate_apply_envelope(&envelope)?;
    let release = verified_release(
        envelope.release_manifest.clone(),
        envelope.release_signature.clone(),
        authority,
    )?;
    let package_path = validated_staging_session(executable_path, paths)?;
    verify_release_artifact_size(executable_path, u64::from(release.setup.size))
        .map_err(|_| SetupError::PrivilegedInput)?;
    verify_regular_file_digest(executable_path, &release.setup.sha256, 64 * 1024 * 1024)
        .map_err(|_| SetupError::PrivilegedInput)?;
    verify_release_artifact_size(&package_path, u64::from(release.package.size))
        .map_err(|_| SetupError::PrivilegedInput)?;
    let staged = stage_verified_package_from(
        &package_path,
        &release.package.sha256,
        &release.version,
        &release.architecture,
        false,
    )?;
    let owner = paths
        .required_owner
        .unwrap_or_else(|| rustix::process::geteuid().as_raw());
    validate_plan_against_installation(&envelope, paths)?;
    for path in [
        &paths.agent,
        &paths.config,
        &paths.ca,
        &paths.helper_authority,
        &paths.firewall_config,
        &setup_state_path(paths),
    ] {
        isolate_damaged_generated_path(path, owner)?;
    }
    if !matches!(envelope.plan, ApplyOperation::Fresh { .. }) {
        let config = paired_configuration(&paths.config, paths)?;
        if let Some(encoded) = &envelope.repair_ca_pem {
            let ca = hex::decode(encoded).map_err(|_| SetupError::PrivilegedInput)?;
            atomic_root_write(&paths.ca, &ca, owner, 0o644)?;
        }
        if let Some(encoded) = &envelope.repair_helper_authority {
            let key = hex::decode(encoded).map_err(|_| SetupError::PrivilegedInput)?;
            if !valid_helper_authority(&key) {
                return Err(SetupError::PrivilegedInput);
            }
            let discovery = prepare::observe_enrollment(
                &config.enrollment_url,
                &config.ca_sha256,
                None,
                runner,
            )?;
            // The authenticated authority owns the current key. A stale
            // unprivileged observation never adds a second admission gate.
            let key = discovery.helper_authority;
            atomic_root_write(
                &paths.helper_authority,
                format!("{}\n", hex::encode(key)).as_bytes(),
                owner,
                0o644,
            )?;
        }
        if let Some(wire) = &envelope.repair_firewall {
            let firewall = FirewallConfig {
                nas_management_ip: plan::ipv4(wire.nas_management_ip)?,
                node_management_ip: plan::ipv4(wire.node_management_ip)?,
                node_fabric_ip: plan::ipv4(wire.node_fabric_ip)?,
                peer_fabric_ip: plan::ipv4(wire.peer_fabric_ip)?,
                endpoint_host_ports: wire.endpoint_host_ports.clone(),
                host_endpoint_ports: wire.host_endpoint_ports.clone(),
                rendezvous_port: wire.rendezvous_port,
                fabric_bandwidth_mbps: wire.fabric_bandwidth_mbps,
            };
            atomic_root_write(
                &paths.firewall_config,
                firewall.render().as_bytes(),
                owner,
                0o600,
            )?;
        }
    }
    match envelope.plan {
        ApplyOperation::Fresh {
            enrollment_url,
            controller_url,
            ca_sha256,
            ca_pem,
            node_id,
            pairing_token,
            host_mapping,
            firewall,
            helper_authority,
        } => {
            install_package(runner, &staged)?;
            let config = GeneratedConfig {
                enrollment_url: *enrollment_url,
                controller_url: *controller_url,
                ca_path: paths.ca.clone(),
                ca_sha256,
                node_id,
                fabric_address: firewall.node_fabric_ip,
                fabric_bandwidth_mbps: firewall.fabric_bandwidth_mbps,
            };
            install_configuration(paths, &config, &firewall, &helper_authority, &ca_pem, owner)?;
            if let Some(mapping) = host_mapping {
                install_host_mapping(paths, &mapping, owner)?;
            }
            write_setup_state(
                paths,
                format!("{}\n", InstallState::UnpairedV1).as_bytes(),
                owner,
            )?;
            pair_agent(
                paths,
                runner,
                &config.enrollment_url,
                &config.ca_sha256,
                pairing_token,
            )?;
            write_setup_state(
                paths,
                format!("{}\n", InstallState::RecoveringV1).as_bytes(),
                owner,
            )?;
            start_and_verify(paths, runner)?;
            write_setup_state(
                paths,
                format!("{}\n", InstallState::PairedV1).as_bytes(),
                owner,
            )
        }
        ApplyOperation::Pair {
            enrollment_url,
            ca_sha256,
            pairing_token,
        } => {
            let config = paired_configuration(&paths.config, paths)?;
            let ca = fs::read(&paths.ca).map_err(|_| SetupError::ExistingInstall)?;
            verify_ca(&ca, &config.ca_sha256)?;
            if config.enrollment_url != enrollment_url || config.ca_sha256 != ca_sha256 {
                return Err(SetupError::PrivilegedInput);
            }
            refresh_configuration(paths, &config, owner)?;
            ensure_package_installed(
                paths,
                runner,
                &staged,
                &release.version,
                &release.architecture,
            )?;
            pair_agent(
                paths,
                runner,
                &config.enrollment_url,
                &config.ca_sha256,
                pairing_token,
            )?;
            write_setup_state(
                paths,
                format!("{}\n", InstallState::RecoveringV1).as_bytes(),
                owner,
            )?;
            start_and_verify(paths, runner)?;
            write_setup_state(
                paths,
                format!("{}\n", InstallState::PairedV1).as_bytes(),
                owner,
            )
        }
        ApplyOperation::Reenroll {
            enrollment_url,
            ca_sha256,
            pairing_token,
        } => {
            let config = paired_configuration(&paths.config, paths)?;
            let ca = fs::read(&paths.ca).map_err(|_| SetupError::ExistingInstall)?;
            verify_ca(&ca, &config.ca_sha256)?;
            if config.enrollment_url != enrollment_url || config.ca_sha256 != ca_sha256 {
                return Err(SetupError::PrivilegedInput);
            }
            refresh_configuration(paths, &config, owner)?;
            ensure_package_installed(
                paths,
                runner,
                &staged,
                &release.version,
                &release.architecture,
            )?;
            pair_agent(paths, runner, &enrollment_url, &ca_sha256, pairing_token)?;
            write_setup_state(
                paths,
                format!("{}\n", InstallState::RecoveringV1).as_bytes(),
                owner,
            )?;
            stop_agent_for_identity_reload(paths, runner)?;
            start_and_verify(paths, runner)?;
            write_setup_state(
                paths,
                format!("{}\n", InstallState::PairedV1).as_bytes(),
                owner,
            )
        }
        ApplyOperation::Recover => {
            let config = paired_configuration(&paths.config, paths)?;
            let ca = fs::read(&paths.ca).map_err(|_| SetupError::ExistingInstall)?;
            verify_ca(&ca, &config.ca_sha256)?;
            refresh_configuration(paths, &config, owner)?;
            ensure_package_installed(
                paths,
                runner,
                &staged,
                &release.version,
                &release.architecture,
            )?;
            stop_agent_for_identity_reload(paths, runner)?;
            start_and_verify(paths, runner)?;
            write_setup_state(
                paths,
                format!("{}\n", InstallState::PairedV1).as_bytes(),
                owner,
            )
        }
        ApplyOperation::Upgrade => {
            let config = paired_configuration(&paths.config, paths)?;
            let ca = fs::read(&paths.ca).map_err(|_| SetupError::ExistingInstall)?;
            verify_ca(&ca, &config.ca_sha256)?;
            refresh_configuration(paths, &config, owner)?;
            upgrade_existing(
                paths,
                runner,
                &staged,
                &release.version,
                &release.architecture,
            )
        }
    }
}

/// Preserve unexpected managed objects without following or deleting their bytes.
fn isolate_damaged_generated_path(path: &Path, owner: u32) -> Result<(), SetupError> {
    let parent = path.parent().ok_or(SetupError::PrivilegedInput)?;
    installation::safe_existing_parent(path, Some(owner))?;
    match fs::symlink_metadata(path) {
        Ok(metadata)
            if metadata.is_file()
                && !metadata.file_type().is_symlink()
                && metadata.uid() == owner
                && metadata.permissions().mode() & 0o022 == 0 =>
        {
            Ok(())
        }
        Ok(_) => fs::rename(
            path,
            parent.join(format!(".vonk-spark-retired-{}", Uuid::new_v4().simple())),
        )
        .map_err(SetupError::PrivilegedWrite),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(SetupError::PrivilegedWrite(error)),
    }
}

pub(super) fn validate_plan_against_installation(
    envelope: &ApplyEnvelope,
    paths: &InstallPaths,
) -> Result<(), SetupError> {
    let observed_ca = || match &envelope.repair_ca_pem {
        Some(encoded) => hex::decode(encoded).map_err(|_| SetupError::PrivilegedInput),
        None => fs::read(&paths.ca).map_err(|_| SetupError::ExistingInstall),
    };
    if let Some(wire) = &envelope.repair_firewall {
        let config = paired_configuration(&paths.config, paths)?;
        if plan::ipv4(wire.node_fabric_ip)? != config.fabric_address {
            return Err(SetupError::PrivilegedInput);
        }
    }
    match &envelope.plan {
        ApplyOperation::Fresh { .. } => Ok(()),
        ApplyOperation::Pair {
            enrollment_url,
            ca_sha256,
            ..
        }
        | ApplyOperation::Reenroll {
            enrollment_url,
            ca_sha256,
            ..
        } => {
            let config = paired_configuration(&paths.config, paths)?;
            let ca = observed_ca()?;
            verify_ca(&ca, &config.ca_sha256)?;
            if &config.enrollment_url != enrollment_url || &config.ca_sha256 != ca_sha256 {
                return Err(SetupError::PrivilegedInput);
            }
            Ok(())
        }
        ApplyOperation::Recover | ApplyOperation::Upgrade => {
            let config = paired_configuration(&paths.config, paths)?;
            let ca = observed_ca()?;
            verify_ca(&ca, &config.ca_sha256)
        }
    }
}

pub(super) fn validate_apply_envelope(envelope: &ApplyEnvelope) -> Result<(), SetupError> {
    if envelope
        .repair_helper_authority
        .as_ref()
        .is_some_and(|encoded| {
            hex::decode(encoded).map_or(true, |key| !valid_helper_authority(&key))
        })
        || envelope
            .repair_ca_pem
            .as_ref()
            .is_some_and(|encoded| hex::decode(encoded).is_err())
    {
        return Err(SetupError::PrivilegedInput);
    }
    if let Some(wire) = &envelope.repair_firewall {
        let firewall = FirewallConfig {
            nas_management_ip: plan::ipv4(wire.nas_management_ip)?,
            node_management_ip: plan::ipv4(wire.node_management_ip)?,
            node_fabric_ip: plan::ipv4(wire.node_fabric_ip)?,
            peer_fabric_ip: plan::ipv4(wire.peer_fabric_ip)?,
            endpoint_host_ports: wire.endpoint_host_ports.clone(),
            host_endpoint_ports: wire.host_endpoint_ports.clone(),
            rendezvous_port: wire.rendezvous_port,
            fabric_bandwidth_mbps: wire.fabric_bandwidth_mbps,
        };
        if !firewall.valid() {
            return Err(SetupError::PrivilegedInput);
        }
    }
    if envelope.schema_version != 1
        || envelope.caller_uid == 0
        || envelope.release_manifest.is_empty()
        || envelope.release_manifest.len() > MAX_RELEASE_BYTES
        || envelope.release_signature.is_empty()
        || envelope.release_signature.len() > MAX_RELEASE_SIGNATURE_BYTES
    {
        return Err(SetupError::PrivilegedInput);
    }
    match &envelope.plan {
        ApplyOperation::Fresh {
            enrollment_url,
            controller_url,
            ca_sha256,
            ca_pem,
            node_id,
            pairing_token,
            host_mapping,
            firewall,
            helper_authority,
        } => {
            if !valid_origin(enrollment_url)
                || !valid_origin(controller_url)
                || !valid_sha256(ca_sha256)
                || !valid_node_id(node_id)
                || !valid_token(pairing_token)
                || !valid_host_mapping(host_mapping.as_ref())
                || !firewall.valid()
                || !valid_helper_authority(helper_authority)
            {
                return Err(SetupError::PrivilegedInput);
            }
            verify_ca(ca_pem, ca_sha256).map_err(|_| SetupError::PrivilegedInput)
        }
        ApplyOperation::Pair {
            enrollment_url,
            ca_sha256,
            pairing_token,
        }
        | ApplyOperation::Reenroll {
            enrollment_url,
            ca_sha256,
            pairing_token,
        } => {
            if !valid_origin(enrollment_url)
                || !valid_sha256(ca_sha256)
                || !valid_token(pairing_token)
            {
                return Err(SetupError::PrivilegedInput);
            }
            Ok(())
        }
        ApplyOperation::Recover | ApplyOperation::Upgrade => Ok(()),
    }
}
