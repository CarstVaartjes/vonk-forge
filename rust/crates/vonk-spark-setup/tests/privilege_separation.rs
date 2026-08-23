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
    CallerIdentity, Command, CommandOutput, CommandRunner, InstallPaths, Prompt, ReleaseAuthority,
    SetupError, SetupRequest, TtyPrompt, apply_setup_from_with_authority, handoff_to_root,
    prepare_setup_with_authority,
};

const TOKEN: &str = "A123456789012345678901234567890123456789012";

#[derive(Clone, Copy)]
struct NativeReleaseIdentity {
    platform: &'static str,
    architecture: &'static str,
    wrong_architecture: &'static str,
}

fn native_release_identity() -> NativeReleaseIdentity {
    match std::env::consts::ARCH {
        "x86_64" => NativeReleaseIdentity {
            platform: "linux-amd64",
            architecture: "amd64",
            wrong_architecture: "arm64",
        },
        "aarch64" => NativeReleaseIdentity {
            platform: "linux-arm64",
            architecture: "arm64",
            wrong_architecture: "amd64",
        },
        architecture => panic!("unsupported test architecture: {architecture}"),
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
    let release = serde_json::json!({
        "artifacts": {
            (agent_artifact): {
                "path": format!("artifacts/stable/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/spark/current/{}/vonk-forge-agent.deb", identity.platform),
                "sha256": hex::encode(Sha256::digest(&package_raw)),
                "size": package_raw.len(),
            },
            (setup_artifact): {
                "path": format!("artifacts/stable/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/spark/current/{}/vonk-spark-setup", identity.platform),
                "sha256": hex::encode(Sha256::digest(&setup_raw)),
                "size": setup_raw.len(),
            },
            (setup_signature_artifact): {
                "path": format!("artifacts/stable/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/spark/current/{}/vonk-spark-setup.sig", identity.platform),
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
        "schema_version": 1,
        "source_sha": "b".repeat(40),
        "version": "1.0.0",
    });
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
}

impl CommandRunner for RecordingRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
        let default = if command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args.first().map(String::as_str) == Some("show")
        {
            CommandOutput::success(b"4242\n".to_vec())
        } else {
            CommandOutput::success_empty()
        };
        self.commands.push(command);
        Ok(self.outputs.pop_front().unwrap_or(default))
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
    });
    RecordingRunner {
        commands: Vec::new(),
        outputs: [
            CommandOutput::success(serde_json::to_vec(&bootstrap).unwrap()),
            CommandOutput::success(serde_json::to_vec(&bootstrap).unwrap()),
        ]
        .into(),
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
    }
}

