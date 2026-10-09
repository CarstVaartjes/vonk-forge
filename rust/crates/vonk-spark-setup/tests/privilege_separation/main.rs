#[path = "../common/mod.rs"]
mod common;

use std::{
    collections::VecDeque,
    fs,
    io::Write,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    process::{Command as ProcessCommand, Stdio},
};

use sha2::{Digest, Sha256};
use tempfile::tempdir;
use vonk_spark_setup::{
    CallerIdentity, Command, CommandOutput, CommandRunner, CommandStderr, InstallPaths, Prompt,
    ReleaseAuthority, SetupError, SetupRequest, TtyPrompt, apply_setup_from_with_authority,
    handoff_to_root_with_authority, prepare_setup_with_authority,
};

const TOKEN: &str = "A123456789012345678901234567890123456789012";

#[derive(Clone, Copy)]
struct NativeReleaseIdentity {
    platform: &'static str,
    architecture: &'static str,
    wrong_architecture: &'static str,
}

fn native_release_identity() -> NativeReleaseIdentity {
    NativeReleaseIdentity {
        platform: "linux-arm64",
        architecture: "arm64",
        wrong_architecture: "amd64",
    }
}

fn package_filename() -> String {
    format!(
        "vonk-forge-agent_1.0.0_{}.deb",
        native_release_identity().architecture
    )
}

struct SignedRelease {
    authority: ReleaseAuthority,
    manifest: PathBuf,
    signature: PathBuf,
    setup_signature: PathBuf,
}

fn signed_release(
    root: &std::path::Path,
    package: &std::path::Path,
    executable: &std::path::Path,
) -> SignedRelease {
    let identity = native_release_identity();
    let agent_artifact = format!("agent-package-{}", identity.platform);
    let setup_artifact = format!("spark-setup-{}", identity.platform);
    let setup_signature_artifact = format!("spark-setup-signature-{}", identity.platform);
    let private_key = root.join("installer-release-private.pem");
    let public_key = root.join("installer-release-public.pem");
    assert!(
        ProcessCommand::new("/usr/bin/openssl")
            .args([
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
            ])
            .arg(&private_key)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .unwrap()
            .success()
    );
    assert!(
        ProcessCommand::new("/usr/bin/openssl")
            .args(["pkey", "-in"])
            .arg(&private_key)
            .args(["-pubout", "-out"])
            .arg(&public_key)
            .status()
            .unwrap()
            .success()
    );
    let manifest = root.join("release.json");
    let package_raw = fs::read(package).unwrap();
    let setup_raw = fs::read(executable).unwrap();
    let setup_raw_signature = root.join("vonk-spark-setup.raw.sig");
    assert!(
        ProcessCommand::new("/usr/bin/openssl")
            .args(["dgst", "-sha256", "-sign"])
            .arg(&private_key)
            .args(["-out"])
            .arg(&setup_raw_signature)
            .arg(executable)
            .status()
            .unwrap()
            .success()
    );
    let setup_signature = root.join("vonk-spark-setup.sig");
    assert!(
        ProcessCommand::new("/usr/bin/openssl")
            .args(["base64", "-A", "-in"])
            .arg(&setup_raw_signature)
            .args(["-out"])
            .arg(&setup_signature)
            .status()
            .unwrap()
            .success()
    );
    fs::OpenOptions::new()
        .append(true)
        .open(&setup_signature)
        .unwrap()
        .write_all(b"\n")
        .unwrap();
    let setup_signature_raw = fs::read(&setup_signature).unwrap();
    let release = common::candidate_release(serde_json::json!({
        "artifacts": {
            (agent_artifact): {
                "architecture": identity.platform,
                "host_signature": "f".repeat(128),
                "package_version": "1.0.0",
                "path": format!("relocated/{}/vonk-forge-agent.deb", identity.platform),
                "sha256": hex::encode(Sha256::digest(&package_raw)),
                "size": package_raw.len(),
                "target_binary_digest": "1".repeat(64),
                "target_build_digest": format!("sha256:{}", "2".repeat(64)),
            },
            (setup_artifact): {
                "path": format!("relocated/{}/vonk-spark-setup", identity.platform),
                "sha256": hex::encode(Sha256::digest(&setup_raw)),
                "size": setup_raw.len(),
            },
            (setup_signature_artifact): {
                "path": format!("relocated/{}/vonk-spark-setup.sig", identity.platform),
                "sha256": hex::encode(Sha256::digest(&setup_signature_raw)),
                "size": setup_signature_raw.len(),
            }
        },
        "bootstraps": {
            "nas": {
                "path": "artifacts/stable/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bootstraps/nas",
                "sha256": "c".repeat(64),
                "size": 1,
            },
            "spark": {
                "path": "artifacts/stable/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bootstraps/spark",
                "sha256": "d".repeat(64),
                "size": 1,
            }
        },
        "channel": "stable",
        "generation": "a".repeat(64),
        "images": {"api": format!("example.test/api@sha256:{}", "e".repeat(64))},
        "schema_version": 2,
        "source_sha": "b".repeat(40),
        "version": "1.0.0",
    }));
    fs::write(
        &manifest,
        format!("{}\n", serde_json::to_string(&release).unwrap()),
    )
    .unwrap();
    let raw_signature = root.join("release.raw.sig");
    assert!(
        ProcessCommand::new("/usr/bin/openssl")
            .args(["dgst", "-sha256", "-sign"])
            .arg(&private_key)
            .args(["-out"])
            .arg(&raw_signature)
            .arg(&manifest)
            .status()
            .unwrap()
            .success()
    );
    let signature = root.join("release.sig");
    assert!(
        ProcessCommand::new("/usr/bin/openssl")
            .args(["base64", "-A", "-in"])
            .arg(&raw_signature)
            .args(["-out"])
            .arg(&signature)
            .status()
            .unwrap()
            .success()
    );
    fs::OpenOptions::new()
        .append(true)
        .open(&signature)
        .unwrap()
        .write_all(b"\n")
        .unwrap();
    SignedRelease {
        authority: ReleaseAuthority::from_pem(fs::read(public_key).unwrap()).unwrap(),
        manifest,
        signature,
        setup_signature,
    }
}

