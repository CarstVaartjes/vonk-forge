#![forbid(unsafe_code)]

use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{Command as ProcessCommand, Stdio},
    thread,
    time::Duration,
};

use serde::Deserialize;
use sha2::{Digest, Sha256};
use tempfile::TempDir;
use thiserror::Error;
use url::Url;
use uuid::Uuid;

const MAX_PACKAGE_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const MAX_CA_BYTES: usize = 64 * 1024;
const CONFIG_PATH: &str = "/etc/vonk-forge-agent/agent.toml";
const CA_PATH: &str = "/etc/vonk-forge-agent/controller-ca.pem";
const AGENT_PATH: &str = "/usr/lib/vonk-forge/vonk-agent";
const UPGRADE_PATH: &str = "/usr/bin/vonk-agent-upgrade";
const SERVICE: &str = "vonk-forge-agent.service";
const DATA_DIR: &str = "/var/lib/vonk-forge-agent";
const FRAME_MAGIC: &[u8] = b"VONK-SPARK-CONFIG-V1\0";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Command {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub stdin: Vec<u8>,
}

impl Command {
    pub fn new(
        program: impl Into<PathBuf>,
        args: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        Self {
            program: program.into(),
            args: args.into_iter().map(Into::into).collect(),
            env: BTreeMap::new(),
            stdin: Vec::new(),
        }
    }

    pub fn with_stdin(mut self, stdin: Vec<u8>) -> Self {
        self.stdin = stdin;
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub success: bool,
    pub stdout: Vec<u8>,
}

impl CommandOutput {
    pub fn success(stdout: Vec<u8>) -> Self {
        Self {
            success: true,
            stdout,
        }
    }

    pub fn success_empty() -> Self {
        Self::success(Vec::new())
    }
}

pub trait CommandRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String>;

    fn sleep(&mut self, duration: Duration) {
        thread::sleep(duration);
    }
}

pub trait Prompt {
    fn value(&mut self, label: &str) -> Result<String, String>;
    fn secret(&mut self, label: &str) -> Result<String, String>;
}

#[derive(Debug, Clone)]
pub struct SetupRequest {
    package: PathBuf,
    expected_sha256: String,
    executable: PathBuf,
}

impl SetupRequest {
    pub fn new(
        package: PathBuf,
        expected_sha256: String,
        executable: PathBuf,
    ) -> Result<Self, SetupError> {
        if !package.is_absolute() || !executable.is_absolute() || !valid_sha256(&expected_sha256) {
            return Err(SetupError::UnsafeInput(
                "release-controlled setup arguments",
            ));
        }
        Ok(Self {
            package,
            expected_sha256,
            executable,
        })
    }
}

#[derive(Debug, Clone)]
pub struct InstallPaths {
    pub config: PathBuf,
    pub ca: PathBuf,
    pub agent: PathBuf,
    pub upgrade: PathBuf,
    pub service: String,
    pub required_owner: Option<u32>,
}