fn configured_install(paths: &InstallPaths, ca: &[u8], state: &str) {
    fs::create_dir_all(paths.config.parent().unwrap()).unwrap();
    fs::create_dir_all(paths.agent.parent().unwrap()).unwrap();
    fs::write(
        &paths.config,
        format!(
            "enrollment_url = \"https://enroll.example.test/\"\ncontroller_url = \"https://controller.example.test/\"\nca_path = \"{}\"\nca_sha256 = \"{}\"\ndata_dir = \"/var/lib/vonk-forge-agent\"\nnode_id = \"spk_0123456789abcdef0123456789abcdef\"\npoll_min_seconds = 2\npoll_max_seconds = 60\nfabric_address = \"192.168.100.10\"\nfabric_bandwidth_mbps = 200000\n",
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

#[test]
fn root_apply_accepts_only_artifacts_bound_by_the_trusted_signed_release() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let (request, authority) = signed_request(temporary.path());
    let ca = controller_ca();
    let mut prepare_runner = runner_with_bootstrap(&ca);
    let mut prompt = fresh_answers(&ca);
    let prepared = prepare_setup_with_authority(
        &request,
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
        &authority,
    )
    .unwrap();
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root(&prepared, &mut handoff_runner).unwrap();
    let frame = handoff_runner.commands[0].stdin.clone();
    let staged_executable = root_session(temporary.path(), &prepared);

    let malicious = temporary.path().join("malicious.deb");
    package_with_identity(
        &malicious,
        "vonk-forge-agent",
        "1.0.0",
        native_release_identity().architecture,
    );
    fs::copy(
        malicious,
        staged_executable
            .parent()
            .unwrap()
            .join("vonk-forge-agent.deb"),
    )
    .unwrap();
    let mut apply_runner = RecordingRunner::default();
    let result = apply_setup_from_with_authority(
        frame.as_slice(),
        &staged_executable,
        &install_paths,
        &mut apply_runner,
        CallerIdentity::sudo_root(1000),
        &authority,
    );

    assert!(result.is_err());
    assert!(apply_runner.commands.is_empty());
    assert!(!install_paths.config.exists());
}

#[test]
fn root_apply_rejects_an_attacker_signed_release_and_non_session_execution() {
    let temporary = tempdir().unwrap();
    let trusted_root = temporary.path().join("trusted");
    let attacker_root = temporary.path().join("attacker");
    fs::create_dir_all(&trusted_root).unwrap();
    fs::create_dir_all(&attacker_root).unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let (_, trusted_authority) = signed_request(&trusted_root);
    let (attacker_request, attacker_authority) = signed_request(&attacker_root);
    let ca = controller_ca();
    let mut prepare_runner = runner_with_bootstrap(&ca);
    let mut prompt = fresh_answers(&ca);
    let attacker_prepared = prepare_setup_with_authority(
        &attacker_request,
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
        &attacker_authority,
    )
    .unwrap();
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root(&attacker_prepared, &mut handoff_runner).unwrap();
    let frame = handoff_runner.commands[0].stdin.clone();
    let staged_executable = root_session(temporary.path(), &attacker_prepared);

    let mut runner = RecordingRunner::default();
    let untrusted = apply_setup_from_with_authority(
        frame.as_slice(),
        &staged_executable,
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
        &trusted_authority,
    );
    assert!(matches!(untrusted, Err(SetupError::ReleaseSignature)));
    assert!(runner.commands.is_empty());

    let mut runner = RecordingRunner::default();
    let outside_session = apply_setup_from_with_authority(
        frame.as_slice(),
        attacker_prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
        &attacker_authority,
    );
    assert!(matches!(outside_session, Err(SetupError::PrivilegedInput)));
    assert!(runner.commands.is_empty());
}

#[test]
fn setup_signature_must_match_the_signed_release_before_prompt_or_sudo() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let package_path = temporary.path().join(package_filename());
    package(&package_path);
    let executable = temporary.path().join("vonk-spark-setup");
    fs::write(&executable, b"verified setup executable").unwrap();
    let signed = signed_release(temporary.path(), &package_path, &executable);
    fs::write(&signed.setup_signature, b"not the published signature\n").unwrap();
    let request = SetupRequest::from_signed_release(
        package_path,
        signed.manifest,
        signed.signature,
        signed.setup_signature,
        executable,
    )
    .unwrap();
    let mut runner = RecordingRunner::default();

    let result = prepare_setup_with_authority(
        &request,
        &install_paths,
        &mut NoPrompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
        &signed.authority,
    );

    assert!(matches!(result, Err(SetupError::ReleaseSignature)));
    assert!(runner.commands.is_empty());
}

#[test]
fn fresh_preparation_discovers_and_prompts_before_a_stdin_only_sudo_handoff() {
    let temporary = tempdir().unwrap();
    fs::create_dir_all(paths(temporary.path()).config.parent().unwrap()).unwrap();
    let ca = controller_ca();
    let mut prepare_runner = runner_with_bootstrap(&ca);
    let mut prompt = fresh_answers(&ca);

    let prepared = prepare_setup(
        &request(temporary.path()),
        &paths(temporary.path()),
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();

    assert_eq!(prepare_runner.commands.len(), 2);
    assert!(
        prepare_runner
            .commands
            .iter()
            .all(|command| command.program == std::path::Path::new("/usr/bin/curl"))
    );
    assert!(
        prepare_runner.commands[0]
            .args
            .contains(&"--insecure".to_owned())
    );
    assert!(
        prepare_runner.commands[1]
            .args
            .contains(&"--cacert".to_owned())
    );

    let mut root_runner = RecordingRunner::default();
    handoff_to_root(&prepared, &mut root_runner).unwrap();

    assert_eq!(root_runner.commands.len(), 1);
    let sudo = &root_runner.commands[0];
    assert_eq!(sudo.program, std::path::Path::new("/usr/bin/sudo"));
    assert!(sudo.args.iter().all(|argument| argument != TOKEN));
    assert!(sudo.env.values().all(|value| value != TOKEN));
    assert!(
        sudo.stdin
            .windows(TOKEN.len())
            .any(|value| value == TOKEN.as_bytes())
    );
    assert!(
        sudo.args
            .iter()
            .any(|argument| argument.contains("__apply"))
    );
    assert!(
        sudo.args
            .iter()
            .all(|argument| !argument.contains("exec \"$setup\"")),
        "the root staging shell must regain control and remove its temporary copies"
    );
    assert!(
        sudo.args
            .iter()
            .all(|argument| !argument.contains("--privileged"))
    );
    assert!(
        sudo.args
            .iter()
            .all(|argument| !argument.contains("/dev/tty"))
    );
    assert!(sudo.args.iter().all(|argument| !argument.contains("curl")));
}

#[test]
fn root_apply_installs_pairs_starts_and_verifies_without_tty_or_discovery() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let ca = controller_ca();
    fs::write(&install_paths.hosts, "127.0.0.1 localhost\n").unwrap();
    let mut prepare_runner = runner_with_private_controller_bootstrap(&ca);
    let mut prompt = fresh_answers(&ca);
    prompt.values.remove(2);
    let prepared = prepare_setup(
        &request(temporary.path())
            .with_controller_address(Some("192.168.1.231"))
            .unwrap(),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    for command in &prepare_runner.commands {
        assert!(command.args.windows(2).any(|arguments| {
            arguments == ["--resolve", "enroll.example.test:443:192.168.1.231"]
        }));
    }
    assert!(
        prepare_runner.commands[0]
            .args
            .contains(&"--insecure".to_owned())
    );
    assert!(
        prepare_runner.commands[1]
            .args
            .contains(&"--cacert".to_owned())
    );
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root(&prepared, &mut handoff_runner).unwrap();
    let frame = handoff_runner.commands[0].stdin.clone();
    let mut apply_runner = RecordingRunner::default();

    apply_setup_from(
        frame.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut apply_runner,
        CallerIdentity::sudo_root(1000),
    )
    .unwrap();

    assert_eq!(
        apply_runner.commands[0].program,
        std::path::Path::new("/usr/bin/apt-get")
    );
    assert_eq!(apply_runner.commands[0].args, ["update"]);
    assert!(
        apply_runner.commands[1]
            .args
            .iter()
            .any(|argument| argument == "install")
    );
    let pair = apply_runner
        .commands
        .iter()
        .find(|command| command.args.iter().any(|argument| argument == "pair"))
        .unwrap();
    assert_eq!(pair.program, std::path::Path::new("/usr/bin/setpriv"));
    assert_eq!(pair.stdin, format!("{TOKEN}\n").into_bytes());
    assert!(pair.args.iter().all(|argument| argument != TOKEN));
    assert!(apply_runner.commands.iter().all(|command| {
        command.program != std::path::Path::new("/usr/bin/curl")
            && command.program != std::path::Path::new("/usr/bin/sudo")
            && command.args.iter().all(|argument| argument != TOKEN)
            && command.env.values().all(|value| value != TOKEN)
    }));
    assert!(install_paths.config.is_file());
    assert!(install_paths.ca.is_file());
    assert_eq!(
        fs::read_to_string(&install_paths.firewall_config).unwrap(),
        "VONK_NAS_MANAGEMENT_IP=192.168.1.231\nVONK_NODE_MANAGEMENT_IP=192.168.1.211\nVONK_NODE_FABRIC_IP=192.168.100.10\nVONK_PEER_FABRIC_IP=192.168.100.11\nVONK_ENDPOINT_HOST_PORTS=8000,8101\nVONK_HOST_ENDPOINT_PORTS=8888\nVONK_RENDEZVOUS_PORT=29500\n"
    );
    assert_eq!(
        fs::metadata(&install_paths.firewall_config)
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(
        fs::read_to_string(&install_paths.helper_authority).unwrap(),
        format!("{}\n", "11".repeat(32))
    );
    let agent_config = fs::read_to_string(&install_paths.config).unwrap();
    assert!(agent_config.contains("fabric_address = \"192.168.100.10\"\n"));
    assert!(agent_config.contains("fabric_bandwidth_mbps = 200000\n"));
    assert_eq!(
        fs::read_to_string(&install_paths.hosts).unwrap(),
        "127.0.0.1 localhost\n# BEGIN VONK FORGE MANAGED HOSTS\n192.168.1.231 control.example.test enroll.example.test controller.example.test registry.example.test\n# END VONK FORGE MANAGED HOSTS\n"
    );
    assert_eq!(
        fs::read_to_string(install_paths.config.with_file_name("setup-state")).unwrap(),
        "paired-v1\n"
    );
    assert!(apply_runner.commands.iter().any(|command| {
        command.program == install_paths.agent
            && command
                .args
                .iter()
                .any(|argument| argument == "verify-readiness")
    }));
    assert!(apply_runner.commands.iter().any(|command| {
        command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args
                == [
                    "enable",
                    "--now",
                    "vonk-forge-docker-firewall.service",
                    "vonk-forge-package-helper.socket",
                    "vonk-forge-agent.service",
                ]
    }));
}

#[test]
fn pairing_recovery_prompts_once_before_sudo_and_uses_the_same_narrow_apply_path() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "unpaired-v1\n");
    let mut prompt = TokenOnlyPrompt { secrets: 0 };
    let mut prepare_runner = RecordingRunner::default();
    let prepared = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();

    assert_eq!(prompt.secrets, 1);
    assert!(prepare_runner.commands.is_empty());
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root(&prepared, &mut handoff_runner).unwrap();
    let mut apply_runner = RecordingRunner::default();
    apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut apply_runner,
        CallerIdentity::sudo_root(1000),
    )
    .unwrap();

    let pair = apply_runner
        .commands
        .iter()
        .find(|command| command.args.iter().any(|argument| argument == "pair"))
        .unwrap();
    assert_eq!(pair.stdin, format!("{TOKEN}\n").into_bytes());
    assert_eq!(
        fs::read_to_string(install_paths.config.with_file_name("setup-state")).unwrap(),
        "paired-v1\n"
    );
}

