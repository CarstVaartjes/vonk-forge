use std::{collections::VecDeque, fs, path::PathBuf, process::Command as ProcessCommand};

use sha2::{Digest, Sha256};
use tempfile::tempdir;
use vonk_spark_setup::{
    Command, CommandOutput, CommandRunner, InstallPaths, Prompt, SetupRequest, handoff_to_root,
    run_setup,
};

const TOKEN: &str = "A123456789012345678901234567890123456789012";
#[derive(Default)]
struct RecordingRunner {
    commands: Vec<Command>,
    outputs: VecDeque<CommandOutput>,
}

impl RecordingRunner {
    fn with_ca(ca: &[u8]) -> Self {
        Self {
            commands: Vec::new(),
            outputs: [
                CommandOutput::success(ca.to_vec()),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success(b"4242\n".to_vec()),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success(b"4242\n".to_vec()),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success(b"4242\n".to_vec()),
                CommandOutput::success_empty(),
            ]
            .into(),
        }
    }

    fn for_upgrade() -> Self {
        Self {
            commands: Vec::new(),
            outputs: [
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success(b"4242\n".to_vec()),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success(b"4242\n".to_vec()),
                CommandOutput::success_empty(),
                CommandOutput::success_empty(),
                CommandOutput::success(b"4242\n".to_vec()),
                CommandOutput::success_empty(),
            ]
            .into(),
        }
    }
}

impl CommandRunner for RecordingRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
        self.commands.push(command);
        Ok(self
            .outputs
            .pop_front()
            .unwrap_or_else(CommandOutput::success_empty))
    }

    fn sleep(&mut self, _duration: std::time::Duration) {}
}

struct Answers {
    values: VecDeque<String>,
}

impl Answers {
    fn fresh(ca: &[u8]) -> Self {
        let mut reader = std::io::BufReader::new(std::io::Cursor::new(ca));
        let certificate = rustls_pemfile::certs(&mut reader).next().unwrap().unwrap();
        Self {
            values: [
                "https://enroll.example.test/".to_owned(),
                "https://controller.example.test/".to_owned(),
                "https://download.example.test/controller-ca.pem".to_owned(),
                hex::encode(Sha256::digest(certificate.as_ref())),
            ]
            .into(),
        }
    }
}

impl Prompt for Answers {
    fn value(&mut self, _label: &str) -> Result<String, String> {
        self.values
            .pop_front()
            .ok_or_else(|| "unexpected prompt".to_owned())
    }

    fn secret(&mut self, _label: &str) -> Result<String, String> {
        Ok(TOKEN.to_owned())
    }
}

fn ca() -> Vec<u8> {
    rcgen::generate_simple_self_signed(vec!["controller.example.test".to_owned()])
        .unwrap()
        .cert
        .pem()
        .into_bytes()
}

fn package(path: &std::path::Path) {
    package_with_identity(path, "vonk-forge-agent", "1.0.0", "amd64");
}

fn package_with_identity(path: &std::path::Path, package: &str, version: &str, architecture: &str) {
    let root = path
        .parent()
        .unwrap()
        .join(format!("package-{package}-{architecture}"));
    fs::create_dir_all(root.join("DEBIAN")).unwrap();
    fs::write(
        root.join("DEBIAN/control"),
        format!(
            "Package: {package}\nVersion: {version}\nArchitecture: {architecture}\nMaintainer: test <test@example.test>\nDescription: test package\n"
        ),
    )
    .unwrap();
    let status = ProcessCommand::new("/usr/bin/dpkg-deb")
        .args(["--build", "--root-owner-group"])
        .arg(&root)
        .arg(path)
        .status()
        .unwrap();
    assert!(status.success());
}

fn request(package: PathBuf) -> SetupRequest {
    let digest = hex::encode(Sha256::digest(fs::read(&package).unwrap()));
    SetupRequest::new(
        package,
        digest,
        "1.0.0".to_owned(),
        "amd64".to_owned(),
        "b".repeat(64),
        PathBuf::from("/opt/vonk-spark-setup"),
    )
    .unwrap()
}

fn paths(root: &std::path::Path) -> InstallPaths {
    InstallPaths {
        config: root.join("etc/vonk-forge-agent/agent.toml"),
        ca: root.join("etc/vonk-forge-agent/controller-ca.pem"),
        agent: root.join("usr/lib/vonk-forge/vonk-agent"),
        upgrade: root.join("usr/bin/vonk-agent-upgrade"),
        service: "vonk-forge-agent.service".to_owned(),
        required_owner: None,
    }
}

fn paired_config(paths: &InstallPaths) -> String {
    format!(
        "enrollment_url = \"https://enroll.example.test/\"\ncontroller_url = \"https://controller.example.test/\"\nca_path = \"{}\"\nca_sha256 = \"{}\"\ndata_dir = \"/var/lib/vonk-forge-agent\"\nnode_id = \"spk_0123456789abcdef0123456789abcdef\"\npoll_min_seconds = 2\npoll_max_seconds = 60\n",
        paths.ca.display(),
        "a".repeat(64),
    )
}