impl InstallPaths {
    pub fn system() -> Self {
        Self {
            config: PathBuf::from(CONFIG_PATH),
            ca: PathBuf::from(CA_PATH),
            agent: PathBuf::from(AGENT_PATH),
            upgrade: PathBuf::from(UPGRADE_PATH),
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
    #[error("existing installation is incomplete or unsafe")]
    ExistingInstall,
    #[error("interactive setup failed")]
    Prompt,
    #[error("controller CA is invalid or does not match its supplied SHA-256")]
    ControllerCa,
    #[error("privileged setup command failed: {0}")]
    Command(String),
    #[error("privileged configuration input is invalid")]
    PrivilegedInput,
    #[error("privileged configuration write failed")]
    PrivilegedWrite(#[source] io::Error),
}

pub fn run_setup(
    request: &SetupRequest,
    paths: &InstallPaths,
    prompt: &mut dyn Prompt,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    let staged = stage_verified_package(&request.package, &request.expected_sha256)?;
    match install_state(paths)? {
        InstallState::Existing => run_checked(
            runner,
            sudo(Command::new(
                &paths.upgrade,
                [
                    "install-local".to_owned(),
                    staged.path().display().to_string(),
                    request.expected_sha256.clone(),
                ],
            )),
        )
        .map(|_| ()),
        InstallState::Fresh => fresh_setup(request, paths, prompt, runner, &staged, true),
        InstallState::Placeholder => fresh_setup(request, paths, prompt, runner, &staged, false),
    }
}

fn fresh_setup(
    request: &SetupRequest,
    paths: &InstallPaths,
    prompt: &mut dyn Prompt,
    runner: &mut dyn CommandRunner,
    staged: &StagedPackage,
    install_before_pair: bool,
) -> Result<(), SetupError> {
    let enrollment = required_origin(prompt, "Enrollment URL")?;
    let controller = required_origin(prompt, "Controller URL")?;
    let ca_url = required_ca_url(prompt)?;
    let ca_sha256 = required_sha256(prompt, "Controller CA SHA-256")?;
    let pairing_token = prompt
        .secret("Pairing token")
        .map_err(|_| SetupError::Prompt)?;
    if !valid_token(&pairing_token) {
        return Err(SetupError::UnsafeInput("pairing token"));
    }

    let ca = run_checked(
        runner,
        Command::new(
            "/usr/bin/curl",
            [
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--max-filesize",
                "65536",
                ca_url.as_str(),
            ],
        ),
    )?
    .stdout;
    verify_ca(&ca, &ca_sha256)?;

    if install_before_pair {
        install_package(request, paths, runner, staged)?;
    }

    let config = GeneratedConfig {
        enrollment_url: enrollment,
        controller_url: controller,
        ca_sha256,
        node_id: format!("spk_{}", Uuid::new_v4().simple()),
    };
    let write = PrivilegedWrite {
        config: config.to_toml(),
        ca,
    };
    run_checked(
        runner,
        sudo(Command::new(&request.executable, ["--privileged-write"]).with_stdin(write.encode()?)),
    )?;

    let pair = Command::new(
        "/usr/bin/setpriv",
        [
            "--reuid",
            "vonk-agent",
            "--regid",
            "vonk-agent",
            "--clear-groups",
            "--",
            paths.agent.to_string_lossy().as_ref(),
            "--config",
            paths.config.to_string_lossy().as_ref(),
            "pair",
            "--enrollment",
            config.enrollment_url.as_str(),
            "--ca-sha256",
            &config.ca_sha256,
            "--token-stdin",
        ],
    )
    .with_stdin(format!("{pairing_token}\n").into_bytes());
    run_checked(runner, sudo(pair))?;
    if !install_before_pair {
        return install_package(request, paths, runner, staged);
    }
    run_checked(
        runner,
        sudo(Command::new(
            "/usr/bin/systemctl",
            ["enable", "--now", &paths.service],
        )),
    )?;
    run_checked(
        runner,
        sudo(Command::new(
            &paths.agent,
            [
                "--config",
                paths.config.to_string_lossy().as_ref(),
                "self-test",
            ],
        )),
    )?;
    verify_sustained_readiness(paths, runner)
}

fn install_package(
    request: &SetupRequest,
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    staged: &StagedPackage,
) -> Result<(), SetupError> {
    run_checked(
        runner,
        sudo(Command::new(
            &paths.upgrade,
            [
                "install-local".to_owned(),
                staged.path().display().to_string(),
                request.expected_sha256.clone(),
            ],
        )),
    )
    .map(|_| ())
}

fn sudo(command: Command) -> Command {
    let mut arguments = Vec::with_capacity(command.args.len() + 1);
    arguments.push(command.program.display().to_string());
    arguments.extend(command.args);
    Command::new("/usr/bin/sudo", arguments).with_stdin(command.stdin)
}

fn run_checked(
    runner: &mut dyn CommandRunner,
    command: Command,
) -> Result<CommandOutput, SetupError> {
    let name = command.program.display().to_string();
    let output = runner.run(command).map_err(SetupError::Command)?;
    if output.success {
        Ok(output)
    } else {
        Err(SetupError::Command(name))
    }
}

fn verify_sustained_readiness(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    let mut healthy = 0_u8;
    let mut pid = None;
    for attempt in 0..30 {
        let active = run_checked(
            runner,
            sudo(Command::new(
                "/usr/bin/systemctl",
                ["is-active", "--quiet", &paths.service],
            )),
        );
        let current_pid = active.and_then(|_| {
            run_checked(
                runner,
                sudo(Command::new(
                    "/usr/bin/systemctl",
                    ["show", "--property", "MainPID", "--value", &paths.service],
                )),
            )
        });
        match current_pid.and_then(|output| {
            let value = String::from_utf8(output.stdout)
                .map_err(|_| SetupError::Command("systemctl".to_owned()))?;
            let value = value.trim();
            if value
                .parse::<u32>()
                .ok()
                .filter(|value| *value != 0)
                .is_none()
            {
                return Err(SetupError::Command("systemctl".to_owned()));
            }
            Ok(value.to_owned())
        }) {
            Ok(current_pid) => {
                if pid.as_ref() != Some(&current_pid) {
                    healthy = 0;
                }
                pid = Some(current_pid.clone());
                let ready = run_checked(
                    runner,
                    sudo(Command::new(
                        &paths.agent,
                        [
                            "--config",
                            paths.config.to_string_lossy().as_ref(),
                            "verify-readiness",
                            "--receipt",
                            "/run/vonk-forge-agent/readiness.json",
                            "--pid",
                            &current_pid,
                            "--max-age-seconds",
                            "90",
                        ],
                    )),
                );
                if ready.is_ok() {
                    healthy += 1;
                } else {
                    healthy = 0;
                }
            }
            Err(_) => {
                healthy = 0;
                pid = None;
            }
        }
        if healthy >= 3 {
            return Ok(());
        }
        if attempt < 29 {
            runner.sleep(Duration::from_secs(2));
        }
    }
    Err(SetupError::Command(
        "controller readiness was not sustained".to_owned(),
    ))
}

enum InstallState {
    Fresh,
    Placeholder,
    Existing,
}

fn install_state(paths: &InstallPaths) -> Result<InstallState, SetupError> {
    let config = safe_existing_file(&paths.config, paths.required_owner)?;
    let agent = safe_existing_file(&paths.agent, paths.required_owner)?;
    match (config, agent) {
        (false, false) => Ok(InstallState::Fresh),
        (true, true) => match paired_configuration(&paths.config, paths)? {
            true => Ok(InstallState::Existing),
            false => Ok(InstallState::Placeholder),
        },
        _ => Err(SetupError::ExistingInstall),
    }
}

fn paired_configuration(path: &Path, paths: &InstallPaths) -> Result<bool, SetupError> {
    let raw = fs::read(path).map_err(|_| SetupError::ExistingInstall)?;
    if raw.len() > 64 * 1024 {
        return Err(SetupError::ExistingInstall);
    }
    let config: WrittenConfig = toml::from_slice(&raw).map_err(|_| SetupError::ExistingInstall)?;
    if packaged_placeholder(&config) {
        return Ok(false);
    }
    if !valid_written_config(&config, paths) {
        return Err(SetupError::ExistingInstall);
    }
    Ok(true)
}

fn packaged_placeholder(config: &WrittenConfig) -> bool {
    config.enrollment_url.as_str() == "https://enroll.vonkforge.invalid/"
        && config.controller_url.as_str() == "https://controller.vonkforge.invalid/"
        && config.ca_path == Path::new(CA_PATH)
        && config.ca_sha256 == "0".repeat(64)
        && config.data_dir == Path::new(DATA_DIR)
        && config.node_id == "spk_00000000000000000000000000000000"
        && config.poll_min_seconds == 2
        && config.poll_max_seconds == 60
}

fn safe_existing_file(path: &Path, required_owner: Option<u32>) -> Result<bool, SetupError> {
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

struct StagedPackage {
    _directory: TempDir,
    path: PathBuf,
}

impl StagedPackage {
    fn path(&self) -> &Path {
        &self.path
    }
}

fn stage_verified_package(source: &Path, expected: &str) -> Result<StagedPackage, SetupError> {
    if !source.is_absolute() || !valid_sha256(expected) || !valid_package_name(source) {
        return Err(SetupError::UnsafePackage);
    }
    let before = fs::symlink_metadata(source).map_err(|_| SetupError::UnsafePackage)?;
    if !before.file_type().is_file()
        || before.file_type().is_symlink()
        || before.nlink() != 1
        || before.len() < 68
        || before.len() > MAX_PACKAGE_BYTES
        || before.permissions().mode() & 0o022 != 0
    {
        return Err(SetupError::UnsafePackage);
    }
    let mut input = OpenOptions::new()
        .read(true)
        .custom_flags(libc_nofollow())
        .open(source)
        .map_err(|_| SetupError::UnsafePackage)?;
    let open = input.metadata().map_err(|_| SetupError::UnsafePackage)?;
    if !same_file(&before, &open) {
        return Err(SetupError::UnsafePackage);
    }
    let directory = tempfile::tempdir().map_err(SetupError::PrivilegedWrite)?;
    let path = directory
        .path()
        .join(source.file_name().ok_or(SetupError::UnsafePackage)?);
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .map_err(SetupError::PrivilegedWrite)?;
    let mut digest = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = input
            .read(&mut buffer)
            .map_err(|_| SetupError::UnsafePackage)?;
        if count == 0 {
            break;
        }
        total = total
            .checked_add(count as u64)
            .ok_or(SetupError::UnsafePackage)?;
        if total > MAX_PACKAGE_BYTES {
            return Err(SetupError::UnsafePackage);
        }
        digest.update(&buffer[..count]);
        output
            .write_all(&buffer[..count])
            .map_err(SetupError::PrivilegedWrite)?;
    }
    output.sync_all().map_err(SetupError::PrivilegedWrite)?;
    let after = input.metadata().map_err(|_| SetupError::UnsafePackage)?;
    if !same_file(&open, &after) || total != before.len() {
        return Err(SetupError::UnsafePackage);
    }
    if hex::encode(digest.finalize()) != expected {
        return Err(SetupError::PackageDigest);
    }
    verify_debian_archive(&path)?;
    Ok(StagedPackage {
        _directory: directory,
        path,
    })
}

fn valid_package_name(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    let Some(version) = name.strip_prefix("vonk-forge-agent_") else {
        return false;
    };
    let Some((version, architecture)) = version.rsplit_once('_') else {
        return false;
    };
    matches!(architecture, "amd64.deb" | "arm64.deb")
        && !version.is_empty()
        && version.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'+' | b'~' | b':' | b'-')
        })
}

