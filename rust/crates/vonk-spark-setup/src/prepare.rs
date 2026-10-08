//! Prepare.

use super::*;
use vonk_agent_protocol::generated::{
    InstallerPackageArtifact, InstallerReleaseManifest, InstallerReleaseObject as ReleaseArtifact,
    SparkFirewallConfig,
};

#[derive(Debug, Clone)]
pub struct InstallPaths {
    pub config: PathBuf,
    pub ca: PathBuf,
    pub firewall_config: PathBuf,
    pub helper_authority: PathBuf,
    pub hosts: PathBuf,
    pub agent: PathBuf,
    pub staging_root: PathBuf,
    pub sudo: PathBuf,
    pub service: String,
    pub required_owner: Option<u32>,
}

impl InstallPaths {
    pub fn system() -> Self {
        Self {
            config: PathBuf::from(CONFIG_PATH),
            ca: PathBuf::from(CA_PATH),
            firewall_config: PathBuf::from(FIREWALL_CONFIG_PATH),
            helper_authority: PathBuf::from(HELPER_AUTHORITY_PATH),
            hosts: PathBuf::from(HOSTS_PATH),
            agent: PathBuf::from(AGENT_PATH),
            staging_root: PathBuf::from("/var/tmp"),
            sudo: PathBuf::from("/usr/bin/sudo"),
            service: SERVICE.to_owned(),
            required_owner: Some(0),
        }
    }
}

#[derive(Debug, Error)]
pub enum SetupError {
    #[error("setup input is unsafe: {0}")]
    UnsafeInput(&'static str),
    #[error("setup package is unsafe")]
    UnsafePackage,
    #[error("setup package digest does not match the release")]
    PackageDigest,
    #[error("setup package is not a Debian package")]
    PackageFormat,
    #[error("setup package identity does not match the selected release")]
    PackageIdentity,
    #[error("immutable installer release signature or claims are invalid")]
    ReleaseSignature,
    #[error(
        "Spark installation requires Debian or Ubuntu with systemd on the selected architecture"
    )]
    UnsupportedHost,
    #[error("existing installation observation is unavailable")]
    ExistingInstall,
    #[error("{0}")]
    ObservationUnavailable(vonk_agent_protocol::generated::WaitReason),
    #[error("interactive setup failed")]
    Prompt,
    #[error("controller CA is invalid or does not match its supplied SHA-256")]
    ControllerCa,
    #[error("controller CA authority differs: pinned {stored}, observed {advertised}")]
    ControllerCaChanged { stored: String, advertised: String },
    #[error(
        "enrollment bootstrap is invalid or does not match the supplied endpoint and CA SHA-256"
    )]
    EnrollmentBootstrap,
    #[error("setup command failed: {0}")]
    Command(String),
    #[error("privileged configuration input is invalid")]
    PrivilegedInput,
    #[error("setup was invoked from the wrong privilege phase")]
    CallerPhase,
    #[error("setup I/O failed")]
    PrivilegedWrite(#[source] io::Error),
}

pub(super) fn observe_enrollment(
    enrollment_url: &Url,
    ca_sha256: &str,
    controller_address: Option<Ipv4Addr>,
    runner: &mut dyn CommandRunner,
) -> Result<EnrollmentDiscovery, SetupError> {
    for attempt in 0..3 {
        match discover_enrollment(enrollment_url, ca_sha256, controller_address, runner) {
            Ok(discovery) => return Ok(discovery),
            Err(error @ (SetupError::ControllerCa | SetupError::UnsafeInput(_))) => {
                return Err(error);
            }
            Err(_) if attempt < 2 => std::thread::sleep(std::time::Duration::from_millis(100)),
            Err(_) => break,
        }
    }
    Err(SetupError::ObservationUnavailable(
        vonk_agent_protocol::generated::WaitReason::ObservationUnavailable,
    ))
}

pub fn validate_system_host(_request: &SetupRequest) -> Result<(), SetupError> {
    let os_release =
        fs::read_to_string("/etc/os-release").map_err(|_| SetupError::UnsupportedHost)?;
    let architecture = match std::env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        _ => return Err(SetupError::UnsupportedHost),
    };
    validate_host_description(
        &os_release,
        Path::new("/run/systemd/system").is_dir(),
        architecture,
        "arm64",
    )?;
    for executable in [
        "/bin/rm",
        "/bin/sh",
        "/usr/bin/apt-get",
        "/usr/bin/cat",
        "/usr/bin/curl",
        "/usr/bin/dpkg-deb",
        "/usr/bin/dpkg-query",
        "/usr/bin/install",
        "/usr/bin/mktemp",
        "/usr/bin/openssl",
        "/usr/bin/setpriv",
        "/usr/bin/sha256sum",
        "/usr/bin/stat",
        "/usr/bin/sudo",
        "/usr/bin/systemctl",
    ] {
        let metadata = fs::metadata(executable).map_err(|_| SetupError::UnsupportedHost)?;
        if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
            return Err(SetupError::UnsupportedHost);
        }
    }
    Ok(())
}