#[derive(Default)]
struct RecordingRunner {
    commands: Vec<Command>,
    outputs: VecDeque<CommandOutput>,
    fail_reset_failed: bool,
    installed_identity: bool,
}

impl CommandRunner for RecordingRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
        let default = if self.installed_identity
            && command.program == std::path::Path::new("/usr/bin/dpkg-query")
        {
            CommandOutput::success(b"ii |1.0.0|arm64".to_vec())
        } else if self.fail_reset_failed
            && command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args.first().map(String::as_str) == Some("reset-failed")
        {
            CommandOutput {
                success: false,
                stdout: Vec::new(),
                stderr: Vec::new(),
            }
        } else if command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args.first().map(String::as_str) == Some("show")
        {
            CommandOutput::success(b"4242\n".to_vec())
        } else {
            CommandOutput::success_empty()
        };
        self.commands.push(command);
        Ok(self.outputs.pop_front().unwrap_or(default))
    }

    fn authenticate_sudo(&mut self, _sudo: &std::path::Path) -> Result<(), SetupError> {
        Ok(())
    }

    fn sleep(&mut self, _duration: std::time::Duration) {}
}

#[derive(Default)]
struct FailingReadinessRunner {
    commands: Vec<Command>,
}

impl CommandRunner for FailingReadinessRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
        let output = if command
            .args
            .iter()
            .any(|argument| argument == "verify-readiness")
        {
            CommandOutput {
                success: false,
                stdout: Vec::new(),
                stderr: Vec::new(),
            }
        } else if command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args.first().map(String::as_str) == Some("show")
        {
            CommandOutput::success(b"4242\n".to_vec())
        } else {
            CommandOutput::success_empty()
        };
        self.commands.push(command);
        Ok(output)
    }

    fn authenticate_sudo(&mut self, _sudo: &std::path::Path) -> Result<(), SetupError> {
        Ok(())
    }

    fn sleep(&mut self, _duration: std::time::Duration) {}
}

struct FreshAnswers {
    values: VecDeque<String>,
}

struct TokenOnlyPrompt {
    secrets: usize,
}

impl Prompt for TokenOnlyPrompt {
    fn value(&mut self, _label: &str) -> Result<String, String> {
        panic!("configured installations must not prompt for endpoints")
    }