#[test]
fn existing_upgrade_never_prompts_or_discovers_and_restarts_through_apply() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "paired-v1\n");
    let mut prompt = NoPrompt;
    let mut prepare_runner = RecordingRunner::default();
    let prepared = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();

    assert!(prepare_runner.commands.is_empty());
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root(&prepared, &mut handoff_runner).unwrap();
    assert!(
        !handoff_runner.commands[0]
            .stdin
            .windows(TOKEN.len())
            .any(|value| value == TOKEN.as_bytes())
    );
    let mut apply_runner = RecordingRunner::default();
    apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut apply_runner,
        CallerIdentity::sudo_root(1000),
    )
    .unwrap();

    assert!(apply_runner.commands.iter().any(|command| {
        command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args == ["restart", "vonk-forge-agent.service"]
    }));
    assert!(apply_runner.commands.iter().any(|command| {
        command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args
                == [
                    "enable",
                    "--now",
                    "vonk-forge-docker-firewall.service",
                    "vonk-forge-package-helper.socket",
                    "vonk-forge-agent.service",
                ]
    }));
    assert!(
        apply_runner
            .commands
            .iter()
            .all(|command| !command.args.iter().any(|argument| argument == "pair"))
    );
}

#[test]
fn tty_prompt_construction_is_lazy_for_headless_upgrades() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "paired-v1\n");
    let setup_request = request(temporary.path());
    let mut prompt = TtyPrompt::new();
    let mut runner = RecordingRunner::default();

    let result = prepare_setup(
        &setup_request,
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
    );

    assert!(result.is_ok());
    assert!(runner.commands.is_empty());
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
    handoff_to_root(&prepared, &mut handoff_runner).unwrap();
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