pub(super) fn validate_host_description(
    os_release: &str,
    systemd_present: bool,
    architecture: &str,
    expected_architecture: &str,
) -> Result<(), SetupError> {
    let distribution = os_release
        .lines()
        .find_map(|line| line.strip_prefix("ID="))
        .map(|value| value.trim_matches('"'));
    if !matches!(distribution, Some("debian" | "ubuntu"))
        || !systemd_present
        || architecture != expected_architecture
    {
        return Err(SetupError::UnsupportedHost);
    }
    Ok(())
}

pub(super) fn validate_native_architecture(architecture: &str) -> Result<(), SetupError> {
    if architecture != "aarch64" {
        return Err(SetupError::UnsupportedHost);
    }
    Ok(())
}

pub fn prepare_setup(
    request: &SetupRequest,
    paths: &InstallPaths,
    prompt: &mut dyn Prompt,
    runner: &mut dyn CommandRunner,
    caller: CallerIdentity,
) -> Result<PreparedSetup, SetupError> {
    validate_native_architecture(std::env::consts::ARCH)?;
    prepare_setup_with_authority(
        request,
        paths,
        prompt,
        runner,
        caller,
        &ReleaseAuthority::canonical(),
    )
}

pub fn prepare_setup_with_authority(
    request: &SetupRequest,
    paths: &InstallPaths,
    prompt: &mut dyn Prompt,
    runner: &mut dyn CommandRunner,
    caller: CallerIdentity,
    authority: &ReleaseAuthority,
) -> Result<PreparedSetup, SetupError> {
    caller.authenticate_for(paths)?;
    let caller_uid = caller.require_unprivileged()?;
    let release = verified_release_from_files(request, authority)?;
    verify_release_artifact_size(&request.executable, u64::from(release.setup.size))?;
    verify_regular_file_digest(&request.executable, &release.setup.sha256, 64 * 1024 * 1024)?;
    verify_release_artifact_size(&request.package, u64::from(release.package.size))?;
    let staged = stage_verified_package_from(
        &request.package,
        &release.package.sha256,
        &release.version,
        &release.architecture,
        true,
    )?;
    let plan = match (install_state(paths)?, request.enroll) {
        (InstallState::Fresh, _) => {
            let enrollment_url = match &request.enrollment_url {
                Some(url) => url.clone(),
                None => required_origin(prompt, "Enrollment URL")?,
            };
            let ca_sha256 = match &request.ca_sha256 {
                Some(value) => value.clone(),
                None => required_sha256(prompt, "Controller CA SHA-256")?,
            };
            let pairing_token = prompt
                .secret("Pairing token")
                .map_err(|_| SetupError::Prompt)?;
            if !valid_token(&pairing_token) {
                return Err(SetupError::UnsafeInput("pairing token"));
            }
            let discovery = observe_enrollment(
                &enrollment_url,
                &ca_sha256,
                request.controller_address,
                runner,
            )?;
            let firewall = FirewallConfig::collect(
                &request.firewall_inputs,
                request.controller_address,
                prompt,
                runner,
            )?;
            ApplyOperation::Fresh {
                enrollment_url: Box::new(enrollment_url),
                controller_url: Box::new(discovery.controller_url),
                ca_sha256,
                ca_pem: discovery.ca_pem,
                node_id: format!("spk_{}", Uuid::new_v4().simple()),
                pairing_token,
                host_mapping: discovery.host_mapping,
                firewall,
                helper_authority: discovery.helper_authority,
            }
        }
        (InstallState::UnpairedV1, _) => {
            let config = paired_configuration(&paths.config, paths)?;
            let pairing_token = prompt
                .secret("Pairing token")
                .map_err(|_| SetupError::Prompt)?;
            if !valid_token(&pairing_token) {
                return Err(SetupError::UnsafeInput("pairing token"));
            }
            ApplyOperation::Pair {
                enrollment_url: config.enrollment_url,
                ca_sha256: config.ca_sha256,
                pairing_token,
            }
        }
        (InstallState::PairedV1, false) => ApplyOperation::Upgrade,
        // An explicit grant replaces the identity even when an earlier setup
        // stopped during readiness. That identity may no longer exist after
        // Controller recovery; retrying it cannot satisfy the new enrollment.
        (InstallState::PairedV1 | InstallState::RecoveringV1, true) => {
            let config = paired_configuration(&paths.config, paths)?;
            // Fail closed before prompting for a grant when the controller no
            // longer advertises the CA this Spark pinned.
            verify_reenroll_controller_ca(&config, request.controller_address, runner)?;
            let pairing_token = prompt
                .secret("Pairing token")
                .map_err(|_| SetupError::Prompt)?;
            if !valid_token(&pairing_token) {
                return Err(SetupError::UnsafeInput("pairing token"));
            }
            ApplyOperation::Reenroll {
                enrollment_url: config.enrollment_url,
                ca_sha256: config.ca_sha256,
                pairing_token,
            }
        }
        (InstallState::RecoveringV1, false) => ApplyOperation::Recover,
    };
    let mut repair_firewall = None;
    let mut repair_ca_pem = None;
    let mut repair_helper_authority = None;
    if !matches!(plan, ApplyOperation::Fresh { .. }) {
        let config = paired_configuration(&paths.config, paths)?;
        if !safe_existing_file(&paths.firewall_config, paths.required_owner).unwrap_or(false)
            || installed_firewall_configuration(paths).is_err()
        {
            let firewall = FirewallConfig::collect(
                &request.firewall_inputs,
                request.controller_address,
                prompt,
                runner,
            )?;
            if firewall.node_fabric_ip != config.fabric_address {
                return Err(SetupError::UnsafeInput("Spark fabric address"));
            }
            repair_firewall = Some(SparkFirewallConfig {
                nas_management_ip: firewall.nas_management_ip.into(),
                node_management_ip: firewall.node_management_ip.into(),
                node_fabric_ip: firewall.node_fabric_ip.into(),
                peer_fabric_ip: firewall.peer_fabric_ip.into(),
                endpoint_host_ports: firewall.endpoint_host_ports,
                host_endpoint_ports: firewall.host_endpoint_ports,
                rendezvous_port: firewall.rendezvous_port,
                fabric_bandwidth_mbps: firewall.fabric_bandwidth_mbps,
            });
        }
        if !safe_existing_file(&paths.ca, paths.required_owner).unwrap_or(false)
            || !safe_existing_file(&paths.helper_authority, paths.required_owner).unwrap_or(false)
            || !fs::read(&paths.ca).is_ok_and(|ca| verify_ca(&ca, &config.ca_sha256).is_ok())
            || installed_helper_authority(paths).is_err()
        {
            // Re-observe through the pinned CA ingress, never invent authority.
            let discovery = observe_enrollment(
                &config.enrollment_url,
                &config.ca_sha256,
                request.controller_address,
                runner,
            )?;
            repair_ca_pem = Some(hex::encode(discovery.ca_pem));
            repair_helper_authority = Some(hex::encode(discovery.helper_authority));
        }
    }
    let envelope = ApplyEnvelope {
        schema_version: 1,
        caller_uid,
        release_manifest: release.raw,
        release_signature: release.signature,
        plan,
        repair_firewall,
        repair_ca_pem,
        repair_helper_authority,
    };
    Ok(PreparedSetup {
        executable: request.executable.clone(),
        setup_signature: request.setup_signature.clone(),
        sudo: paths.sudo.clone(),
        staging_root: paths.staging_root.clone(),
        required_owner: paths.required_owner,
        staged,
        frame: encode_apply_frame(&envelope)?,
    })
}

