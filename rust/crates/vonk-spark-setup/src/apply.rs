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
        envelope.release_manifest,
        envelope.release_signature,
        authority,
    )?;
    let state = install_state(paths, StateValidation::Complete)?;
    if !matches!(
        (&envelope.plan, state),
        (ApplyOperation::Fresh { .. }, InstallState::Fresh)
            | (
                ApplyOperation::Pair { .. },
                InstallState::ConfiguredUnpaired
            )
            | (
                ApplyOperation::Reenroll { .. },
                InstallState::Existing | InstallState::Recovering
            )
            | (ApplyOperation::Recover, InstallState::Recovering)
            | (ApplyOperation::Upgrade, InstallState::Existing)
    ) {
        return Err(SetupError::PrivilegedInput);
    }
    validate_plan_against_installation(&envelope.plan, paths)?;
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
            write_setup_state(paths, b"unpaired-v1\n", owner)?;
            pair_agent(
                paths,
                runner,
                &config.enrollment_url,
                &config.ca_sha256,
                pairing_token,
            )?;
            write_setup_state(paths, b"recovering-v1\n", owner)?;
            start_and_verify(paths, runner)?;
            write_setup_state(paths, b"paired-v1\n", owner)
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
            write_setup_state(paths, b"recovering-v1\n", owner)?;
            start_and_verify(paths, runner)?;
            write_setup_state(paths, b"paired-v1\n", owner)
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
            write_setup_state(paths, b"recovering-v1\n", owner)?;
            stop_agent_for_identity_reload(paths, runner)?;
            start_and_verify(paths, runner)?;
            write_setup_state(paths, b"paired-v1\n", owner)
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
            write_setup_state(paths, b"paired-v1\n", owner)
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

pub(super) fn validate_plan_against_installation(
    plan: &ApplyOperation,
    paths: &InstallPaths,
) -> Result<(), SetupError> {
    match plan {
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
            let ca = fs::read(&paths.ca).map_err(|_| SetupError::ExistingInstall)?;
            verify_ca(&ca, &config.ca_sha256)?;
            if &config.enrollment_url != enrollment_url || &config.ca_sha256 != ca_sha256 {
                return Err(SetupError::PrivilegedInput);
            }
            Ok(())
        }
        ApplyOperation::Recover | ApplyOperation::Upgrade => {
            let config = paired_configuration(&paths.config, paths)?;
            let ca = fs::read(&paths.ca).map_err(|_| SetupError::ExistingInstall)?;
            verify_ca(&ca, &config.ca_sha256)
        }
    }
}

pub(super) fn validate_apply_envelope(envelope: &ApplyEnvelope) -> Result<(), SetupError> {
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