    fn secret(&mut self, _label: &str) -> Result<String, String> {
        self.secrets += 1;
        Ok(TOKEN.to_owned())
    }
}

struct NoPrompt;

impl Prompt for NoPrompt {
    fn value(&mut self, _label: &str) -> Result<String, String> {
        panic!("upgrades must not prompt")
    }

    fn secret(&mut self, _label: &str) -> Result<String, String> {
        panic!("upgrades must not prompt")
    }
}

impl Prompt for FreshAnswers {
    fn value(&mut self, _label: &str) -> Result<String, String> {
        self.values
            .pop_front()
            .ok_or_else(|| "unexpected prompt".to_owned())
    }

    fn secret(&mut self, _label: &str) -> Result<String, String> {
        Ok(TOKEN.to_owned())
    }
}

fn controller_ca() -> Vec<u8> {
    rcgen::generate_simple_self_signed(vec!["controller.example.test".to_owned()])
        .unwrap()
        .cert
        .pem()
        .into_bytes()
}

fn ca_fingerprint(ca: &[u8]) -> String {
    let mut reader = std::io::BufReader::new(std::io::Cursor::new(ca));
    let certificate = rustls_pemfile::certs(&mut reader).next().unwrap().unwrap();
    hex::encode(Sha256::digest(certificate.as_ref()))
}

fn fresh_answers(ca: &[u8]) -> FreshAnswers {
    FreshAnswers {
        values: [
            "https://enroll.example.test/".to_owned(),
            ca_fingerprint(ca),
            "192.168.1.231".to_owned(),
            "192.168.1.211".to_owned(),
            "192.168.100.10".to_owned(),
            "192.168.100.11".to_owned(),
        ]
        .into(),
    }
}

fn package(path: &std::path::Path) {
    package_with_identity(
        path,
        "vonk-forge-agent",
        "1.0.0",
        native_release_identity().architecture,
    );
}

fn package_with_identity(
    path: &std::path::Path,
    package_name: &str,
    version: &str,
    architecture: &str,
) {
    let root = path.parent().unwrap().join("package");
    fs::create_dir_all(root.join("DEBIAN")).unwrap();
    fs::create_dir_all(root.join("usr/lib/vonk-forge")).unwrap();
    fs::write(
        root.join("usr/lib/vonk-forge/vonk-agent"),
        b"installed agent",
    )
    .unwrap();
    fs::write(
        root.join("DEBIAN/control"),
        format!(
            "Package: {package_name}\nVersion: {version}\nArchitecture: {architecture}\nMaintainer: test <test@example.test>\nDescription: test package\n"
        ),
    )
    .unwrap();
    assert!(
        ProcessCommand::new("/usr/bin/dpkg-deb")
            .args(["--build", "--root-owner-group"])
            .arg(&root)
            .arg(path)
            .status()
            .unwrap()
            .success()
    );
}

fn paths(root: &std::path::Path) -> InstallPaths {
    InstallPaths {
        config: root.join("etc/vonk-forge-agent/agent.toml"),
        ca: root.join("etc/vonk-forge-agent/controller-ca.pem"),
        firewall_config: root.join("etc/vonk-forge-agent/docker-firewall.conf"),
        helper_authority: root.join("etc/vonk-forge-agent/host-helper-authority.pub"),
        hosts: root.join("etc/hosts"),
        agent: root.join("usr/lib/vonk-forge/vonk-agent"),
        staging_root: root.join("var/tmp"),
        sudo: PathBuf::from("/usr/bin/sudo"),
        service: "vonk-forge-agent.service".to_owned(),
        required_owner: None,
    }
}

fn signed_request(root: &std::path::Path) -> (SetupRequest, ReleaseAuthority) {
    let package_path = root.join(package_filename());
    package(&package_path);
    let executable = root.join("vonk-spark-setup");
    fs::write(&executable, b"verified setup executable").unwrap();
    let signed = signed_release(root, &package_path, &executable);
    let request = SetupRequest::from_signed_release(
        package_path,
        signed.manifest,
        signed.signature,
        signed.setup_signature,
        executable,
    )
    .unwrap();
    (request, signed.authority)
}