#[test]
fn privileged_frame_fails_closed_when_truncated_tampered_oversized_or_extended() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    let valid = handoff_runner.commands[0].stdin.clone();
    let mut truncated = valid.clone();
    truncated.pop();
    let mut tampered = valid.clone();
    let middle = tampered.len() / 2;
    tampered[middle] ^= 1;
    let mut extended = valid.clone();
    extended.push(0);
    let oversized = vec![0_u8; 300 * 1024];

    for malformed in [truncated, tampered, extended, oversized] {
        let mut runner = RecordingRunner::default();
        let result = apply_setup_from(
            malformed.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut runner,
            CallerIdentity::sudo_root(1000),
        );
        assert!(result.is_err());
        assert!(runner.commands.is_empty());
        assert!(!install_paths.config.exists());
    }
}

#[test]
fn apply_frame_is_bound_to_the_authenticated_sudo_caller() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    let frame = &handoff_runner.commands[0].stdin;

    for caller in [
        CallerIdentity::unprivileged(1000),
        CallerIdentity::direct_root(),
        CallerIdentity::sudo_root(1001),
    ] {
        let mut runner = RecordingRunner::default();
        let result = apply_setup_from(
            frame.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut runner,
            caller,
        );
        assert!(result.is_err());
        assert!(runner.commands.is_empty());
    }
}