pub(super) fn encode_apply_frame(envelope: &ApplyEnvelope) -> Result<Vec<u8>, SetupError> {
    let payload =
        serde_json::to_vec(&envelope.to_wire()).map_err(|_| SetupError::PrivilegedInput)?;
    if payload.is_empty() || payload.len() > MAX_APPLY_FRAME_BYTES {
        return Err(SetupError::PrivilegedInput);
    }
    let mut frame = Vec::with_capacity(APPLY_FRAME_MAGIC.len() + 4 + payload.len() + 32);
    frame.extend_from_slice(APPLY_FRAME_MAGIC);
    frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    frame.extend_from_slice(&payload);
    frame.extend_from_slice(&Sha256::digest(&payload));
    Ok(frame)
}

pub(super) fn decode_apply_frame(input: impl Read) -> Result<ApplyEnvelope, SetupError> {
    let maximum = APPLY_FRAME_MAGIC.len() + 4 + MAX_APPLY_FRAME_BYTES + 32;
    let mut raw = Vec::new();
    input
        .take((maximum + 1) as u64)
        .read_to_end(&mut raw)
        .map_err(SetupError::PrivilegedWrite)?;
    if raw.len() < APPLY_FRAME_MAGIC.len() + 4 + 32
        || raw.len() > maximum
        || !raw.starts_with(APPLY_FRAME_MAGIC)
    {
        return Err(SetupError::PrivilegedInput);
    }
    let length_offset = APPLY_FRAME_MAGIC.len();
    let payload_offset = length_offset + 4;
    let payload_length = u32::from_be_bytes(
        raw[length_offset..payload_offset]
            .try_into()
            .map_err(|_| SetupError::PrivilegedInput)?,
    ) as usize;
    if payload_length == 0 || payload_length > MAX_APPLY_FRAME_BYTES {
        return Err(SetupError::PrivilegedInput);
    }
    let digest_offset = payload_offset
        .checked_add(payload_length)
        .ok_or(SetupError::PrivilegedInput)?;
    if digest_offset + 32 != raw.len() {
        return Err(SetupError::PrivilegedInput);
    }
    let payload = &raw[payload_offset..digest_offset];
    if raw[digest_offset..] != Sha256::digest(payload)[..] {
        return Err(SetupError::PrivilegedInput);
    }
    let wire: SparkApplyEnvelope =
        serde_json::from_slice(payload).map_err(|_| SetupError::PrivilegedInput)?;
    ApplyEnvelope::from_wire(wire)
}