fn libc_nofollow() -> i32 {
    0o400000
}

fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.dev() == right.dev()
        && left.ino() == right.ino()
        && left.nlink() == right.nlink()
        && left.len() == right.len()
        && left.mtime_nsec() == right.mtime_nsec()
        && left.ctime_nsec() == right.ctime_nsec()
}

fn verify_debian_archive(path: &Path) -> Result<(), SetupError> {
    let mut file = File::open(path).map_err(SetupError::PrivilegedWrite)?;
    let mut magic = [0_u8; 8];
    file.read_exact(&mut magic)
        .map_err(|_| SetupError::PackageFormat)?;
    if magic != *b"!<arch>\n" {
        return Err(SetupError::PackageFormat);
    }
    let mut header = [0_u8; 60];
    file.read_exact(&mut header)
        .map_err(|_| SetupError::PackageFormat)?;
    if &header[..16]
        .iter()
        .copied()
        .take_while(|byte| *byte != b' ')
        .collect::<Vec<_>>()
        != b"debian-binary/"
        || &header[58..] != b"`\n"
    {
        return Err(SetupError::PackageFormat);
    }
    let size = std::str::from_utf8(&header[48..58])
        .ok()
        .and_then(|value| value.trim().parse::<usize>().ok())
        .ok_or(SetupError::PackageFormat)?;
    if size != 4 {
        return Err(SetupError::PackageFormat);
    }
    let mut version = [0_u8; 4];
    file.read_exact(&mut version)
        .map_err(|_| SetupError::PackageFormat)?;
    if version != *b"2.0\n" {
        return Err(SetupError::PackageFormat);
    }
    Ok(())
}