#[test]
fn fresh_setup_stages_verified_package_before_sudo_and_pipes_pairing_token() {
    let temporary = tempdir().unwrap();
    let package_path = temporary.path().join("vonk-forge-agent_1.0.0_amd64.deb");
    package(&package_path);
    let ca = ca();
    fs::create_dir_all(paths(temporary.path()).config.parent().unwrap()).unwrap();
    let mut runner = RecordingRunner::with_ca(&ca);
    let mut prompt = Answers::fresh(&ca);

    let result = run_setup(
        &request(package_path),
        &paths(temporary.path()),
        &mut prompt,
        &mut runner,
    );

    assert!(result.is_ok(), "{result:?}");
    assert!(runner.commands[0].program.ends_with("curl"));
    let privileged = runner
        .commands
        .iter()
        .filter(|command| command.program != std::path::Path::new("/usr/bin/curl"))
        .collect::<Vec<_>>();
    assert!(
        privileged[0]
            .args
            .iter()
            .any(|argument| argument == "update")
    );
    assert!(
        privileged[1]
            .args
            .iter()
            .any(|argument| argument.ends_with("vonk-forge-agent_1.0.0_amd64.deb"))
    );
    assert!(
        privileged
            .iter()
            .all(|command| command.program != std::path::Path::new("/usr/bin/sudo")),
        "the privileged phase must consume root-owned artifacts without nested sudo"
    );
    assert!(
        privileged.iter().all(|command| !command
            .args
            .iter()
            .any(|argument| argument.ends_with("vonk-agent-upgrade"))),
        "a fresh install cannot invoke a helper that is not installed yet"
    );
    let pair = privileged
        .iter()
        .find(|command| command.args.iter().any(|argument| argument == "pair"))
        .unwrap();
    assert!(pair.args.iter().all(|argument| argument != TOKEN));
    assert_eq!(pair.stdin, format!("{TOKEN}\n").into_bytes());
    assert!(
        runner
            .commands
            .iter()
            .all(|command| command.env.values().all(|value| value != TOKEN))
    );
}

#[test]
fn changed_or_non_debian_package_fails_before_any_command_can_escalate() {
    let temporary = tempdir().unwrap();
    let package_path = temporary.path().join("vonk-forge-agent_1.0.0_amd64.deb");
    fs::write(&package_path, b"not a debian archive").unwrap();
    let digest = hex::encode(Sha256::digest(fs::read(&package_path).unwrap()));
    let request = SetupRequest::new(
        package_path,
        digest,
        "1.0.0".to_owned(),
        "amd64".to_owned(),
        "b".repeat(64),
        PathBuf::from("/opt/vonk-spark-setup"),
    )
    .unwrap();
    let mut runner = RecordingRunner::for_upgrade();
    let mut prompt = Answers::fresh(&ca());

    let result = run_setup(&request, &paths(temporary.path()), &mut prompt, &mut runner);

    assert!(result.is_err());
    assert!(
        runner.commands.is_empty(),
        "invalid package must stop before curl or sudo"
    );
}

#[test]
fn root_handoff_reverifies_root_owned_copies_before_privileged_setup() {
    let temporary = tempdir().unwrap();
    let package_path = temporary.path().join("vonk-forge-agent_1.0.0_amd64.deb");
    package(&package_path);
    let executable = temporary.path().join("vonk-spark-setup");
    fs::write(&executable, b"verified setup executable").unwrap();
    let executable_digest = hex::encode(Sha256::digest(b"verified setup executable"));
    let package_digest = hex::encode(Sha256::digest(fs::read(&package_path).unwrap()));
    let request = SetupRequest::new(
        package_path,
        package_digest,
        "1.0.0".to_owned(),
        "amd64".to_owned(),
        executable_digest.clone(),
        executable,
    )
    .unwrap();
    let mut runner = RecordingRunner::default();

    handoff_to_root(&request, &mut runner).unwrap();

    assert_eq!(runner.commands.len(), 1);
    let command = &runner.commands[0];
    assert_eq!(command.program, std::path::Path::new("/usr/bin/sudo"));
    assert_eq!(&command.args[..2], ["/bin/sh", "-ceu"]);
    assert!(command.args[2].contains("/usr/bin/install -o root -g root -m 0700"));
    assert!(command.args[2].contains("/usr/bin/sha256sum --check --status"));
    assert!(command.args[2].contains("--privileged"));
    assert!(
        command
            .args
            .iter()
            .any(|argument| argument == &executable_digest)
    );
}