fn root_session(root: &std::path::Path, prepared: &vonk_spark_setup::PreparedSetup) -> PathBuf {
    let session = root.join("var/tmp/vonk-spark-setup.0123456789abcdef");
    fs::create_dir_all(&session).unwrap();
    fs::set_permissions(
        &session,
        std::os::unix::fs::PermissionsExt::from_mode(0o700),
    )
    .unwrap();
    let executable = session.join("vonk-spark-setup");
    fs::copy(prepared.executable_path(), &executable).unwrap();
    fs::set_permissions(
        &executable,
        std::os::unix::fs::PermissionsExt::from_mode(0o700),
    )
    .unwrap();
    let package = session.join("vonk-forge-agent.deb");
    fs::copy(prepared.package_path(), &package).unwrap();
    fs::set_permissions(package, std::os::unix::fs::PermissionsExt::from_mode(0o600)).unwrap();
    executable
}

fn request(root: &std::path::Path) -> SetupRequest {
    let package_path = root.join(package_filename());
    package(&package_path);
    request_for_package(root, package_path)
}

fn request_for_package(root: &std::path::Path, package_path: PathBuf) -> SetupRequest {
    let executable = root.join("vonk-spark-setup");
    fs::write(&executable, b"verified setup executable").unwrap();
    let signed = signed_release(root, &package_path, &executable);
    SetupRequest::from_signed_release(
        package_path,
        signed.manifest,
        signed.signature,
        signed.setup_signature,
        executable,
    )
    .unwrap()
}

fn test_authority(paths: &InstallPaths) -> ReleaseAuthority {
    let root = paths
        .staging_root
        .parent()
        .and_then(std::path::Path::parent)
        .unwrap();
    ReleaseAuthority::from_pem(fs::read(root.join("installer-release-public.pem")).unwrap())
        .unwrap()
}

fn prepare_setup(
    request: &SetupRequest,
    paths: &InstallPaths,
    prompt: &mut dyn Prompt,
    runner: &mut dyn CommandRunner,
    caller: CallerIdentity,
) -> Result<vonk_spark_setup::PreparedSetup, SetupError> {
    prepare_setup_with_authority(
        request,
        paths,
        prompt,
        runner,
        caller,
        &test_authority(paths),
    )
}

fn apply_setup_from(
    input: impl std::io::Read,
    package_source: &std::path::Path,
    executable_source: &std::path::Path,
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    caller: CallerIdentity,
) -> Result<(), SetupError> {
    let session = paths.staging_root.join("vonk-spark-setup.0123456789abcdef");
    if session.exists() {
        fs::remove_dir_all(&session).unwrap();
    }
    fs::create_dir_all(&session).unwrap();
    fs::set_permissions(
        &session,
        std::os::unix::fs::PermissionsExt::from_mode(0o700),
    )
    .unwrap();
    let executable = session.join("vonk-spark-setup");
    if executable_source.is_file() {
        fs::copy(executable_source, &executable).unwrap();
        fs::set_permissions(
            &executable,
            std::os::unix::fs::PermissionsExt::from_mode(0o700),
        )
        .unwrap();
    }
    let package = session.join("vonk-forge-agent.deb");
    if package_source.is_file() {
        fs::copy(package_source, &package).unwrap();
        fs::set_permissions(
            &package,
            std::os::unix::fs::PermissionsExt::from_mode(0o600),
        )
        .unwrap();
    }
    apply_setup_from_with_authority(
        input,
        &executable,
        paths,
        runner,
        caller,
        &test_authority(paths),
    )
}

fn runner_with_bootstrap(ca: &[u8]) -> RecordingRunner {
    let bootstrap = serde_json::json!({
        "controller_endpoint": "https://controller.example.test",
        "enrollment_endpoint": "https://enroll.example.test",
        "ca_fingerprint": ca_fingerprint(ca),
        "ca_pem": String::from_utf8(ca.to_vec()).unwrap(),
        "host_helper_authority_public_key": "11".repeat(32),
        "controller_address": null,
        "service_hostnames": [],
    });
    RecordingRunner {
        commands: Vec::new(),
        outputs: [
            CommandOutput::success(serde_json::to_vec(&bootstrap).unwrap()),
            CommandOutput::success(serde_json::to_vec(&bootstrap).unwrap()),
        ]
        .into(),
        ..Default::default()
    }
}