#[test]
fn public_preparation_rejects_root_before_prompting_or_network_io() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let mut runner = RecordingRunner::default();
    let mut prompt = NoPrompt;

    let result = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::direct_root(),
    );

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
}

#[test]
fn markerless_agent_only_state_fails_closed_before_prompting_or_network_io() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    fs::create_dir_all(install_paths.agent.parent().unwrap()).unwrap();
    fs::write(&install_paths.agent, b"partial agent install").unwrap();
    let setup_request = request(temporary.path());
    let mut prompt = NoPrompt;
    let mut runner = RecordingRunner::default();

    let result = prepare_setup(
        &setup_request,
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
    );

    assert!(matches!(result, Err(SetupError::ExistingInstall)));
    assert!(runner.commands.is_empty());
}

#[test]
fn apply_rejects_a_plan_for_a_different_installation_phase() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    configured_install(&install_paths, &controller_ca(), "unpaired-v1\n");
    let mut runner = RecordingRunner::default();
    let missing_package = temporary.path().join("missing-package.deb");

    let result = apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        &missing_package,
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(matches!(result, Err(SetupError::PrivilegedInput)));
    assert!(runner.commands.is_empty());
}

#[test]
fn package_mutation_after_preparation_is_rejected_before_root_commands() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    fs::OpenOptions::new()
        .append(true)
        .open(prepared.package_path())
        .unwrap()
        .write_all(b"changed")
        .unwrap();
    let mut runner = RecordingRunner::default();

    let result = apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
}

#[test]
fn root_rejects_a_setup_binary_changed_after_unprivileged_verification() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    fs::write(prepared.executable_path(), b"changed setup executable").unwrap();
    let mut runner = RecordingRunner::default();

    let result = apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
}

#[test]
fn package_format_identity_and_release_name_are_verified_before_prompt_or_sudo() {
    for case in ["format", "identity", "name"] {
        let temporary = tempdir().unwrap();
        let install_paths = paths(temporary.path());
        fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
        let package_path = if case == "name" {
            temporary.path().join("release.deb")
        } else {
            temporary.path().join(package_filename())
        };
        match case {
            "format" => fs::write(&package_path, vec![b'x'; 80]).unwrap(),
            "identity" => package_with_identity(
                &package_path,
                "vonk-forge-agent",
                "1.0.0",
                native_release_identity().wrong_architecture,
            ),
            "name" => package(&package_path),
            _ => unreachable!(),
        }
        let setup_request = request_for_package(temporary.path(), package_path);
        let mut prompt = NoPrompt;
        let mut runner = RecordingRunner::default();

        let result = prepare_setup(
            &setup_request,
            &install_paths,
            &mut prompt,
            &mut runner,
            CallerIdentity::unprivileged(1000),
        );

        let exact_error = match case {
            "format" => matches!(result, Err(SetupError::PackageFormat)),
            "identity" => matches!(result, Err(SetupError::PackageIdentity)),
            "name" => matches!(result, Err(SetupError::UnsafePackage)),
            _ => unreachable!(),
        };
        assert!(exact_error, "{case} must be rejected by its exact boundary");
        assert!(runner.commands.is_empty());
    }
}

#[test]
fn enrollment_bootstrap_must_match_the_prompted_ca_before_sudo() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let ca = controller_ca();
    let bootstrap = serde_json::json!({
        "controller_endpoint": "https://controller.example.test",
        "enrollment_endpoint": "https://enroll.example.test",
        "ca_fingerprint": "0".repeat(64),
        "ca_pem": String::from_utf8(ca.clone()).unwrap(),
        "host_helper_authority_public_key": "11".repeat(32),
    });
    let mut runner = RecordingRunner {
        commands: Vec::new(),
        outputs: [CommandOutput::success(
            serde_json::to_vec(&bootstrap).unwrap(),
        )]
        .into(),
    };
    let mut prompt = fresh_answers(&ca);

    let result = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
    );

    assert!(result.is_err());
    assert_eq!(runner.commands.len(), 1);
    assert_eq!(
        runner.commands[0].program,
        std::path::Path::new("/usr/bin/curl")
    );
}