pub(super) fn read_bounded_regular(path: &Path, maximum: usize) -> Result<Vec<u8>, SetupError> {
    let before = fs::symlink_metadata(path).map_err(|_| SetupError::ReleaseSignature)?;
    if !before.file_type().is_file()
        || before.file_type().is_symlink()
        || before.nlink() != 1
        || before.len() == 0
        || before.len() > maximum as u64
        || before.permissions().mode() & 0o022 != 0
    {
        return Err(SetupError::ReleaseSignature);
    }
    let mut input = OpenOptions::new()
        .read(true)
        .custom_flags(libc_nofollow())
        .open(path)
        .map_err(|_| SetupError::ReleaseSignature)?;
    let open = input.metadata().map_err(|_| SetupError::ReleaseSignature)?;
    if !same_file(&before, &open) {
        return Err(SetupError::ReleaseSignature);
    }
    let mut raw = Vec::with_capacity(open.len() as usize);
    Read::by_ref(&mut input)
        .take((maximum + 1) as u64)
        .read_to_end(&mut raw)
        .map_err(|_| SetupError::ReleaseSignature)?;
    let after = input.metadata().map_err(|_| SetupError::ReleaseSignature)?;
    if raw.is_empty() || raw.len() > maximum || !same_file(&open, &after) {
        return Err(SetupError::ReleaseSignature);
    }
    Ok(raw)
}

pub(super) fn verified_release_from_files(
    request: &SetupRequest,
    authority: &ReleaseAuthority,
) -> Result<VerifiedRelease, SetupError> {
    let manifest = read_bounded_regular(&request.release_manifest, MAX_RELEASE_BYTES)?;
    let signature = read_bounded_regular(&request.release_signature, MAX_RELEASE_SIGNATURE_BYTES)?;
    let release = verified_release(manifest, signature, authority)?;
    let setup_signature =
        read_bounded_regular(&request.setup_signature, MAX_RELEASE_SIGNATURE_BYTES)?;
    verify_release_artifact_size(
        &request.setup_signature,
        u64::from(release.setup_signature.size),
    )?;
    verify_regular_file_digest(
        &request.setup_signature,
        &release.setup_signature.sha256,
        MAX_RELEASE_SIGNATURE_BYTES as u64,
    )
    .map_err(|_| SetupError::ReleaseSignature)?;
    let setup = read_bounded_regular(&request.executable, 64 * 1024 * 1024)?;
    authority.verify_setup(&setup, &setup_signature)?;
    Ok(release)
}