fn runner_with_private_controller_bootstrap(ca: &[u8]) -> RecordingRunner {
    let bootstrap = serde_json::json!({
        "controller_endpoint": "https://controller.example.test",
        "enrollment_endpoint": "https://enroll.example.test",
        "ca_fingerprint": ca_fingerprint(ca),
        "ca_pem": String::from_utf8(ca.to_vec()).unwrap(),
        "host_helper_authority_public_key": "11".repeat(32),
        "controller_address": "192.168.1.231",
        "service_hostnames": [
            "control.example.test",
            "enroll.example.test",
            "controller.example.test",
            "registry.example.test",
        ],
    });
    RecordingRunner {
        commands: Vec::new(),
        outputs: [
            CommandOutput::success(serde_json::to_vec(&bootstrap).unwrap()),
            CommandOutput::success(serde_json::to_vec(&bootstrap).unwrap()),
        ]
        .into(),
        ..Default::default()
    }
}

fn configured_install(paths: &InstallPaths, ca: &[u8], state: &str) {
    fs::create_dir_all(paths.config.parent().unwrap()).unwrap();
    fs::create_dir_all(paths.agent.parent().unwrap()).unwrap();
    fs::write(
        &paths.config,
        format!(
            "enrollment_url = \"https://enroll.example.test/\"\ncontroller_url = \"https://controller.example.test/\"\nca_path = \"{}\"\nca_sha256 = \"{}\"\ndata_dir = \"/var/lib/vonk-forge-agent\"\nnode_id = \"spk_0123456789abcdef0123456789abcdef\"\nfabric_address = \"192.168.100.10\"\nfabric_bandwidth_mbps = 200000\n",
            paths.ca.display(),
            ca_fingerprint(ca),
        ),
    )
    .unwrap();
    fs::write(&paths.ca, ca).unwrap();
    fs::write(
        &paths.firewall_config,
        "VONK_NAS_MANAGEMENT_IP=192.168.1.231\nVONK_NODE_MANAGEMENT_IP=192.168.1.211\nVONK_NODE_FABRIC_IP=192.168.100.10\nVONK_PEER_FABRIC_IP=192.168.100.11\nVONK_ENDPOINT_HOST_PORTS=8000,8101\nVONK_HOST_ENDPOINT_PORTS=8888\nVONK_RENDEZVOUS_PORT=29500\n",
    )
    .unwrap();
    fs::write(&paths.helper_authority, format!("{}\n", "11".repeat(32))).unwrap();
    fs::write(&paths.agent, "installed agent").unwrap();
    fs::write(paths.config.with_file_name("setup-state"), state).unwrap();
}

fn fresh_prepared(
    root: &std::path::Path,
    install_paths: &InstallPaths,
) -> (vonk_spark_setup::PreparedSetup, RecordingRunner) {
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let ca = controller_ca();
    let mut prepare_runner = runner_with_bootstrap(&ca);
    let mut prompt = fresh_answers(&ca);
    let prepared = prepare_setup(
        &request(root),
        install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(
        &prepared,
        &mut handoff_runner,
        &ReleaseAuthority::canonical(),
    )
    .unwrap();
    (prepared, handoff_runner)
}

fn rewrite_frame(
    valid: &[u8],
    change: impl FnOnce(&mut serde_json::Map<String, serde_json::Value>),
) -> Vec<u8> {
    const MAGIC: &[u8] = b"VONK-SPARK-APPLY-V1\0";
    let payload_length =
        u32::from_be_bytes(valid[MAGIC.len()..MAGIC.len() + 4].try_into().unwrap()) as usize;
    let mut payload: serde_json::Value =
        serde_json::from_slice(&valid[MAGIC.len() + 4..MAGIC.len() + 4 + payload_length]).unwrap();
    change(payload["plan"].as_object_mut().unwrap());
    let payload = serde_json::to_vec(&payload).unwrap();
    let mut frame = Vec::new();
    frame.extend_from_slice(MAGIC);
    frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    frame.extend_from_slice(&payload);
    frame.extend_from_slice(&Sha256::digest(&payload));
    frame
}

mod authority;
mod enrollment;
mod frames;
mod upgrades;