#[test]
fn successful_pairing_is_recorded_before_later_service_recovery_is_needed() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    let mut runner = RecordingRunner {
        commands: Vec::new(),
        outputs: [
            CommandOutput::success_empty(),
            CommandOutput::success_empty(),
            CommandOutput::success_empty(),
            CommandOutput {
                success: false,
                stdout: Vec::new(),
            },
        ]
        .into(),
    };

    let result = apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(result.is_err());
    assert_eq!(
        fs::read_to_string(install_paths.config.with_file_name("setup-state")).unwrap(),
        "paired-v1\n",
        "a retry must upgrade/restart without asking for an already consumed token"
    );
}

#[test]
fn privileged_plan_rejects_unknown_fields_even_with_a_valid_frame_digest() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    let valid = &handoff_runner.commands[0].stdin;
    let frame = rewrite_frame(valid, |plan| {
        plan.insert("unexpected".to_owned(), serde_json::Value::Bool(true));
    });
    let mut runner = RecordingRunner::default();

    let result = apply_setup_from(
        frame.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
}

#[test]
fn semantic_plan_validation_precedes_any_root_artifact_processing() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    let frame = rewrite_frame(&handoff_runner.commands[0].stdin, |plan| {
        plan.insert(
            "pairing_token".to_owned(),
            serde_json::Value::String("invalid".to_owned()),
        );
    });
    let missing_package = temporary.path().join("missing-package.deb");
    let mut runner = RecordingRunner::default();

    let result = apply_setup_from(
        frame.as_slice(),
        &missing_package,
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(matches!(result, Err(SetupError::PrivilegedInput)));
    assert!(runner.commands.is_empty());
}

#[test]
fn pairing_plan_must_match_root_owned_configuration_before_package_processing() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "unpaired-v1\n");
    let mut prompt = TokenOnlyPrompt { secrets: 0 };
    let mut prepare_runner = RecordingRunner::default();
    let prepared = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root(&prepared, &mut handoff_runner).unwrap();
    let frame = rewrite_frame(&handoff_runner.commands[0].stdin, |plan| {
        plan.insert(
            "ca_sha256".to_owned(),
            serde_json::Value::String("0".repeat(64)),
        );
    });
    let mut runner = RecordingRunner::default();

    let result = apply_setup_from(
        frame.as_slice(),
        &temporary.path().join("missing-package.deb"),
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(matches!(result, Err(SetupError::PrivilegedInput)));
    assert!(runner.commands.is_empty());
}

#[test]
fn system_path_operations_cannot_use_a_synthetic_caller_identity() {
    let temporary = tempdir().unwrap();
    let mut install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    install_paths.required_owner = Some(0);
    let ca = controller_ca();
    let mut prompt = fresh_answers(&ca);
    let mut runner = runner_with_bootstrap(&ca);

    let result = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::unprivileged(u32::MAX - 1),
    );

    assert!(matches!(result, Err(SetupError::CallerPhase)));
    assert!(runner.commands.is_empty());
}

#[test]
fn setup_state_below_a_symlinked_configuration_directory_fails_before_prompting() {
    let temporary = tempdir().unwrap();
    let real_directory = temporary.path().join("real-configuration");
    fs::create_dir_all(&real_directory).unwrap();
    let unsafe_directory = temporary.path().join("etc/vonk-forge-agent");
    fs::create_dir_all(unsafe_directory.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink(&real_directory, &unsafe_directory).unwrap();
    let install_paths = InstallPaths {
        config: unsafe_directory.join("agent.toml"),
        ca: unsafe_directory.join("controller-ca.pem"),
        firewall_config: unsafe_directory.join("docker-firewall.conf"),
        helper_authority: unsafe_directory.join("host-helper-authority.pub"),
        hosts: temporary.path().join("etc/hosts"),
        agent: temporary.path().join("usr/lib/vonk-forge/vonk-agent"),
        staging_root: temporary.path().join("var/tmp"),
        sudo: PathBuf::from("/usr/bin/sudo"),
        service: "vonk-forge-agent.service".to_owned(),
        required_owner: None,
    };
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "paired-v1\n");
    let mut prompt = TokenOnlyPrompt { secrets: 0 };
    let mut runner = RecordingRunner::default();

    let result = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
    );

    assert!(result.is_err());
    assert_eq!(prompt.secrets, 0);
    assert!(runner.commands.is_empty());
}