pub(super) fn verified_release(
    raw: Vec<u8>,
    signature: Vec<u8>,
    authority: &ReleaseAuthority,
) -> Result<VerifiedRelease, SetupError> {
    let document = authority.verify_manifest(&raw, &signature)?;
    // The canonical publication graph declares its target independently of
    // the reader host; actual system entry points enforce native execution.
    let (platform, architecture) = ("linux-arm64", "arm64");
    let (package, setup, setup_signature, channel, generation, version, acceptance_only) =
        match document {
            InstallerReleaseManifest::CandidateRelease(document) => {
                if !valid_package_release_identity(
                    &document.artifacts.agent_package_linux_arm64,
                    platform,
                    &document.version,
                ) {
                    return Err(SetupError::ReleaseSignature);
                }
                (
                    ReleaseArtifact::from(&document.artifacts.agent_package_linux_arm64),
                    document.artifacts.spark_setup_linux_arm64,
                    document.artifacts.spark_setup_signature_linux_arm64,
                    document.channel.to_string(),
                    document.generation,
                    document.version,
                    false,
                )
            }
            InstallerReleaseManifest::AcceptanceBaselineRelease(document) => (
                document.artifacts.agent_package_linux_arm64,
                document.artifacts.spark_setup_linux_arm64,
                document.artifacts.spark_setup_signature_linux_arm64,
                document.channel.to_string(),
                document.generation,
                document.version,
                true,
            ),
        };
    let prefix = release_artifact_prefix(&channel, &generation, platform, acceptance_only);
    if package.path != format!("{prefix}vonk-forge-agent.deb")
        || setup.path != format!("{prefix}vonk-spark-setup")
        || setup_signature.path != format!("{prefix}vonk-spark-setup.sig")
        || !valid_sha256(&package.sha256)
        || !valid_sha256(&setup.sha256)
        || !valid_sha256(&setup_signature.sha256)
        || package.size < 68
        || u64::from(package.size) > MAX_PACKAGE_BYTES
        || setup.size == 0
        || setup.size > 64 * 1024 * 1024
        || setup_signature.size == 0
        || setup_signature.size as usize > MAX_RELEASE_SIGNATURE_BYTES
    {
        return Err(SetupError::ReleaseSignature);
    }
    Ok(VerifiedRelease {
        raw,
        signature,
        package,
        setup,
        setup_signature,
        version,
        architecture: architecture.to_owned(),
    })
}

pub(super) fn valid_package_release_identity(
    artifact: &InstallerPackageArtifact,
    platform: &str,
    version: &str,
) -> bool {
    artifact.architecture == platform
        && artifact.package_version == version
        && valid_package_version(&artifact.package_version)
}

pub(super) fn release_artifact_prefix(
    channel: &str,
    generation: &str,
    platform: &str,
    acceptance_only: bool,
) -> String {
    let baseline = if acceptance_only {
        "acceptance-baseline/"
    } else {
        ""
    };
    format!("artifacts/{channel}/releases/{generation}/{baseline}spark/current/{platform}/")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_validation_rejects_non_debian_and_missing_systemd_before_setup() {
        assert!(matches!(
            validate_host_description("ID=fedora\n", true, "amd64", "amd64"),
            Err(SetupError::UnsupportedHost)
        ));
        assert!(matches!(
            validate_host_description("ID=debian\n", false, "amd64", "amd64"),
            Err(SetupError::UnsupportedHost)
        ));
    }

    #[test]
    fn host_validation_rejects_a_release_for_another_architecture() {
        assert!(matches!(
            validate_native_architecture("x86_64"),
            Err(SetupError::UnsupportedHost)
        ));
        assert!(validate_native_architecture("aarch64").is_ok());
        assert!(matches!(
            validate_host_description("ID=ubuntu\n", true, "amd64", "arm64"),
            Err(SetupError::UnsupportedHost)
        ));
    }

    #[test]
    fn acceptance_only_release_resolves_only_its_immutable_baseline_graph() {
        assert_eq!(
            release_artifact_prefix("dev", &"a".repeat(64), "linux-arm64", true,),
            format!(
                "artifacts/dev/releases/{}/acceptance-baseline/spark/current/linux-arm64/",
                "a".repeat(64)
            )
        );
        assert_eq!(
            release_artifact_prefix("stable", &"b".repeat(64), "linux-amd64", false,),
            format!(
                "artifacts/stable/releases/{}/spark/current/linux-amd64/",
                "b".repeat(64)
            )
        );
    }

    #[test]
    fn package_release_identity_is_bound_to_platform_and_version() {
        let artifact: InstallerPackageArtifact = serde_json::from_value(serde_json::json!({
            "architecture": "linux-arm64", "host_signature": "a".repeat(128),
            "package_version": "1.0.0", "path": "immutable", "sha256": "b".repeat(64),
            "size": 1, "target_binary_digest": "c".repeat(64),
            "target_build_digest": format!("sha256:{}", "d".repeat(64)),
        }))
        .unwrap();
        assert!(valid_package_release_identity(
            &artifact,
            "linux-arm64",
            "1.0.0"
        ));
        assert!(!valid_package_release_identity(
            &artifact,
            "linux-amd64",
            "1.0.0"
        ));
        assert!(!valid_package_release_identity(
            &artifact,
            "linux-arm64",
            "1.0.1"
        ));
    }
}
