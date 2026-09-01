use std::{
    collections::{BTreeMap, VecDeque},
    fs,
    io::Write,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Command as ProcessCommand, Stdio},
};

use sha2::{Digest, Sha256};
use tempfile::tempdir;
use vonk_spark_setup::{
    CallerIdentity, Command, CommandOutput, CommandRunner, CommandStderr, InstallPaths, Prompt,
    ReleaseAuthority, SetupRequest, SystemCommandRunner, TtyPrompt,
    apply_setup_from_with_authority, handoff_to_root, handoff_to_root_with_authority,
    prepare_setup_with_authority,
};

const HELPER_ROOT: &str = "VONK_SPARK_PROCESS_HELPER_ROOT";
const APPLY_HELPER: &str = "VONK_SPARK_PROCESS_APPLY_HELPER";
const TOKEN: &str = "A123456789012345678901234567890123456789012";

#[derive(Clone, Copy)]
struct NativeReleaseIdentity {
    platform: &'static str,
    architecture: &'static str,
}

fn native_release_identity() -> NativeReleaseIdentity {
    match std::env::consts::ARCH {
        "x86_64" => NativeReleaseIdentity {
            platform: "linux-amd64",
            architecture: "amd64",
        },
        "aarch64" => NativeReleaseIdentity {
            platform: "linux-arm64",
            architecture: "arm64",
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

struct NoCommands;

impl CommandRunner for NoCommands {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
        panic!(
            "headless upgrade unexpectedly ran {}",
            command.program.display()
        )
    }
}

struct FreshPrompt {
    values: VecDeque<String>,
}

impl Prompt for FreshPrompt {
    fn value(&mut self, _label: &str) -> Result<String, String> {
        self.values
            .pop_front()
            .ok_or_else(|| "unexpected prompt".to_owned())
    }

    fn secret(&mut self, _label: &str) -> Result<String, String> {
        Ok(TOKEN.to_owned())
    }
}

struct BootstrapRunner {
    body: Vec<u8>,
}

#[derive(Default)]
struct ApplyRecordingRunner {
    commands: Vec<Command>,
}

impl CommandRunner for ApplyRecordingRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
        let output = if command.program == Path::new("/usr/bin/systemctl")
            && command.args.first().map(String::as_str) == Some("show")
        {
            CommandOutput::success(b"4242\n".to_vec())
        } else {
            CommandOutput::success_empty()
        };
        self.commands.push(command);
        Ok(output)
    }

    fn sleep(&mut self, _duration: std::time::Duration) {}
}

impl CommandRunner for BootstrapRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
        assert_eq!(command.program, Path::new("/usr/bin/curl"));
        Ok(CommandOutput::success(self.body.clone()))
    }
}