#[test]
fn package_bytes_changed_after_release_digest_selection_never_reach_sudo() {
    let temporary = tempdir().unwrap();
    let package_path = temporary.path().join("vonk-forge-agent_1.0.0_amd64.deb");
    package(&package_path);
    let request = request(package_path.clone());
    fs::write(
        &package_path,
        [fs::read(&package_path).unwrap(), b"changed".to_vec()].concat(),
    )
    .unwrap();
    let mut runner = RecordingRunner::default();
    let mut prompt = Answers::fresh(&ca());

    let result = run_setup(&request, &paths(temporary.path()), &mut prompt, &mut runner);

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
}

#[test]
fn a_verified_archive_with_an_untrusted_package_name_never_reaches_sudo() {
    let temporary = tempdir().unwrap();
    let package_path = temporary.path().join("release.deb");
    package(&package_path);
    let mut runner = RecordingRunner::default();
    let mut prompt = Answers::fresh(&ca());

    let result = run_setup(
        &request(package_path),
        &paths(temporary.path()),
        &mut prompt,
        &mut runner,
    );

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
}

#[test]
fn existing_install_preserves_identity_and_resolves_dependencies_with_apt() {
    let temporary = tempdir().unwrap();
    let package_path = temporary.path().join("vonk-forge-agent_1.0.0_amd64.deb");
    package(&package_path);
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    fs::create_dir_all(install_paths.agent.parent().unwrap()).unwrap();
    fs::write(&install_paths.config, paired_config(&install_paths)).unwrap();
    fs::write(&install_paths.agent, "existing agent").unwrap();
    let mut runner = RecordingRunner::for_upgrade();
    let mut prompt = Answers::fresh(&ca());

    let result = run_setup(
        &request(package_path),
        &install_paths,
        &mut prompt,
        &mut runner,
    );

    assert!(result.is_ok(), "{result:?}");
    assert!(runner.commands.iter().any(|command| {
        command.program == std::path::Path::new("/usr/bin/apt-get")
            && command
                .args
                .iter()
                .any(|argument| argument.ends_with("vonk-forge-agent_1.0.0_amd64.deb"))
    }));
    let install = runner
        .commands
        .iter()
        .find(|command| {
            command.program == std::path::Path::new("/usr/bin/apt-get")
                && command.args.iter().any(|argument| argument == "install")
        })
        .unwrap();
    assert_eq!(
        install.env.get("DEBIAN_FRONTEND").map(String::as_str),
        Some("noninteractive")
    );
    assert!(
        install
            .args
            .iter()
            .any(|argument| argument == "Dpkg::Options::=--force-confold")
    );
    assert!(runner.commands.iter().all(|command| {
        !command
            .args
            .iter()
            .any(|argument| argument.ends_with("vonk-agent-upgrade"))
    }));
}

#[test]
fn actual_debian_metadata_must_match_the_selected_release_and_host() {
    let temporary = tempdir().unwrap();
    let package_path = temporary.path().join("vonk-forge-agent_1.0.0_amd64.deb");
    package_with_identity(&package_path, "vonk-forge-agent", "1.0.0", "arm64");
    let mut runner = RecordingRunner::default();
    let mut prompt = Answers::fresh(&ca());

    let result = run_setup(
        &request(package_path),
        &paths(temporary.path()),
        &mut prompt,
        &mut runner,
    );

    assert!(format!("{result:?}").contains("PackageIdentity"));
    assert!(
        runner.commands.is_empty(),
        "metadata mismatch must fail before prompts or commands"
    );
}

#[test]
fn installed_package_without_generated_configuration_retries_fresh_pairing() {
    let temporary = tempdir().unwrap();
    let package_path = temporary.path().join("vonk-forge-agent_1.0.0_amd64.deb");
    package(&package_path);
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.agent.parent().unwrap()).unwrap();
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    fs::write(&install_paths.agent, "unpaired agent").unwrap();
    let ca = ca();
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let mut runner = RecordingRunner::with_ca(&ca);
    let mut prompt = Answers::fresh(&ca);

    let result = run_setup(
        &request(package_path),
        &install_paths,
        &mut prompt,
        &mut runner,
    );

    assert!(result.is_ok(), "{result:?}");
    assert!(runner.commands[0].program.ends_with("curl"));
    let install = runner
        .commands
        .iter()
        .position(|command| {
            command
                .args
                .iter()
                .any(|argument| argument.ends_with("vonk-forge-agent_1.0.0_amd64.deb"))
        })
        .unwrap();
    let pair = runner
        .commands
        .iter()
        .position(|command| command.args.iter().any(|argument| argument == "pair"))
        .unwrap();
    assert!(
        install < pair,
        "an interrupted stock install must install the verified package before pairing"
    );
    assert!(runner.commands.iter().all(|command| {
        !command
            .args
            .iter()
            .any(|argument| argument.ends_with("vonk-agent-upgrade"))
    }));
}