fn required_origin(prompt: &mut dyn Prompt, label: &str) -> Result<Url, SetupError> {
    let value = prompt.value(label).map_err(|_| SetupError::Prompt)?;
    let url = Url::parse(&value).map_err(|_| SetupError::UnsafeInput("endpoint URL"))?;
    if valid_origin(&url) {
        Ok(url)
    } else {
        Err(SetupError::UnsafeInput("endpoint URL"))
    }
}

fn required_ca_url(prompt: &mut dyn Prompt) -> Result<Url, SetupError> {
    let value = prompt
        .value("Controller CA PEM URL")
        .map_err(|_| SetupError::Prompt)?;
    let url = Url::parse(&value).map_err(|_| SetupError::UnsafeInput("controller CA URL"))?;
    if url.scheme() == "https"
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none()
    {
        Ok(url)
    } else {
        Err(SetupError::UnsafeInput("controller CA URL"))
    }
}

fn required_sha256(prompt: &mut dyn Prompt, label: &str) -> Result<String, SetupError> {
    let value = prompt.value(label).map_err(|_| SetupError::Prompt)?;
    if valid_sha256(&value) {
        Ok(value)
    } else {
        Err(SetupError::UnsafeInput("SHA-256"))
    }
}

fn valid_origin(url: &Url) -> bool {
    url.scheme() == "https"
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
        && url.path() == "/"
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_token(value: &str) -> bool {
    value.len() == 43
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn verify_ca(ca: &[u8], expected: &str) -> Result<(), SetupError> {
    if ca.len() > MAX_CA_BYTES {
        return Err(SetupError::ControllerCa);
    }
    let mut reader = BufReader::new(ca);
    let certificate = rustls_pemfile::certs(&mut reader)
        .next()
        .ok_or(SetupError::ControllerCa)
        .and_then(|value| value.map_err(|_| SetupError::ControllerCa))?;
    if rustls_pemfile::certs(&mut reader).next().is_some()
        || hex::encode(Sha256::digest(certificate.as_ref())) != expected
    {
        return Err(SetupError::ControllerCa);
    }
    Ok(())
}

#[derive(Clone)]
struct GeneratedConfig {
    enrollment_url: Url,
    controller_url: Url,
    ca_sha256: String,
    node_id: String,
}

impl GeneratedConfig {
    fn to_toml(&self) -> String {
        format!(
            "enrollment_url = \"{}\"\ncontroller_url = \"{}\"\nca_path = \"{CA_PATH}\"\nca_sha256 = \"{}\"\ndata_dir = \"{DATA_DIR}\"\nnode_id = \"{}\"\npoll_min_seconds = 2\npoll_max_seconds = 60\n",
            self.enrollment_url, self.controller_url, self.ca_sha256, self.node_id,
        )
    }
}

struct PrivilegedWrite {
    config: String,
    ca: Vec<u8>,
}

impl PrivilegedWrite {
    fn encode(&self) -> Result<Vec<u8>, SetupError> {
        let config = self.config.as_bytes();
        if config.len() > 64 * 1024 || self.ca.len() > MAX_CA_BYTES {
            return Err(SetupError::PrivilegedInput);
        }
        let mut frame = Vec::with_capacity(FRAME_MAGIC.len() + 8 + config.len() + self.ca.len());
        frame.extend_from_slice(FRAME_MAGIC);
        frame.extend_from_slice(&(config.len() as u32).to_be_bytes());
        frame.extend_from_slice(config);
        frame.extend_from_slice(&(self.ca.len() as u32).to_be_bytes());
        frame.extend_from_slice(&self.ca);
        Ok(frame)
    }

    fn decode(input: impl Read) -> Result<Self, SetupError> {
        let mut raw = Vec::new();
        input
            .take((FRAME_MAGIC.len() + 8 + 64 * 1024 + MAX_CA_BYTES + 1) as u64)
            .read_to_end(&mut raw)
            .map_err(SetupError::PrivilegedWrite)?;
        if raw.len() < FRAME_MAGIC.len() + 8 || !raw.starts_with(FRAME_MAGIC) {
            return Err(SetupError::PrivilegedInput);
        }
        let mut cursor = FRAME_MAGIC.len();
        let config = take_frame(&raw, &mut cursor, 64 * 1024)?;
        let ca = take_frame(&raw, &mut cursor, MAX_CA_BYTES)?;
        if cursor != raw.len() {
            return Err(SetupError::PrivilegedInput);
        }
        Ok(Self {
            config: String::from_utf8(config).map_err(|_| SetupError::PrivilegedInput)?,
            ca,
        })
    }
}

fn take_frame(raw: &[u8], cursor: &mut usize, maximum: usize) -> Result<Vec<u8>, SetupError> {
    let length = raw
        .get(*cursor..*cursor + 4)
        .ok_or(SetupError::PrivilegedInput)?;
    *cursor += 4;
    let length =
        u32::from_be_bytes(length.try_into().map_err(|_| SetupError::PrivilegedInput)?) as usize;
    if length > maximum {
        return Err(SetupError::PrivilegedInput);
    }
    let value = raw
        .get(*cursor..*cursor + length)
        .ok_or(SetupError::PrivilegedInput)?
        .to_vec();
    *cursor += length;
    Ok(value)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WrittenConfig {
    enrollment_url: Url,
    controller_url: Url,
    ca_path: PathBuf,
    ca_sha256: String,
    data_dir: PathBuf,
    node_id: String,
    poll_min_seconds: u64,
    poll_max_seconds: u64,
}

pub fn privileged_write_from(input: impl Read, paths: &InstallPaths) -> Result<(), SetupError> {
    if rustix::process::geteuid().as_raw() != 0 {
        return Err(SetupError::PrivilegedInput);
    }
    write_privileged_as(input, paths, 0)
}

fn write_privileged_as(
    input: impl Read,
    paths: &InstallPaths,
    required_owner: u32,
) -> Result<(), SetupError> {
    let write = PrivilegedWrite::decode(input)?;
    let config: WrittenConfig =
        toml::from_str(&write.config).map_err(|_| SetupError::PrivilegedInput)?;
    if !valid_written_config(&config, paths) {
        return Err(SetupError::PrivilegedInput);
    }
    verify_ca(&write.ca, &config.ca_sha256)?;
    atomic_root_write(&paths.ca, &write.ca, required_owner, 0o644)?;
    atomic_root_write(
        &paths.config,
        write.config.as_bytes(),
        required_owner,
        0o644,
    )?;
    Ok(())
}

fn valid_written_config(config: &WrittenConfig, paths: &InstallPaths) -> bool {
    valid_origin(&config.enrollment_url)
        && valid_origin(&config.controller_url)
        && config.ca_path == paths.ca
        && config.data_dir == Path::new(DATA_DIR)
        && valid_sha256(&config.ca_sha256)
        && valid_node_id(&config.node_id)
        && config.poll_min_seconds == 2
        && config.poll_max_seconds == 60
}

fn valid_node_id(value: &str) -> bool {
    value.len() == 36
        && value.starts_with("spk_")
        && value[4..]
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn atomic_root_write(
    path: &Path,
    bytes: &[u8],
    required_owner: u32,
    mode: u32,
) -> Result<(), SetupError> {
    let parent = path.parent().ok_or(SetupError::PrivilegedInput)?;
    let directory = fs::symlink_metadata(parent).map_err(SetupError::PrivilegedWrite)?;
    if !directory.file_type().is_dir()
        || directory.file_type().is_symlink()
        || directory.uid() != required_owner
        || directory.permissions().mode() & 0o022 != 0
    {
        return Err(SetupError::PrivilegedInput);
    }
    if let Ok(existing) = fs::symlink_metadata(path)
        && (!existing.file_type().is_file()
            || existing.file_type().is_symlink()
            || existing.uid() != required_owner
            || existing.permissions().mode() & 0o022 != 0)
    {
        return Err(SetupError::PrivilegedInput);
    }
    let temporary = parent.join(format!(
        ".vonk-spark-setup-{}-{}.new",
        std::process::id(),
        Uuid::new_v4().simple()
    ));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .open(&temporary)
        .map_err(SetupError::PrivilegedWrite)?;
    file.set_permissions(fs::Permissions::from_mode(mode))
        .map_err(SetupError::PrivilegedWrite)?;
    file.write_all(bytes).map_err(SetupError::PrivilegedWrite)?;
    file.sync_all().map_err(SetupError::PrivilegedWrite)?;
    fs::rename(&temporary, path).map_err(SetupError::PrivilegedWrite)?;
    File::open(parent)
        .and_then(|directory| directory.sync_all())
        .map_err(SetupError::PrivilegedWrite)
}

pub struct SystemCommandRunner;

impl CommandRunner for SystemCommandRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
        let mut process = ProcessCommand::new(&command.program);
        process
            .args(&command.args)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .envs(command.env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut child = process
            .spawn()
            .map_err(|_| command.program.display().to_string())?;
        child
            .stdin
            .take()
            .ok_or_else(|| command.program.display().to_string())?
            .write_all(&command.stdin)
            .map_err(|_| command.program.display().to_string())?;
        let output = child
            .wait_with_output()
            .map_err(|_| command.program.display().to_string())?;
        Ok(CommandOutput {
            success: output.status.success(),
            stdout: output.stdout,
        })
    }
}

pub struct TtyPrompt {
    reader: BufReader<File>,
    tty: File,
}

impl TtyPrompt {
    pub fn open() -> Result<Self, SetupError> {
        let tty = OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/tty")
            .map_err(SetupError::PrivilegedWrite)?;
        let reader = BufReader::new(tty.try_clone().map_err(SetupError::PrivilegedWrite)?);
        Ok(Self { reader, tty })
    }
}

impl Prompt for TtyPrompt {
    fn value(&mut self, label: &str) -> Result<String, String> {
        write!(self.tty, "{label}: ").map_err(|_| "tty output failed".to_owned())?;
        self.tty
            .flush()
            .map_err(|_| "tty output failed".to_owned())?;
        let mut value = String::new();
        if self
            .reader
            .read_line(&mut value)
            .map_err(|_| "tty input failed".to_owned())?
            == 0
        {
            return Err("tty input ended".to_owned());
        }
        Ok(value.trim().to_owned())
    }

    fn secret(&mut self, label: &str) -> Result<String, String> {
        rpassword::prompt_password(format!("{label}: "))
            .map_err(|_| "tty secret input failed".to_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn valid_write(paths: &InstallPaths) -> Vec<u8> {
        let certificate =
            rcgen::generate_simple_self_signed(vec!["controller.example.test".to_owned()])
                .unwrap()
                .cert
                .pem()
                .into_bytes();
        let mut reader = BufReader::new(certificate.as_slice());
        let der = rustls_pemfile::certs(&mut reader).next().unwrap().unwrap();
        PrivilegedWrite {
            config: format!(
                "enrollment_url = \"https://enroll.example.test/\"\ncontroller_url = \"https://controller.example.test/\"\nca_path = \"{}\"\nca_sha256 = \"{}\"\ndata_dir = \"{DATA_DIR}\"\nnode_id = \"spk_0123456789abcdef0123456789abcdef\"\npoll_min_seconds = 2\npoll_max_seconds = 60\n",
                paths.ca.display(),
                hex::encode(Sha256::digest(der.as_ref())),
            ),
            ca: certificate,
        }
        .encode()
        .unwrap()
    }

    #[test]
    fn failed_ca_write_never_replaces_the_configuration() {
        let temporary = tempfile::tempdir().unwrap();
        let config = temporary.path().join("etc/vonk-forge-agent/agent.toml");
        let ca = temporary.path().join("unsafe-ca/controller-ca.pem");
        fs::create_dir_all(config.parent().unwrap()).unwrap();
        std::os::unix::fs::symlink(temporary.path().join("outside"), ca.parent().unwrap()).unwrap();
        let paths = InstallPaths {
            config: config.clone(),
            ca: ca.clone(),
            agent: temporary.path().join("agent"),
            upgrade: temporary.path().join("upgrade"),
            service: SERVICE.to_owned(),
            required_owner: None,
        };
        let frame = valid_write(&paths);

        let result = write_privileged_as(
            frame.as_slice(),
            &paths,
            rustix::process::geteuid().as_raw(),
        );

        assert!(result.is_err());
        assert!(
            !config.exists(),
            "CA failure must not leave a new configuration behind"
        );
    }

    #[test]
    fn privileged_writer_keeps_agent_configuration_and_ca_root_owned_but_readable() {
        let temporary = tempfile::tempdir().unwrap();
        let paths = InstallPaths {
            config: temporary.path().join("etc/vonk-forge-agent/agent.toml"),
            ca: temporary
                .path()
                .join("etc/vonk-forge-agent/controller-ca.pem"),
            agent: temporary.path().join("agent"),
            upgrade: temporary.path().join("upgrade"),
            service: SERVICE.to_owned(),
            required_owner: None,
        };
        fs::create_dir_all(paths.config.parent().unwrap()).unwrap();
        let frame = valid_write(&paths);

        write_privileged_as(
            frame.as_slice(),
            &paths,
            rustix::process::geteuid().as_raw(),
        )
        .unwrap();

        assert_eq!(
            fs::metadata(&paths.config).unwrap().permissions().mode() & 0o777,
            0o644
        );
        assert_eq!(
            fs::metadata(&paths.ca).unwrap().permissions().mode() & 0o777,
            0o644
        );
    }
}