fn package(path: &Path) {
    let architecture = native_release_identity().architecture;
    let root = path.parent().unwrap().join("package-root");
    fs::create_dir_all(root.join("DEBIAN")).unwrap();
    fs::write(
        root.join("DEBIAN/control"),
        format!("Package: vonk-forge-agent\nVersion: 1.0.0\nArchitecture: {architecture}\nMaintainer: test <test@example.test>\nDescription: process test\n"),
    )
    .unwrap();
    assert!(
        ProcessCommand::new("/usr/bin/dpkg-deb")
            .args(["--build", "--root-owner-group"])
            .arg(&root)
            .arg(path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .unwrap()
            .success()
    );
}

fn signed_request(root: &Path) -> (SetupRequest, ReleaseAuthority) {
    signed_request_with_setup(root, b"process-boundary setup executable")
}

fn signed_request_with_setup(
    root: &Path,
    setup_contents: &[u8],
) -> (SetupRequest, ReleaseAuthority) {
    let identity = native_release_identity();
    let package_path = root.join(package_filename());
    package(&package_path);
    let executable = root.join("vonk-spark-setup");
    fs::write(&executable, setup_contents).unwrap();
    let private_key = root.join("test-private.pem");
    let public_key = root.join("test-public.pem");
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
            .stdout(Stdio::null())
            .status()
            .unwrap()
            .success()
    );
    let setup_raw_signature = root.join("setup.raw.sig");
    assert!(
        ProcessCommand::new("/usr/bin/openssl")
            .args(["dgst", "-sha256", "-sign"])
            .arg(&private_key)
            .args(["-out"])
            .arg(&setup_raw_signature)
            .arg(&executable)
            .stdout(Stdio::null())
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
            .stdout(Stdio::null())
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
    let generation = "a".repeat(64);
    let agent_artifact = format!("agent-package-{}", identity.platform);
    let setup_artifact = format!("spark-setup-{}", identity.platform);
    let setup_signature_artifact = format!("spark-setup-signature-{}", identity.platform);
    let manifest = root.join("release.json");
    let release = serde_json::json!({
        "artifacts": {
            (agent_artifact): {
                "architecture": identity.platform,
                "host_signature": "f".repeat(128),
                "package_version": "1.0.0",
                "path": format!("artifacts/stable/releases/{generation}/spark/current/{}/vonk-forge-agent.deb", identity.platform),
                "sha256": hex::encode(Sha256::digest(fs::read(&package_path).unwrap())),
                "size": fs::metadata(&package_path).unwrap().len(),
                "target_binary_digest": "1".repeat(64),
                "target_build_digest": format!("sha256:{}", "2".repeat(64)),
            },
            (setup_artifact): {
                "path": format!("artifacts/stable/releases/{generation}/spark/current/{}/vonk-spark-setup", identity.platform),
                "sha256": hex::encode(Sha256::digest(fs::read(&executable).unwrap())),
                "size": fs::metadata(&executable).unwrap().len(),
            },
            (setup_signature_artifact): {
                "path": format!("artifacts/stable/releases/{generation}/spark/current/{}/vonk-spark-setup.sig", identity.platform),
                "sha256": hex::encode(Sha256::digest(fs::read(&setup_signature).unwrap())),
                "size": fs::metadata(&setup_signature).unwrap().len(),
            }
        },
        "bootstraps": {"spark": {"path": "immutable", "sha256": "b".repeat(64), "size": 1}},
        "channel": "stable",
        "generation": generation,
        "images": {"api": format!("example.test/api@sha256:{}", "c".repeat(64))},
        "schema_version": 2,
        "source_sha": "d".repeat(40),
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
            .stdout(Stdio::null())
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
            .stdout(Stdio::null())
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
    let request = SetupRequest::from_signed_release(
        package_path,
        manifest,
        signature,
        setup_signature,
        executable,
    )
    .unwrap();
    let authority = ReleaseAuthority::from_pem(fs::read(public_key).unwrap()).unwrap();
    (request, authority)
}

fn install_paths(root: &Path) -> InstallPaths {
    InstallPaths {
        config: root.join("etc/vonk-forge-agent/agent.toml"),
        ca: root.join("etc/vonk-forge-agent/controller-ca.pem"),
        firewall_config: root.join("etc/vonk-forge-agent/docker-firewall.conf"),
        helper_authority: root.join("etc/vonk-forge-agent/host-helper-authority.pub"),
        hosts: root.join("etc/hosts"),
        agent: root.join("usr/lib/vonk-forge/vonk-agent"),
        staging_root: root.join("var/tmp"),
        sudo: root.join("fake-sudo"),
        service: "vonk-forge-agent.service".to_owned(),
        required_owner: None,
    }
}

fn configured_upgrade(paths: &InstallPaths) {
    let ca = rcgen::generate_simple_self_signed(vec!["controller.example.test".to_owned()])
        .unwrap()
        .cert
        .pem()
        .into_bytes();
    let mut reader = std::io::BufReader::new(std::io::Cursor::new(&ca));
    let certificate = rustls_pemfile::certs(&mut reader).next().unwrap().unwrap();
    let fingerprint = hex::encode(Sha256::digest(certificate.as_ref()));
    fs::create_dir_all(paths.config.parent().unwrap()).unwrap();
    fs::create_dir_all(paths.agent.parent().unwrap()).unwrap();
    fs::write(
        &paths.config,
        format!(
            "enrollment_url = \"https://enroll.example.test/\"\ncontroller_url = \"https://controller.example.test/\"\nca_path = \"{}\"\nca_sha256 = \"{fingerprint}\"\ndata_dir = \"/var/lib/vonk-forge-agent\"\nnode_id = \"spk_0123456789abcdef0123456789abcdef\"\npoll_min_seconds = 2\npoll_max_seconds = 60\nfabric_address = \"192.168.100.10\"\nfabric_bandwidth_mbps = 200000\n",
            paths.ca.display(),
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
    fs::write(&paths.agent, b"installed agent").unwrap();
    fs::write(paths.config.with_file_name("setup-state"), b"paired-v1\n").unwrap();
}

fn run_headless_upgrade_process(root: PathBuf) {
    let paths = install_paths(&root);
    configured_upgrade(&paths);
    let (request, authority) = signed_request(&root);
    let receipt = root.join("sudo-receipt");
    fs::write(
        &paths.sudo,
        format!(
            "#!/bin/sh\nset -eu\nprintf '%s\\0' \"$@\" > '{}.argv'\nenv -0 > '{}.env'\ncat > '{}.stdin'\n",
            receipt.display(),
            receipt.display(),
            receipt.display(),
        ),
    )
    .unwrap();
    fs::set_permissions(&paths.sudo, fs::Permissions::from_mode(0o700)).unwrap();
    let mut prompt = TtyPrompt::new();
    let mut no_commands = NoCommands;
    let prepared = prepare_setup_with_authority(
        &request,
        &paths,
        &mut prompt,
        &mut no_commands,
        CallerIdentity::unprivileged(rustix::process::geteuid().as_raw()),
        &authority,
    )
    .unwrap();
    handoff_to_root(&prepared, &mut SystemCommandRunner).unwrap();
}

fn run_fresh_handoff_process(root: PathBuf) {
    let paths = install_paths(&root);
    fs::create_dir_all(paths.config.parent().unwrap()).unwrap();
    let (request, authority) = signed_request(&root);
    let ca = rcgen::generate_simple_self_signed(vec!["controller.example.test".to_owned()])
        .unwrap()
        .cert
        .pem()
        .into_bytes();
    let mut reader = std::io::BufReader::new(std::io::Cursor::new(&ca));
    let certificate = rustls_pemfile::certs(&mut reader).next().unwrap().unwrap();
    let fingerprint = hex::encode(Sha256::digest(certificate.as_ref()));
    let mut prompt = FreshPrompt {
        values: [
            "https://enroll.example.test/".to_owned(),
            fingerprint.clone(),
            "192.168.1.231".to_owned(),
            "192.168.1.211".to_owned(),
            "192.168.100.10".to_owned(),
            "192.168.100.11".to_owned(),
        ]
        .into(),
    };
    let bootstrap = serde_json::to_vec(&serde_json::json!({
        "controller_endpoint": "https://controller.example.test",
        "enrollment_endpoint": "https://enroll.example.test",
        "ca_fingerprint": fingerprint,
        "ca_pem": String::from_utf8(ca).unwrap(),
        "host_helper_authority_public_key": "11".repeat(32),
    }))
    .unwrap();
    let receipt = root.join("sudo-receipt");
    fs::write(
        &paths.sudo,
        format!(
            "#!/bin/sh\nset -eu\nprintf '%s\\0' \"$@\" > '{}.argv'\nenv -0 > '{}.env'\ncat > '{}.stdin'\n",
            receipt.display(),
            receipt.display(),
            receipt.display(),
        ),
    )
    .unwrap();
    fs::set_permissions(&paths.sudo, fs::Permissions::from_mode(0o700)).unwrap();
    let prepared = prepare_setup_with_authority(
        &request,
        &paths,
        &mut prompt,
        &mut BootstrapRunner { body: bootstrap },
        CallerIdentity::unprivileged(rustix::process::geteuid().as_raw()),
        &authority,
    )
    .unwrap();
    handoff_to_root(&prepared, &mut SystemCommandRunner).unwrap();
}

fn run_apply_process(root: PathBuf) {
    let paths = install_paths(&root);
    let authority =
        ReleaseAuthority::from_pem(fs::read(root.join("test-public.pem")).unwrap()).unwrap();
    let executable = paths
        .staging_root
        .join("vonk-spark-setup.012345")
        .join("vonk-spark-setup");
    let mut runner = ApplyRecordingRunner::default();
    apply_setup_from_with_authority(
        std::io::stdin().lock(),
        &executable,
        &paths,
        &mut runner,
        CallerIdentity::sudo_root(rustix::process::geteuid().as_raw()),
        &authority,
    )
    .unwrap();
    let commands = runner
        .commands
        .iter()
        .map(|command| {
            serde_json::json!({
                "program": command.program,
                "args": command.args,
                "env": command.env,
                "stdin": command.stdin,
            })
        })
        .collect::<Vec<_>>();
    fs::write(
        root.join("apply-commands.json"),
        serde_json::to_vec(&commands).unwrap(),
    )
    .unwrap();
}

#[test]
fn real_system_runner_clears_environment_and_forwards_stdin() {
    let temporary = tempdir().unwrap();
    let script = temporary.path().join("capture");
    fs::write(
        &script,
        "#!/bin/sh\nset -eu\npayload=$(cat)\nprintf '%s|%s|%s' \"$payload\" \"${VONK_ALLOWED:-}\" \"${HOME-unset}\"\n",
    )
    .unwrap();
    fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
    let command = Command {
        program: script,
        args: Vec::new(),
        env: BTreeMap::from([("VONK_ALLOWED".to_owned(), "yes".to_owned())]),
        stdin: b"stdin-secret\n".to_vec(),
        stderr: CommandStderr::Inherit,
    };

    let output = SystemCommandRunner.run(command).unwrap();

    assert!(output.success);
    assert_eq!(output.stdout, b"stdin-secret|yes|unset");
}

#[test]
fn canonical_release_anchor_has_the_audited_identity() {
    let anchor =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../install/installer-release-public.pem");
    let der = ProcessCommand::new("/usr/bin/openssl")
        .args(["pkey", "-pubin", "-in"])
        .arg(&anchor)
        .args(["-outform", "DER"])
        .output()
        .unwrap();
    assert!(der.status.success());
    assert_eq!(
        hex::encode(Sha256::digest(&der.stdout)),
        "579342a2286f13fd2fe9ea25e44866bb9e50f05bc1f9a5354b5cda28950e1ed4"
    );
    let description = ProcessCommand::new("/usr/bin/openssl")
        .args(["pkey", "-pubin", "-in"])
        .arg(anchor)
        .args(["-text", "-noout"])
        .output()
        .unwrap();
    assert!(description.status.success());
    assert!(String::from_utf8_lossy(&description.stdout).contains("4096 bit"));
}

fn install_executing_sudo(paths: &InstallPaths) {
    fs::create_dir_all(&paths.staging_root).unwrap();
    fs::write(&paths.sudo, "#!/bin/sh\nset -eu\nexec \"$@\"\n").unwrap();
    fs::set_permissions(&paths.sudo, fs::Permissions::from_mode(0o700)).unwrap();
}

#[test]
fn generated_root_handoff_executes_only_the_verified_staged_setup_with_argument_free_apply() {
    let temporary = tempdir().unwrap();
    let paths = install_paths(temporary.path());
    configured_upgrade(&paths);
    install_executing_sudo(&paths);
    let receipt = temporary.path().join("verified-setup");
    let setup = format!(
        "#!/bin/sh\nset -eu\nprintf '%s\\0' \"$0\" \"$@\" > '{}.argv'\nenv -0 > '{}.env'\ncat > '{}.stdin'\n",
        receipt.display(),
        receipt.display(),
        receipt.display(),
    );
    let (request, authority) = signed_request_with_setup(temporary.path(), setup.as_bytes());
    let mut no_commands = NoCommands;
    let prepared = prepare_setup_with_authority(
        &request,
        &paths,
        &mut TtyPrompt::new(),
        &mut no_commands,
        CallerIdentity::unprivileged(rustix::process::geteuid().as_raw()),
        &authority,
    )
    .unwrap();

    handoff_to_root_with_authority(&prepared, &mut SystemCommandRunner, &authority).unwrap();

    let argv = fs::read(format!("{}.argv", receipt.display())).unwrap();
    let arguments = argv
        .split(|byte| *byte == 0)
        .filter(|value| !value.is_empty())
        .collect::<Vec<_>>();
    assert_eq!(arguments.len(), 2);
    assert!(arguments[0].ends_with(b"/vonk-spark-setup"));
    assert_eq!(arguments[1], b"__apply");
    let environment = fs::read(format!("{}.env", receipt.display())).unwrap();
    assert!(
        !environment
            .windows(TOKEN.len())
            .any(|part| part == TOKEN.as_bytes())
    );
    let stdin = fs::read(format!("{}.stdin", receipt.display())).unwrap();
    assert!(stdin.starts_with(b"VONK-SPARK-APPLY-V1\0"));
}

#[test]
fn generated_root_handoff_rejects_post_prepare_setup_replacement_before_payload_execution() {
    let temporary = tempdir().unwrap();
    let paths = install_paths(temporary.path());
    configured_upgrade(&paths);
    install_executing_sudo(&paths);
    let payload_marker = temporary.path().join("replacement-executed");
    let (request, authority) =
        signed_request_with_setup(temporary.path(), b"#!/bin/sh\nset -eu\nexit 0\n");
    let mut no_commands = NoCommands;
    let prepared = prepare_setup_with_authority(
        &request,
        &paths,
        &mut TtyPrompt::new(),
        &mut no_commands,
        CallerIdentity::unprivileged(rustix::process::geteuid().as_raw()),
        &authority,
    )
    .unwrap();
    fs::write(
        prepared.executable_path(),
        format!(
            "#!/bin/sh\nset -eu\nprintf executed > '{}'\n",
            payload_marker.display()
        ),
    )
    .unwrap();

    let result = handoff_to_root_with_authority(&prepared, &mut SystemCommandRunner, &authority);

    assert!(result.is_err());
    assert!(!payload_marker.exists());
}

fn caller_bound_frame(uid: u32) -> Vec<u8> {
    const MAGIC: &[u8] = b"VONK-SPARK-APPLY-V1\0";
    let payload = serde_json::to_vec(&serde_json::json!({
        "schema_version": 1,
        "caller_uid": uid,
        "release_manifest": [1],
        "release_signature": [1],
        "plan": {"operation": "upgrade"},
    }))
    .unwrap();
    let mut frame = Vec::new();
    frame.extend_from_slice(MAGIC);
    frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    frame.extend_from_slice(&payload);
    frame.extend_from_slice(&Sha256::digest(&payload));
    frame
}

#[test]
fn compiled_cli_rejects_direct_apply_from_the_unprivileged_phase() {
    let mut child = ProcessCommand::new(env!("CARGO_BIN_EXE_vonk-spark-setup"))
        .arg("__apply")
        .env_clear()
        .env("LANG", "C.UTF-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(&caller_bound_frame(rustix::process::geteuid().as_raw()))
        .unwrap();
    let output = child.wait_with_output().unwrap();

    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("wrong privilege phase"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn headless_upgrade_reaches_the_real_stdin_only_sudo_boundary() {
    if let Ok(root) = std::env::var(HELPER_ROOT) {
        if std::env::var_os(APPLY_HELPER).is_some() {
            run_apply_process(PathBuf::from(root));
        } else {
            run_headless_upgrade_process(PathBuf::from(root));
        }
        return;
    }
    let temporary = tempdir().unwrap();
    let output = ProcessCommand::new("/usr/bin/setsid")
        .arg(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "headless_upgrade_reaches_the_real_stdin_only_sudo_boundary",
        ])
        .env(HELPER_ROOT, temporary.path())
        .env("TMPDIR", temporary.path())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .unwrap();

    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let receipt = temporary.path().join("sudo-receipt");
    let argv = fs::read(format!("{}.argv", receipt.display())).unwrap();
    let environment = fs::read(format!("{}.env", receipt.display())).unwrap();
    let stdin = fs::read(format!("{}.stdin", receipt.display())).unwrap();
    assert!(
        argv.windows(b"__apply".len())
            .any(|part| part == b"__apply")
    );
    assert!(
        !argv
            .windows(b"pairing_token".len())
            .any(|part| part == b"pairing_token")
    );
    assert!(
        !environment
            .windows(b"pairing_token".len())
            .any(|part| part == b"pairing_token")
    );
    assert!(stdin.starts_with(b"VONK-SPARK-APPLY-V1\0"));

    let session = temporary.path().join("var/tmp/vonk-spark-setup.012345");
    fs::create_dir_all(&session).unwrap();
    fs::set_permissions(&session, fs::Permissions::from_mode(0o700)).unwrap();
    let executable = session.join("vonk-spark-setup");
    fs::copy(temporary.path().join("vonk-spark-setup"), &executable).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    let package = session.join("vonk-forge-agent.deb");
    fs::copy(temporary.path().join(package_filename()), &package).unwrap();
    fs::set_permissions(&package, fs::Permissions::from_mode(0o600)).unwrap();
    let mut apply = ProcessCommand::new("/usr/bin/setsid")
        .arg(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "headless_upgrade_reaches_the_real_stdin_only_sudo_boundary",
        ])
        .env(HELPER_ROOT, temporary.path())
        .env(APPLY_HELPER, "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    apply.stdin.take().unwrap().write_all(&stdin).unwrap();
    let apply = apply.wait_with_output().unwrap();
    assert!(
        apply.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&apply.stdout),
        String::from_utf8_lossy(&apply.stderr)
    );
    let commands: serde_json::Value =
        serde_json::from_slice(&fs::read(temporary.path().join("apply-commands.json")).unwrap())
            .unwrap();
    let commands = commands.as_array().unwrap();
    assert!(commands.iter().any(|command| {
        command["program"] == "/usr/bin/apt-get"
            && command["args"].as_array().is_some_and(|arguments| {
                arguments
                    .iter()
                    .any(|argument| argument.as_str() == Some("install"))
            })
    }));
    assert!(commands.iter().all(|command| {
        command["program"] != "/usr/bin/curl"
            && !command.to_string().contains("/dev/tty")
            && !command.to_string().contains(TOKEN)
    }));
}

#[test]
fn real_sudo_process_receives_the_pairing_token_only_in_the_bounded_stdin_frame() {
    const MODE: &str = "VONK_SPARK_FRESH_HANDOFF_HELPER";
    if std::env::var_os(MODE).is_some() {
        run_fresh_handoff_process(PathBuf::from(std::env::var(HELPER_ROOT).unwrap()));
        return;
    }
    let temporary = tempdir().unwrap();
    let output = ProcessCommand::new("/usr/bin/setsid")
        .arg(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "real_sudo_process_receives_the_pairing_token_only_in_the_bounded_stdin_frame",
        ])
        .env(HELPER_ROOT, temporary.path())
        .env(MODE, "1")
        .env("TMPDIR", temporary.path())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .unwrap();

    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let receipt = temporary.path().join("sudo-receipt");
    let argv = fs::read(format!("{}.argv", receipt.display())).unwrap();
    let environment = fs::read(format!("{}.env", receipt.display())).unwrap();
    let stdin = fs::read(format!("{}.stdin", receipt.display())).unwrap();
    assert!(
        !argv
            .windows(TOKEN.len())
            .any(|part| part == TOKEN.as_bytes())
    );
    assert!(
        !environment
            .windows(TOKEN.len())
            .any(|part| part == TOKEN.as_bytes())
    );
    assert!(
        stdin
            .windows(TOKEN.len())
            .any(|part| part == TOKEN.as_bytes())
    );
}
