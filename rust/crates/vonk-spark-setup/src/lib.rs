#![forbid(unsafe_code)]

use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    net::Ipv4Addr,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{Command as ProcessCommand, Stdio},
    thread,
    time::Duration,
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tempfile::TempDir;
use thiserror::Error;
use url::Url;
use uuid::Uuid;

const MAX_PACKAGE_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const MAX_CA_BYTES: usize = 64 * 1024;
const MAX_BOOTSTRAP_BYTES: usize = 128 * 1024;
const MAX_RELEASE_BYTES: usize = 1024 * 1024;
const MAX_RELEASE_SIGNATURE_BYTES: usize = 16 * 1024;
const CONFIG_PATH: &str = "/etc/vonk-forge-agent/agent.toml";
const CA_PATH: &str = "/etc/vonk-forge-agent/controller-ca.pem";
const FIREWALL_CONFIG_PATH: &str = "/etc/vonk-forge-agent/docker-firewall.conf";
const HELPER_AUTHORITY_PATH: &str = "/etc/vonk-forge-agent/host-helper-authority.pub";
const HOSTS_PATH: &str = "/etc/hosts";
const AGENT_PATH: &str = "/usr/lib/vonk-forge/vonk-agent";
const SERVICE: &str = "vonk-forge-agent.service";
const HELPER_SOCKET: &str = "vonk-forge-package-helper.socket";
const FIREWALL_SERVICE: &str = "vonk-forge-docker-firewall.service";
const DATA_DIR: &str = "/var/lib/vonk-forge-agent";
const DEFAULT_ENDPOINT_HOST_PORTS: &str = "8000,8101";
const DEFAULT_HOST_ENDPOINT_PORTS: &str = "8888";
const DEFAULT_RENDEZVOUS_PORT: &str = "29500";
const DEFAULT_FABRIC_BANDWIDTH_MBPS: &str = "200000";
const APPLY_FRAME_MAGIC: &[u8] = b"VONK-SPARK-APPLY-V1\0";
const MAX_APPLY_FRAME_BYTES: usize = 2 * 1024 * 1024;
const INSTALLER_RELEASE_PUBLIC_KEY: &[u8] =
    include_bytes!("../../../../install/installer-release-public.pem");
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Command {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub stdin: Vec<u8>,
    pub stderr: CommandStderr,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CommandStderr {
    Inherit,
    Suppress,
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
            stderr: CommandStderr::Inherit,
        }
    }

    pub fn with_stdin(mut self, stdin: Vec<u8>) -> Self {
        self.stdin = stdin;
        self
    }

    pub fn with_env(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.env.insert(name.into(), value.into());
        self
    }

    pub fn suppress_stderr(mut self) -> Self {
        self.stderr = CommandStderr::Suppress;
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
    release_manifest: PathBuf,
    release_signature: PathBuf,
    setup_signature: PathBuf,
    executable: PathBuf,
    controller_address: Option<Ipv4Addr>,
    firewall_inputs: FirewallInputs,
}

#[derive(Debug, Clone, Default)]
pub struct FirewallInputs {
    nas_management_ip: Option<String>,
    node_management_ip: Option<String>,
    node_fabric_ip: Option<String>,
    peer_fabric_ip: Option<String>,
    endpoint_host_ports: Option<String>,
    host_endpoint_ports: Option<String>,
    rendezvous_port: Option<String>,
    fabric_bandwidth_mbps: Option<String>,
}

impl FirewallInputs {
    pub fn from_environment() -> Result<Self, SetupError> {
        fn optional(name: &'static str) -> Result<Option<String>, SetupError> {
            match std::env::var(name) {
                Ok(value) => Ok(Some(value)),
                Err(std::env::VarError::NotPresent) => Ok(None),
                Err(std::env::VarError::NotUnicode(_)) => Err(SetupError::UnsafeInput(name)),
            }
        }

        Ok(Self {
            nas_management_ip: optional("VONK_NAS_MANAGEMENT_IP")?,
            node_management_ip: optional("VONK_NODE_MANAGEMENT_IP")?,
            node_fabric_ip: optional("VONK_NODE_FABRIC_IP")?,
            peer_fabric_ip: optional("VONK_PEER_FABRIC_IP")?,
            endpoint_host_ports: optional("VONK_ENDPOINT_HOST_PORTS")?,
            host_endpoint_ports: optional("VONK_HOST_ENDPOINT_PORTS")?,
            rendezvous_port: optional("VONK_RENDEZVOUS_PORT")?,
            fabric_bandwidth_mbps: optional("VONK_FABRIC_BANDWIDTH_MBPS")?,
        })
    }
}

#[derive(Debug, Clone)]
pub struct ReleaseAuthority {
    public_key_pem: Vec<u8>,
}

impl ReleaseAuthority {
    pub fn canonical() -> Self {
        Self {
            public_key_pem: INSTALLER_RELEASE_PUBLIC_KEY.to_vec(),
        }
    }

    pub fn from_pem(public_key_pem: Vec<u8>) -> Result<Self, SetupError> {
        if public_key_pem.is_empty()
            || public_key_pem.len() > 16 * 1024
            || !public_key_pem.starts_with(b"-----BEGIN PUBLIC KEY-----\n")
            || !public_key_pem.ends_with(b"-----END PUBLIC KEY-----\n")
        {
            return Err(SetupError::ReleaseSignature);
        }
        Ok(Self { public_key_pem })
    }

    fn verify(&self, manifest: &[u8], encoded_signature: &[u8]) -> Result<(), SetupError> {
        self.verify_bounded(manifest, encoded_signature, MAX_RELEASE_BYTES)
    }

    fn verify_setup(&self, setup: &[u8], encoded_signature: &[u8]) -> Result<(), SetupError> {
        self.verify_bounded(setup, encoded_signature, 64 * 1024 * 1024)
    }

    fn verify_bounded(
        &self,
        payload: &[u8],
        encoded_signature: &[u8],
        maximum_payload: usize,
    ) -> Result<(), SetupError> {
        if payload.is_empty()
            || payload.len() > maximum_payload
            || encoded_signature.is_empty()
            || encoded_signature.len() > MAX_RELEASE_SIGNATURE_BYTES
            || !encoded_signature.ends_with(b"\n")
            || encoded_signature[..encoded_signature.len() - 1].contains(&b'\n')
        {
            return Err(SetupError::ReleaseSignature);
        }
        let directory = secure_tempdir("vonk-spark-release.")?;
        let key = directory.path().join("release-public.pem");
        let claims = directory.path().join("signed-payload");
        let encoded_signature_path = directory.path().join("release.sig.b64");
        let signature_path = directory.path().join("release.sig");
        fs::write(&key, &self.public_key_pem).map_err(SetupError::PrivilegedWrite)?;
        fs::write(&claims, payload).map_err(SetupError::PrivilegedWrite)?;
        fs::write(&encoded_signature_path, encoded_signature)
            .map_err(SetupError::PrivilegedWrite)?;
        let decoded = ProcessCommand::new("/usr/bin/openssl")
            .args(["base64", "-d", "-A", "-in"])
            .arg(&encoded_signature_path)
            .args(["-out"])
            .arg(&signature_path)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map_err(|_| SetupError::ReleaseSignature)?;
        let decoded_size = fs::metadata(&signature_path)
            .map_err(|_| SetupError::ReleaseSignature)?
            .len();
        if !decoded.success() || decoded_size == 0 || decoded_size > 1024 {
            return Err(SetupError::ReleaseSignature);
        }
        let status = ProcessCommand::new("/usr/bin/openssl")
            .args(["dgst", "-sha256", "-verify"])
            .arg(&key)
            .args(["-signature"])
            .arg(&signature_path)
            .arg(&claims)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map_err(|_| SetupError::ReleaseSignature)?;
        if status.success() {
            Ok(())
        } else {
            Err(SetupError::ReleaseSignature)
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CallerIdentity {
    effective_uid: u32,
    sudo_uid: Option<u32>,
}

impl CallerIdentity {
    pub fn unprivileged(uid: u32) -> Self {
        Self {
            effective_uid: uid,
            sudo_uid: None,
        }
    }

    pub fn sudo_root(uid: u32) -> Self {
        Self {
            effective_uid: 0,
            sudo_uid: Some(uid),
        }
    }

    pub fn direct_root() -> Self {
        Self {
            effective_uid: 0,
            sudo_uid: None,
        }
    }

    pub fn current() -> Result<Self, SetupError> {
        let effective_uid = rustix::process::geteuid().as_raw();
        let sudo_uid = std::env::var("SUDO_UID")
            .ok()
            .map(|value| value.parse::<u32>())
            .transpose()
            .map_err(|_| SetupError::CallerPhase)?;
        Ok(Self {
            effective_uid,
            sudo_uid,
        })
    }

    pub fn ensure_public(self) -> Result<(), SetupError> {
        self.require_unprivileged().map(|_| ())
    }

    fn require_unprivileged(self) -> Result<u32, SetupError> {
        if self.effective_uid == 0 || self.sudo_uid.is_some() {
            return Err(SetupError::CallerPhase);
        }
        Ok(self.effective_uid)
    }

    fn require_sudo_root(self, expected_uid: u32) -> Result<(), SetupError> {
        if self.effective_uid != 0 || expected_uid == 0 || self.sudo_uid != Some(expected_uid) {
            return Err(SetupError::CallerPhase);
        }
        Ok(())
    }

    fn authenticate_for(self, paths: &InstallPaths) -> Result<(), SetupError> {
        if paths.required_owner.is_some() && self != Self::current()? {
            return Err(SetupError::CallerPhase);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "operation", rename_all = "kebab-case", deny_unknown_fields)]
enum ApplyOperation {
    Fresh {
        enrollment_url: Box<Url>,
        controller_url: Box<Url>,
        ca_sha256: String,
        ca_pem: Vec<u8>,
        node_id: String,
        pairing_token: String,
        host_mapping: Option<HostMapping>,
        firewall: FirewallConfig,
        helper_authority: Vec<u8>,
    },
    Pair {
        enrollment_url: Url,
        ca_sha256: String,
        pairing_token: String,
    },
    Upgrade,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct HostMapping {
    address: String,
    hostnames: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct EnrollmentDiscovery {
    controller_url: Url,
    ca_pem: Vec<u8>,
    host_mapping: Option<HostMapping>,
    helper_authority: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FirewallConfig {
    nas_management_ip: Ipv4Addr,
    node_management_ip: Ipv4Addr,
    node_fabric_ip: Ipv4Addr,
    peer_fabric_ip: Ipv4Addr,
    endpoint_host_ports: Vec<u16>,
    host_endpoint_ports: Vec<u16>,
    rendezvous_port: u16,
    fabric_bandwidth_mbps: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ApplyEnvelope {
    schema_version: u8,
    caller_uid: u32,
    release_manifest: Vec<u8>,
    release_signature: Vec<u8>,
    plan: ApplyOperation,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReleaseDocument {
    #[serde(default)]
    acceptance_only: bool,
    artifacts: BTreeMap<String, ReleaseArtifact>,
    bootstraps: BTreeMap<String, ReleaseArtifact>,
    channel: String,
    generation: String,
    images: BTreeMap<String, String>,
    schema_version: u8,
    source_sha: String,
    version: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReleaseArtifact {
    path: String,
    sha256: String,
    size: u64,
}

struct VerifiedRelease {
    raw: Vec<u8>,
    signature: Vec<u8>,
    package: ReleaseArtifact,
    setup: ReleaseArtifact,
    setup_signature: ReleaseArtifact,
    version: String,
    architecture: String,
}

pub struct PreparedSetup {
    executable: PathBuf,
    setup_signature: PathBuf,
    sudo: PathBuf,
    staging_root: PathBuf,
    required_owner: Option<u32>,
    staged: StagedPackage,
    frame: Vec<u8>,
}

impl PreparedSetup {
    pub fn package_path(&self) -> &Path {
        self.staged.path()
    }

    pub fn executable_path(&self) -> &Path {
        &self.executable
    }
}

impl SetupRequest {
    pub fn from_signed_release(
        package: PathBuf,
        release_manifest: PathBuf,
        release_signature: PathBuf,
        setup_signature: PathBuf,
        executable: PathBuf,
    ) -> Result<Self, SetupError> {
        if !package.is_absolute()
            || !release_manifest.is_absolute()
            || !release_signature.is_absolute()
            || !setup_signature.is_absolute()
            || !executable.is_absolute()
        {
            return Err(SetupError::UnsafeInput(
                "release-controlled setup arguments",
            ));
        }
        Ok(Self {
            package,
            release_manifest,
            release_signature,
            setup_signature,
            executable,
            controller_address: None,
            firewall_inputs: FirewallInputs::default(),
        })
    }

    pub fn with_controller_address(mut self, value: Option<&str>) -> Result<Self, SetupError> {
        self.controller_address = value
            .map(|value| {
                value
                    .parse::<Ipv4Addr>()
                    .map_err(|_| SetupError::UnsafeInput("controller network address"))
            })
            .transpose()?;
        Ok(self)
    }

    pub fn with_firewall_inputs(mut self, inputs: FirewallInputs) -> Self {
        self.firewall_inputs = inputs;
        self
    }
}

impl FirewallConfig {
    fn collect(
        inputs: &FirewallInputs,
        controller_address: Option<Ipv4Addr>,
        prompt: &mut dyn Prompt,
    ) -> Result<Self, SetupError> {
        fn supplied_or_prompted(
            supplied: Option<&String>,
            prompt: &mut dyn Prompt,
            label: &'static str,
        ) -> Result<String, SetupError> {
            supplied
                .cloned()
                .map(Ok)
                .unwrap_or_else(|| prompt.value(label).map_err(|_| SetupError::Prompt))
        }

        fn ipv4(value: String, field: &'static str) -> Result<Ipv4Addr, SetupError> {
            let address = value
                .parse::<Ipv4Addr>()
                .map_err(|_| SetupError::UnsafeInput(field))?;
            if !valid_site_ipv4(address) {
                return Err(SetupError::UnsafeInput(field));
            }
            Ok(address)
        }

        fn port(value: &str, field: &'static str) -> Result<u16, SetupError> {
            let parsed = value
                .parse::<u16>()
                .map_err(|_| SetupError::UnsafeInput(field))?;
            if parsed < 1024 || parsed.to_string() != value {
                return Err(SetupError::UnsafeInput(field));
            }
            Ok(parsed)
        }

        fn ports(value: &str, field: &'static str, required: bool) -> Result<Vec<u16>, SetupError> {
            if value.is_empty() {
                return if required {
                    Err(SetupError::UnsafeInput(field))
                } else {
                    Ok(Vec::new())
                };
            }
            let parsed = value
                .split(',')
                .map(|value| port(value, field))
                .collect::<Result<Vec<_>, _>>()?;
            let mut unique = parsed.clone();
            unique.sort_unstable();
            unique.dedup();
            if unique.len() != parsed.len() {
                return Err(SetupError::UnsafeInput(field));
            }
            Ok(parsed)
        }

        let nas_management_ip = inputs
            .nas_management_ip
            .clone()
            .or_else(|| controller_address.map(|value| value.to_string()))
            .map(Ok)
            .unwrap_or_else(|| {
                prompt
                    .value("NAS management IPv4 address")
                    .map_err(|_| SetupError::Prompt)
            })?;
        let endpoint_host_ports = inputs
            .endpoint_host_ports
            .as_deref()
            .unwrap_or(DEFAULT_ENDPOINT_HOST_PORTS);
        let host_endpoint_ports = inputs
            .host_endpoint_ports
            .as_deref()
            .unwrap_or(DEFAULT_HOST_ENDPOINT_PORTS);
        let rendezvous_port = inputs
            .rendezvous_port
            .as_deref()
            .unwrap_or(DEFAULT_RENDEZVOUS_PORT);
        let fabric_bandwidth_mbps = inputs
            .fabric_bandwidth_mbps
            .as_deref()
            .unwrap_or(DEFAULT_FABRIC_BANDWIDTH_MBPS)
            .parse::<u64>()
            .map_err(|_| SetupError::UnsafeInput("fabric bandwidth"))?;
        let config = Self {
            nas_management_ip: ipv4(nas_management_ip, "NAS management address")?,
            node_management_ip: ipv4(
                supplied_or_prompted(
                    inputs.node_management_ip.as_ref(),
                    prompt,
                    "Spark management IPv4 address",
                )?,
                "Spark management address",
            )?,
            node_fabric_ip: ipv4(
                supplied_or_prompted(
                    inputs.node_fabric_ip.as_ref(),
                    prompt,
                    "Spark fabric IPv4 address",
                )?,
                "Spark fabric address",
            )?,
            peer_fabric_ip: ipv4(
                supplied_or_prompted(
                    inputs.peer_fabric_ip.as_ref(),
                    prompt,
                    "Peer Spark fabric IPv4 address",
                )?,
                "peer Spark fabric address",
            )?,
            endpoint_host_ports: ports(endpoint_host_ports, "endpoint host ports", true)?,
            host_endpoint_ports: ports(host_endpoint_ports, "host endpoint ports", false)?,
            rendezvous_port: port(rendezvous_port, "rendezvous port")?,
            fabric_bandwidth_mbps,
        };
        if !config.valid() {
            return Err(SetupError::UnsafeInput("Spark network topology"));
        }
        Ok(config)
    }

    fn valid(&self) -> bool {
        let addresses = [
            self.nas_management_ip,
            self.node_management_ip,
            self.node_fabric_ip,
            self.peer_fabric_ip,
        ];
        addresses.iter().all(|address| valid_site_ipv4(*address))
            && addresses
                .iter()
                .enumerate()
                .all(|(index, address)| !addresses[index + 1..].contains(address))
            && !self.endpoint_host_ports.is_empty()
            && valid_unique_ports(&self.endpoint_host_ports)
            && valid_unique_ports(&self.host_endpoint_ports)
            && !self.endpoint_host_ports.contains(&self.rendezvous_port)
            && !self.host_endpoint_ports.contains(&self.rendezvous_port)
            && self.rendezvous_port >= 1024
            && (1..=1_000_000).contains(&self.fabric_bandwidth_mbps)
    }

    fn render(&self) -> String {
        let ports = |values: &[u16]| {
            values
                .iter()
                .map(u16::to_string)
                .collect::<Vec<_>>()
                .join(",")
        };
        format!(
            "VONK_NAS_MANAGEMENT_IP={}\nVONK_NODE_MANAGEMENT_IP={}\nVONK_NODE_FABRIC_IP={}\nVONK_PEER_FABRIC_IP={}\nVONK_ENDPOINT_HOST_PORTS={}\nVONK_HOST_ENDPOINT_PORTS={}\nVONK_RENDEZVOUS_PORT={}\n",
            self.nas_management_ip,
            self.node_management_ip,
            self.node_fabric_ip,
            self.peer_fabric_ip,
            ports(&self.endpoint_host_ports),
            ports(&self.host_endpoint_ports),
            self.rendezvous_port,
        )
    }
}

fn valid_site_ipv4(value: Ipv4Addr) -> bool {
    !value.is_unspecified()
        && !value.is_loopback()
        && !value.is_link_local()
        && !value.is_multicast()
        && !value.is_broadcast()
}

fn valid_unique_ports(values: &[u16]) -> bool {
    values.iter().all(|value| *value >= 1024)
        && values
            .iter()
            .enumerate()
            .all(|(index, value)| !values[index + 1..].contains(value))
}

pub fn handoff_to_root(
    prepared: &PreparedSetup,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    handoff_to_root_with_authority(prepared, runner, &ReleaseAuthority::canonical())
}

pub fn handoff_to_root_with_authority(
    prepared: &PreparedSetup,
    runner: &mut dyn CommandRunner,
    authority: &ReleaseAuthority,
) -> Result<(), SetupError> {
    let root_handoff = root_handoff_script(prepared, authority)?;
    let command = Command::new(
        &prepared.sudo,
        [
            "/bin/sh".to_owned(),
            "-ceu".to_owned(),
            root_handoff,
            "vonk-spark-root-handoff".to_owned(),
            prepared.executable.display().to_string(),
            prepared.staged.path().display().to_string(),
            prepared.setup_signature.display().to_string(),
        ],
    )
    .with_stdin(prepared.frame.clone());
    run_checked(runner, command).map(|_| ())
}

fn root_handoff_script(
    prepared: &PreparedSetup,
    authority: &ReleaseAuthority,
) -> Result<String, SetupError> {
    let staging_root = prepared
        .staging_root
        .to_str()
        .filter(|value| value.starts_with('/') && !value.contains(['\n', '\r', '\0']))
        .ok_or(SetupError::PrivilegedInput)?;
    let staging_root = staging_root.replace('\'', "'\\''");
    let install_owner = if prepared.required_owner == Some(0) {
        "-o root -g root "
    } else {
        ""
    };
    let public_key =
        std::str::from_utf8(&authority.public_key_pem).map_err(|_| SetupError::ReleaseSignature)?;
    if public_key.contains("VONK_INSTALLER_RELEASE_PUBLIC_KEY") {
        return Err(SetupError::ReleaseSignature);
    }
    Ok(format!(
        r#"umask 077
root=$(/usr/bin/mktemp -d '{staging_root}/vonk-spark-setup.XXXXXX')
trap '/bin/rm -rf -- "$root"' EXIT HUP INT TERM
setup=$root/vonk-spark-setup
package=$root/vonk-forge-agent.deb
encoded_signature=$root/vonk-spark-setup.sig
signature=$root/vonk-spark-setup.raw.sig
public_key=$root/installer-release-public.pem
/usr/bin/install {install_owner}-m 0700 -- "$1" "$setup"
/usr/bin/install {install_owner}-m 0600 -- "$3" "$encoded_signature"
/usr/bin/cat > "$public_key" <<'VONK_INSTALLER_RELEASE_PUBLIC_KEY'
{public_key}VONK_INSTALLER_RELEASE_PUBLIC_KEY
[ "$(/usr/bin/stat -c %s "$encoded_signature")" -le {MAX_RELEASE_SIGNATURE_BYTES} ]
/usr/bin/openssl base64 -d -A -in "$encoded_signature" -out "$signature" >/dev/null 2>&1
[ "$(/usr/bin/stat -c %s "$signature")" -gt 0 ]
[ "$(/usr/bin/stat -c %s "$signature")" -le 1024 ]
/usr/bin/openssl dgst -sha256 -verify "$public_key" -signature "$signature" "$setup" >/dev/null 2>&1
/usr/bin/install {install_owner}-m 0600 -- "$2" "$package"
"$setup" __apply
"#
    ))
}

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
    #[error("existing installation is incomplete or unsafe")]
    ExistingInstall,
    #[error("interactive setup failed")]
    Prompt,
    #[error("controller CA is invalid or does not match its supplied SHA-256")]
    ControllerCa,
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
        architecture,
    )?;
    for executable in [
        "/bin/rm",
        "/bin/sh",
        "/usr/bin/apt-get",
        "/usr/bin/cat",
        "/usr/bin/curl",
        "/usr/bin/dpkg-deb",
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

fn validate_host_description(
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

pub fn prepare_setup(
    request: &SetupRequest,
    paths: &InstallPaths,
    prompt: &mut dyn Prompt,
    runner: &mut dyn CommandRunner,
    caller: CallerIdentity,
) -> Result<PreparedSetup, SetupError> {
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
    verify_release_artifact_size(&request.executable, release.setup.size)?;
    verify_regular_file_digest(&request.executable, &release.setup.sha256, 64 * 1024 * 1024)?;
    verify_release_artifact_size(&request.package, release.package.size)?;
    let staged = stage_verified_package_from(
        &request.package,
        &release.package.sha256,
        &release.version,
        &release.architecture,
        true,
    )?;
    let plan = match install_state(paths, StateValidation::MetadataOnly)? {
        InstallState::Fresh => {
            let enrollment_url = required_origin(prompt, "Enrollment URL")?;
            let ca_sha256 = required_sha256(prompt, "Controller CA SHA-256")?;
            let pairing_token = prompt
                .secret("Pairing token")
                .map_err(|_| SetupError::Prompt)?;
            if !valid_token(&pairing_token) {
                return Err(SetupError::UnsafeInput("pairing token"));
            }
            let discovery = discover_enrollment(
                &enrollment_url,
                &ca_sha256,
                request.controller_address,
                runner,
            )?;
            let firewall = FirewallConfig::collect(
                &request.firewall_inputs,
                request.controller_address,
                prompt,
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
        InstallState::ConfiguredUnpaired => {
            let config = paired_configuration(&paths.config, paths)?;
            let ca = fs::read(&paths.ca).map_err(|_| SetupError::ExistingInstall)?;
            verify_ca(&ca, &config.ca_sha256)?;
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
        InstallState::Existing => ApplyOperation::Upgrade,
    };
    let envelope = ApplyEnvelope {
        schema_version: 1,
        caller_uid,
        release_manifest: release.raw,
        release_signature: release.signature,
        plan,
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

fn encode_apply_frame(envelope: &ApplyEnvelope) -> Result<Vec<u8>, SetupError> {
    let payload = serde_json::to_vec(envelope).map_err(|_| SetupError::PrivilegedInput)?;
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

fn decode_apply_frame(input: impl Read) -> Result<ApplyEnvelope, SetupError> {
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
    serde_json::from_slice(payload).map_err(|_| SetupError::PrivilegedInput)
}

fn read_bounded_regular(path: &Path, maximum: usize) -> Result<Vec<u8>, SetupError> {
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

fn verified_release_from_files(
    request: &SetupRequest,
    authority: &ReleaseAuthority,
) -> Result<VerifiedRelease, SetupError> {
    let manifest = read_bounded_regular(&request.release_manifest, MAX_RELEASE_BYTES)?;
    let signature = read_bounded_regular(&request.release_signature, MAX_RELEASE_SIGNATURE_BYTES)?;
    let release = verified_release(manifest, signature, authority)?;
    let setup_signature =
        read_bounded_regular(&request.setup_signature, MAX_RELEASE_SIGNATURE_BYTES)?;
    verify_release_artifact_size(&request.setup_signature, release.setup_signature.size)?;
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

fn verified_release(
    raw: Vec<u8>,
    signature: Vec<u8>,
    authority: &ReleaseAuthority,
) -> Result<VerifiedRelease, SetupError> {
    authority.verify(&raw, &signature)?;
    let document: ReleaseDocument =
        serde_json::from_slice(&raw).map_err(|_| SetupError::ReleaseSignature)?;
    let (platform, architecture) = match std::env::consts::ARCH {
        "x86_64" => ("linux-amd64", "amd64"),
        "aarch64" => ("linux-arm64", "arm64"),
        _ => return Err(SetupError::ReleaseSignature),
    };
    if document.schema_version != 1
        || !matches!(document.channel.as_str(), "dev" | "stable")
        || !valid_sha256(&document.generation)
        || document.source_sha.len() != 40
        || !document
            .source_sha
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        || !valid_package_version(&document.version)
        || document.images.is_empty()
        || document.bootstraps.is_empty()
    {
        return Err(SetupError::ReleaseSignature);
    }
    let package = document
        .artifacts
        .get(&format!("agent-package-{platform}"))
        .cloned()
        .ok_or(SetupError::ReleaseSignature)?;
    let setup = document
        .artifacts
        .get(&format!("spark-setup-{platform}"))
        .cloned()
        .ok_or(SetupError::ReleaseSignature)?;
    let setup_signature = document
        .artifacts
        .get(&format!("spark-setup-signature-{platform}"))
        .cloned()
        .ok_or(SetupError::ReleaseSignature)?;
    let prefix = release_artifact_prefix(
        &document.channel,
        &document.generation,
        platform,
        document.acceptance_only,
    );
    if package.path != format!("{prefix}vonk-forge-agent.deb")
        || setup.path != format!("{prefix}vonk-spark-setup")
        || setup_signature.path != format!("{prefix}vonk-spark-setup.sig")
        || !valid_sha256(&package.sha256)
        || !valid_sha256(&setup.sha256)
        || !valid_sha256(&setup_signature.sha256)
        || package.size < 68
        || package.size > MAX_PACKAGE_BYTES
        || setup.size == 0
        || setup.size > 64 * 1024 * 1024
        || setup_signature.size == 0
        || setup_signature.size > MAX_RELEASE_SIGNATURE_BYTES as u64
    {
        return Err(SetupError::ReleaseSignature);
    }
    Ok(VerifiedRelease {
        raw,
        signature,
        package,
        setup,
        setup_signature,
        version: document.version,
        architecture: architecture.to_owned(),
    })
}

fn release_artifact_prefix(
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

pub fn apply_setup_from(
    input: impl Read,
    executable_path: &Path,
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    caller: CallerIdentity,
) -> Result<(), SetupError> {
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
            | (ApplyOperation::Upgrade, InstallState::Existing)
    ) {
        return Err(SetupError::PrivilegedInput);
    }
    validate_plan_against_installation(&envelope.plan, paths)?;
    let package_path = validated_staging_session(executable_path, paths)?;
    verify_release_artifact_size(executable_path, release.setup.size)
        .map_err(|_| SetupError::PrivilegedInput)?;
    verify_regular_file_digest(executable_path, &release.setup.sha256, 64 * 1024 * 1024)
        .map_err(|_| SetupError::PrivilegedInput)?;
    verify_release_artifact_size(&package_path, release.package.size)
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
            write_setup_state(paths, b"paired-v1\n", owner)?;
            start_and_verify(paths, runner)
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
            install_package(runner, &staged)?;
            pair_agent(
                paths,
                runner,
                &config.enrollment_url,
                &config.ca_sha256,
                pairing_token,
            )?;
            write_setup_state(paths, b"paired-v1\n", owner)?;
            start_and_verify(paths, runner)
        }
        ApplyOperation::Upgrade => {
            let config = paired_configuration(&paths.config, paths)?;
            let ca = fs::read(&paths.ca).map_err(|_| SetupError::ExistingInstall)?;
            verify_ca(&ca, &config.ca_sha256)?;
            upgrade_existing(paths, runner, &staged)
        }
    }
}

fn validate_plan_against_installation(
    plan: &ApplyOperation,
    paths: &InstallPaths,
) -> Result<(), SetupError> {
    match plan {
        ApplyOperation::Fresh { .. } => Ok(()),
        ApplyOperation::Pair {
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
        ApplyOperation::Upgrade => {
            let config = paired_configuration(&paths.config, paths)?;
            let ca = fs::read(&paths.ca).map_err(|_| SetupError::ExistingInstall)?;
            verify_ca(&ca, &config.ca_sha256)
        }
    }
}

fn validate_apply_envelope(envelope: &ApplyEnvelope) -> Result<(), SetupError> {
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
        } => {
            if !valid_origin(enrollment_url)
                || !valid_sha256(ca_sha256)
                || !valid_token(pairing_token)
            {
                return Err(SetupError::PrivilegedInput);
            }
            Ok(())
        }
        ApplyOperation::Upgrade => Ok(()),
    }
}

fn install_configuration(
    paths: &InstallPaths,
    config: &GeneratedConfig,
    firewall: &FirewallConfig,
    helper_authority: &[u8],
    ca: &[u8],
    owner: u32,
) -> Result<(), SetupError> {
    let rendered = config.to_toml();
    let parsed: WrittenConfig =
        toml::from_str(&rendered).map_err(|_| SetupError::PrivilegedInput)?;
    if !valid_written_config(&parsed, paths) {
        return Err(SetupError::PrivilegedInput);
    }
    if !valid_helper_authority(helper_authority) {
        return Err(SetupError::PrivilegedInput);
    }
    atomic_root_write(&paths.ca, ca, owner, 0o644)?;
    atomic_root_write(&paths.config, rendered.as_bytes(), owner, 0o644)?;
    atomic_root_write(
        &paths.firewall_config,
        firewall.render().as_bytes(),
        owner,
        0o600,
    )?;
    atomic_root_write(
        &paths.helper_authority,
        format!("{}\n", hex::encode(helper_authority)).as_bytes(),
        owner,
        0o644,
    )
}

const HOSTS_BEGIN: &str = "# BEGIN VONK FORGE MANAGED HOSTS";
const HOSTS_END: &str = "# END VONK FORGE MANAGED HOSTS";
const MAX_HOSTS_BYTES: u64 = 1024 * 1024;

fn install_host_mapping(
    paths: &InstallPaths,
    mapping: &HostMapping,
    owner: u32,
) -> Result<(), SetupError> {
    if !valid_host_mapping(Some(mapping)) {
        return Err(SetupError::PrivilegedInput);
    }
    let metadata = fs::symlink_metadata(&paths.hosts).map_err(SetupError::PrivilegedWrite)?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.uid() != owner
        || metadata.len() > MAX_HOSTS_BYTES
        || metadata.permissions().mode() & 0o022 != 0
    {
        return Err(SetupError::PrivilegedInput);
    }
    let existing = fs::read_to_string(&paths.hosts).map_err(SetupError::PrivilegedWrite)?;
    let mut retained = Vec::new();
    let mut managed = false;
    for line in existing.lines() {
        if line == HOSTS_BEGIN {
            if managed {
                return Err(SetupError::PrivilegedInput);
            }
            managed = true;
        } else if line == HOSTS_END {
            if !managed {
                return Err(SetupError::PrivilegedInput);
            }
            managed = false;
        } else if !managed {
            retained.push(line);
        }
    }
    if managed {
        return Err(SetupError::PrivilegedInput);
    }
    while retained.last().is_some_and(|line| line.is_empty()) {
        retained.pop();
    }
    let mut rendered = retained.join("\n");
    if !rendered.is_empty() {
        rendered.push('\n');
    }
    rendered.push_str(HOSTS_BEGIN);
    rendered.push('\n');
    rendered.push_str(&mapping.address);
    rendered.push(' ');
    rendered.push_str(&mapping.hostnames.join(" "));
    rendered.push('\n');
    rendered.push_str(HOSTS_END);
    rendered.push('\n');
    atomic_root_write(&paths.hosts, rendered.as_bytes(), owner, 0o644)
}

fn setup_state_path(paths: &InstallPaths) -> PathBuf {
    paths.config.with_file_name("setup-state")
}

fn write_setup_state(paths: &InstallPaths, state: &[u8], owner: u32) -> Result<(), SetupError> {
    atomic_root_write(&setup_state_path(paths), state, owner, 0o644)
}

fn pair_agent(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    enrollment_url: &Url,
    ca_sha256: &str,
    pairing_token: String,
) -> Result<(), SetupError> {
    if !valid_token(&pairing_token) {
        return Err(SetupError::UnsafeInput("pairing token"));
    }
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
            enrollment_url.as_str(),
            "--ca-sha256",
            ca_sha256,
            "--token-stdin",
        ],
    )
    .with_stdin(format!("{pairing_token}\n").into_bytes());
    run_checked(runner, pair)?;
    Ok(())
}

fn start_and_verify(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    enable_runtime_units(paths, runner)?;
    verify_sustained_readiness(paths, runner)
}

fn enable_runtime_units(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    run_checked(
        runner,
        Command::new(
            "/usr/bin/systemctl",
            [
                "enable",
                "--now",
                FIREWALL_SERVICE,
                HELPER_SOCKET,
                &paths.service,
            ],
        ),
    )
    .map(|_| ())
}

fn install_package(
    runner: &mut dyn CommandRunner,
    staged: &StagedPackage,
) -> Result<(), SetupError> {
    run_checked(
        runner,
        Command::new("/usr/bin/apt-get", ["update"]).with_env("DEBIAN_FRONTEND", "noninteractive"),
    )?;
    run_checked(
        runner,
        Command::new(
            "/usr/bin/apt-get",
            [
                "install".to_owned(),
                "--yes".to_owned(),
                "--no-install-recommends".to_owned(),
                "-o".to_owned(),
                "Dpkg::Options::=--force-confold".to_owned(),
                staged.path().display().to_string(),
            ],
        )
        .with_env("DEBIAN_FRONTEND", "noninteractive"),
    )
    .map(|_| ())
}

fn upgrade_existing(
    paths: &InstallPaths,
    runner: &mut dyn CommandRunner,
    staged: &StagedPackage,
) -> Result<(), SetupError> {
    install_package(runner, staged)?;
    enable_runtime_units(paths, runner)?;
    run_checked(
        runner,
        Command::new("/usr/bin/systemctl", ["restart", &paths.service]),
    )?;
    verify_sustained_readiness(paths, runner)
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
            Command::new(
                "/usr/bin/systemctl",
                ["is-active", "--quiet", &paths.service],
            ),
        );
        let current_pid = active.and_then(|_| {
            run_checked(
                runner,
                Command::new(
                    "/usr/bin/systemctl",
                    ["show", "--property", "MainPID", "--value", &paths.service],
                ),
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
                    Command::new(
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
                    )
                    .suppress_stderr(),
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
    ConfiguredUnpaired,
    Existing,
}

#[derive(Clone, Copy)]
enum StateValidation {
    MetadataOnly,
    Complete,
}

fn install_state(
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
                b"paired-v1\n" => Ok(InstallState::Existing),
                _ => Err(SetupError::ExistingInstall),
            }
        }
        _ => Err(SetupError::ExistingInstall),
    }
}

fn installed_helper_authority(paths: &InstallPaths) -> Result<Vec<u8>, SetupError> {
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

fn valid_helper_authority(value: &[u8]) -> bool {
    value.len() == 32 && value.iter().any(|byte| *byte != 0)
}

fn installed_firewall_configuration(paths: &InstallPaths) -> Result<FirewallConfig, SetupError> {
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

fn safe_existing_parent(path: &Path, required_owner: Option<u32>) -> Result<bool, SetupError> {
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

fn paired_configuration(path: &Path, paths: &InstallPaths) -> Result<WrittenConfig, SetupError> {
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

fn verify_release_artifact_size(path: &Path, expected: u64) -> Result<(), SetupError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| SetupError::ReleaseSignature)?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() != expected
    {
        return Err(SetupError::ReleaseSignature);
    }
    Ok(())
}

fn validated_staging_session(
    executable_path: &Path,
    paths: &InstallPaths,
) -> Result<PathBuf, SetupError> {
    if !executable_path.is_absolute()
        || executable_path.file_name().and_then(|name| name.to_str()) != Some("vonk-spark-setup")
    {
        return Err(SetupError::PrivilegedInput);
    }
    let session = executable_path
        .parent()
        .ok_or(SetupError::PrivilegedInput)?;
    if session.parent() != Some(paths.staging_root.as_path()) {
        return Err(SetupError::PrivilegedInput);
    }
    let session_name = session
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or(SetupError::PrivilegedInput)?;
    let suffix = session_name
        .strip_prefix("vonk-spark-setup.")
        .ok_or(SetupError::PrivilegedInput)?;
    if !(6..=64).contains(&suffix.len()) || !suffix.bytes().all(|byte| byte.is_ascii_alphanumeric())
    {
        return Err(SetupError::PrivilegedInput);
    }
    let expected_owner = paths
        .required_owner
        .unwrap_or_else(|| rustix::process::geteuid().as_raw());
    let session_metadata =
        fs::symlink_metadata(session).map_err(|_| SetupError::PrivilegedInput)?;
    if !session_metadata.file_type().is_dir()
        || session_metadata.file_type().is_symlink()
        || session_metadata.uid() != expected_owner
        || session_metadata.permissions().mode() & 0o777 != 0o700
    {
        return Err(SetupError::PrivilegedInput);
    }
    let package = session.join("vonk-forge-agent.deb");
    for (path, mode) in [(executable_path, 0o700), (package.as_path(), 0o600)] {
        let metadata = fs::symlink_metadata(path).map_err(|_| SetupError::PrivilegedInput)?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.nlink() != 1
            || metadata.uid() != expected_owner
            || metadata.permissions().mode() & 0o777 != mode
        {
            return Err(SetupError::PrivilegedInput);
        }
    }
    Ok(package)
}

struct StagedPackage {
    _directory: TempDir,
    path: PathBuf,
}

fn secure_tempdir(prefix: &str) -> Result<TempDir, SetupError> {
    tempfile::Builder::new()
        .prefix(prefix)
        .tempdir_in("/var/tmp")
        .map_err(SetupError::PrivilegedWrite)
}

impl StagedPackage {
    fn path(&self) -> &Path {
        &self.path
    }
}

fn stage_verified_package_from(
    source: &Path,
    expected: &str,
    expected_version: &str,
    expected_architecture: &str,
    require_release_name: bool,
) -> Result<StagedPackage, SetupError> {
    if !source.is_absolute()
        || !valid_sha256(expected)
        || (require_release_name && !valid_package_name(source))
    {
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
    let directory = secure_tempdir("vonk-spark-package.")?;
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
    verify_debian_identity(&path, expected_version, expected_architecture)?;
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

fn verify_regular_file_digest(path: &Path, expected: &str, maximum: u64) -> Result<(), SetupError> {
    let before = fs::symlink_metadata(path).map_err(|_| SetupError::UnsafePackage)?;
    if !before.file_type().is_file()
        || before.file_type().is_symlink()
        || before.nlink() != 1
        || before.len() == 0
        || before.len() > maximum
        || before.permissions().mode() & 0o022 != 0
    {
        return Err(SetupError::UnsafePackage);
    }
    let mut input = OpenOptions::new()
        .read(true)
        .custom_flags(libc_nofollow())
        .open(path)
        .map_err(|_| SetupError::UnsafePackage)?;
    let open = input.metadata().map_err(|_| SetupError::UnsafePackage)?;
    if !same_file(&before, &open) {
        return Err(SetupError::UnsafePackage);
    }
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = input
            .read(&mut buffer)
            .map_err(|_| SetupError::UnsafePackage)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let after = input.metadata().map_err(|_| SetupError::UnsafePackage)?;
    if !same_file(&open, &after) || hex::encode(digest.finalize()) != expected {
        return Err(SetupError::PackageDigest);
    }
    Ok(())
}

fn verify_debian_identity(
    path: &Path,
    expected_version: &str,
    expected_architecture: &str,
) -> Result<(), SetupError> {
    fn field(path: &Path, name: &str) -> Result<String, SetupError> {
        let output = ProcessCommand::new("/usr/bin/dpkg-deb")
            .args(["--field"])
            .arg(path)
            .arg(name)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .map_err(|_| SetupError::PackageFormat)?;
        if !output.status.success() {
            return Err(SetupError::PackageFormat);
        }
        String::from_utf8(output.stdout)
            .map(|value| value.trim().to_owned())
            .map_err(|_| SetupError::PackageFormat)
    }

    let package = field(path, "Package")?;
    let version = field(path, "Version")?;
    let architecture = field(path, "Architecture")?;
    if package != "vonk-forge-agent"
        || version != expected_version
        || architecture != expected_architecture
    {
        return Err(SetupError::PackageIdentity);
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

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentBootstrap {
    controller_endpoint: String,
    enrollment_endpoint: String,
    ca_fingerprint: String,
    ca_pem: String,
    #[serde(default)]
    controller_address: Option<String>,
    #[serde(default)]
    service_hostnames: Vec<String>,
    host_helper_authority_public_key: String,
}

fn discover_enrollment(
    expected_enrollment: &Url,
    expected_ca_sha256: &str,
    expected_controller_address: Option<Ipv4Addr>,
    runner: &mut dyn CommandRunner,
) -> Result<EnrollmentDiscovery, SetupError> {
    let mut bootstrap_url = expected_enrollment.clone();
    bootstrap_url.set_path("/agent/v1/bootstrap");
    bootstrap_url.set_query(Some("setup_schema=2"));
    let output = run_checked(
        runner,
        bootstrap_curl(&bootstrap_url, expected_controller_address, None),
    )?
    .stdout;
    if output.is_empty() || output.len() > MAX_BOOTSTRAP_BYTES {
        return Err(SetupError::EnrollmentBootstrap);
    }
    let bootstrap: EnrollmentBootstrap =
        serde_json::from_slice(&output).map_err(|_| SetupError::EnrollmentBootstrap)?;
    let discovered = validate_enrollment_bootstrap(
        bootstrap,
        expected_enrollment,
        expected_ca_sha256,
        expected_controller_address,
    )?;
    let directory = secure_tempdir("vonk-enrollment-ca.")?;
    let ca_path = directory.path().join("controller-ca.pem");
    fs::write(&ca_path, &discovered.ca_pem).map_err(SetupError::PrivilegedWrite)?;
    let authenticated = run_checked(
        runner,
        bootstrap_curl(
            &bootstrap_url,
            discovered.host_mapping.as_ref().map(|value| {
                value
                    .address
                    .parse::<Ipv4Addr>()
                    .expect("validated controller address")
            }),
            Some(&ca_path),
        ),
    )?
    .stdout;
    if authenticated.is_empty() || authenticated.len() > MAX_BOOTSTRAP_BYTES {
        return Err(SetupError::EnrollmentBootstrap);
    }
    let authenticated: EnrollmentBootstrap =
        serde_json::from_slice(&authenticated).map_err(|_| SetupError::EnrollmentBootstrap)?;
    let verified = validate_enrollment_bootstrap(
        authenticated,
        expected_enrollment,
        expected_ca_sha256,
        discovered
            .host_mapping
            .as_ref()
            .map(|value| value.address.parse().expect("validated controller address")),
    )?;
    if verified != discovered {
        return Err(SetupError::EnrollmentBootstrap);
    }
    Ok(verified)
}

fn bootstrap_curl(
    bootstrap_url: &Url,
    controller_address: Option<Ipv4Addr>,
    ca_path: Option<&Path>,
) -> Command {
    let mut arguments = vec![
        "--fail".to_owned(),
        "--silent".to_owned(),
        "--show-error".to_owned(),
        "--proto".to_owned(),
        "=https".to_owned(),
        "--tlsv1.2".to_owned(),
    ];
    if let Some(address) = controller_address {
        let hostname = bootstrap_url.host_str().expect("validated HTTPS origin");
        let port = bootstrap_url.port_or_known_default().unwrap_or(443);
        arguments.extend([
            "--resolve".to_owned(),
            format!("{hostname}:{port}:{address}"),
        ]);
    }
    if let Some(path) = ca_path {
        arguments.extend(["--cacert".to_owned(), path.display().to_string()]);
    } else {
        arguments.push("--insecure".to_owned());
    }
    arguments.extend([
        "--max-filesize".to_owned(),
        MAX_BOOTSTRAP_BYTES.to_string(),
        bootstrap_url.as_str().to_owned(),
    ]);
    Command::new("/usr/bin/curl", arguments)
}

fn validate_enrollment_bootstrap(
    bootstrap: EnrollmentBootstrap,
    expected_enrollment: &Url,
    expected_ca_sha256: &str,
    expected_controller_address: Option<Ipv4Addr>,
) -> Result<EnrollmentDiscovery, SetupError> {
    let enrollment =
        Url::parse(&bootstrap.enrollment_endpoint).map_err(|_| SetupError::EnrollmentBootstrap)?;
    let controller =
        Url::parse(&bootstrap.controller_endpoint).map_err(|_| SetupError::EnrollmentBootstrap)?;
    if !valid_origin(&enrollment)
        || !valid_origin(&controller)
        || &enrollment != expected_enrollment
        || bootstrap.ca_fingerprint != expected_ca_sha256
        || bootstrap.ca_pem.len() > MAX_CA_BYTES
    {
        return Err(SetupError::EnrollmentBootstrap);
    }
    let ca = bootstrap.ca_pem.into_bytes();
    verify_ca(&ca, expected_ca_sha256).map_err(|_| SetupError::EnrollmentBootstrap)?;
    if bootstrap.host_helper_authority_public_key.len() != 64
        || !bootstrap
            .host_helper_authority_public_key
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(SetupError::EnrollmentBootstrap);
    }
    let helper_authority = hex::decode(bootstrap.host_helper_authority_public_key)
        .map_err(|_| SetupError::EnrollmentBootstrap)?;
    if !valid_helper_authority(&helper_authority) {
        return Err(SetupError::EnrollmentBootstrap);
    }
    let mapping = match bootstrap.controller_address {
        Some(address) => {
            let address = address
                .parse::<Ipv4Addr>()
                .map_err(|_| SetupError::EnrollmentBootstrap)?;
            if expected_controller_address.is_some_and(|expected| expected != address) {
                return Err(SetupError::EnrollmentBootstrap);
            }
            let mapping = HostMapping {
                address: address.to_string(),
                hostnames: bootstrap.service_hostnames,
            };
            if !valid_host_mapping(Some(&mapping))
                || !mapping
                    .hostnames
                    .iter()
                    .any(|value| Some(value.as_str()) == enrollment.host_str())
                || !mapping
                    .hostnames
                    .iter()
                    .any(|value| Some(value.as_str()) == controller.host_str())
            {
                return Err(SetupError::EnrollmentBootstrap);
            }
            Some(mapping)
        }
        None if bootstrap.service_hostnames.is_empty() && expected_controller_address.is_none() => {
            None
        }
        None => return Err(SetupError::EnrollmentBootstrap),
    };
    Ok(EnrollmentDiscovery {
        controller_url: controller,
        ca_pem: ca,
        host_mapping: mapping,
        helper_authority,
    })
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

fn valid_host_mapping(mapping: Option<&HostMapping>) -> bool {
    let Some(mapping) = mapping else {
        return true;
    };
    mapping.address.parse::<Ipv4Addr>().is_ok()
        && !mapping.hostnames.is_empty()
        && mapping.hostnames.len() <= 16
        && mapping
            .hostnames
            .iter()
            .enumerate()
            .all(|(index, hostname)| {
                valid_hostname(hostname) && !mapping.hostnames[..index].contains(hostname)
            })
}

fn valid_hostname(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 253
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-' || byte == b'.'
        })
        && value.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && label
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric)
                && label
                    .as_bytes()
                    .last()
                    .is_some_and(u8::is_ascii_alphanumeric)
        })
        && value.parse::<Ipv4Addr>().is_err()
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_package_version(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'+' | b'~' | b':' | b'-')
        })
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
    ca_path: PathBuf,
    ca_sha256: String,
    node_id: String,
    fabric_address: Ipv4Addr,
    fabric_bandwidth_mbps: u64,
}

impl GeneratedConfig {
    fn to_toml(&self) -> String {
        format!(
            "enrollment_url = \"{}\"\ncontroller_url = \"{}\"\nca_path = \"{}\"\nca_sha256 = \"{}\"\ndata_dir = \"{DATA_DIR}\"\nnode_id = \"{}\"\npoll_min_seconds = 2\npoll_max_seconds = 60\nfabric_address = \"{}\"\nfabric_bandwidth_mbps = {}\n",
            self.enrollment_url,
            self.controller_url,
            self.ca_path.display(),
            self.ca_sha256,
            self.node_id,
            self.fabric_address,
            self.fabric_bandwidth_mbps,
        )
    }
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
    fabric_address: Ipv4Addr,
    fabric_bandwidth_mbps: u64,
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
        && valid_site_ipv4(config.fabric_address)
        && (1..=1_000_000).contains(&config.fabric_bandwidth_mbps)
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
            .stdout(Stdio::piped());
        process.stderr(match command.stderr {
            CommandStderr::Inherit => Stdio::inherit(),
            CommandStderr::Suppress => Stdio::null(),
        });
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
    terminal: Option<TtyTerminal>,
}

struct TtyTerminal {
    reader: BufReader<File>,
    tty: File,
}

impl TtyPrompt {
    pub fn new() -> Self {
        Self { terminal: None }
    }

    fn terminal(&mut self) -> Result<&mut TtyTerminal, String> {
        if self.terminal.is_none() {
            self.terminal = Some(Self::open_terminal()?);
        }
        self.terminal
            .as_mut()
            .ok_or_else(|| "tty unavailable".to_owned())
    }

    fn open_terminal() -> Result<TtyTerminal, String> {
        let tty = OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/tty")
            .map_err(|_| "tty unavailable".to_owned())?;
        let reader = BufReader::new(tty.try_clone().map_err(|_| "tty unavailable".to_owned())?);
        Ok(TtyTerminal { reader, tty })
    }
}

impl Default for TtyPrompt {
    fn default() -> Self {
        Self::new()
    }
}

impl Prompt for TtyPrompt {
    fn value(&mut self, label: &str) -> Result<String, String> {
        let terminal = self.terminal()?;
        write!(terminal.tty, "{label}: ").map_err(|_| "tty output failed".to_owned())?;
        terminal
            .tty
            .flush()
            .map_err(|_| "tty output failed".to_owned())?;
        let mut value = String::new();
        if terminal
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
    use std::collections::VecDeque;
    use std::os::unix::fs::PermissionsExt;
    use tempfile::tempdir;

    struct Values(VecDeque<String>);

    impl Prompt for Values {
        fn value(&mut self, _label: &str) -> Result<String, String> {
            self.0.pop_front().ok_or_else(|| "missing value".to_owned())
        }

        fn secret(&mut self, _label: &str) -> Result<String, String> {
            Err("unexpected secret prompt".to_owned())
        }
    }

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
            validate_host_description("ID=ubuntu\n", true, "amd64", "arm64"),
            Err(SetupError::UnsupportedHost)
        ));
    }

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
                "enrollment_url = \"https://enroll.example.test/\"\ncontroller_url = \"https://controller.example.test/\"\nca_path = \"{}\"\nca_sha256 = \"{}\"\ndata_dir = \"{}\"\nnode_id = \"spk_0123456789abcdef0123456789abcdef\"\npoll_min_seconds = 2\npoll_max_seconds = 60\nfabric_address = \"192.168.100.10\"\nfabric_bandwidth_mbps = 200000\n",
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
    fn firewall_configuration_defaults_are_canonical_and_controller_bound() {
        let mut prompt = Values(
            ["192.168.1.211", "192.168.100.10", "192.168.100.11"]
                .map(str::to_owned)
                .into(),
        );
        let config = FirewallConfig::collect(
            &FirewallInputs::default(),
            Some("192.168.1.231".parse().unwrap()),
            &mut prompt,
        )
        .unwrap();

        assert_eq!(
            config.render(),
            "VONK_NAS_MANAGEMENT_IP=192.168.1.231\nVONK_NODE_MANAGEMENT_IP=192.168.1.211\nVONK_NODE_FABRIC_IP=192.168.100.10\nVONK_PEER_FABRIC_IP=192.168.100.11\nVONK_ENDPOINT_HOST_PORTS=8000,8101\nVONK_HOST_ENDPOINT_PORTS=8888\nVONK_RENDEZVOUS_PORT=29500\n"
        );
        assert_eq!(config.fabric_bandwidth_mbps, 200_000);
        assert!(prompt.0.is_empty());
    }

    #[test]
    fn firewall_configuration_rejects_ambiguous_topology_and_ports() {
        let base = FirewallInputs {
            nas_management_ip: Some("192.168.1.231".to_owned()),
            node_management_ip: Some("192.168.1.211".to_owned()),
            node_fabric_ip: Some("192.168.100.10".to_owned()),
            peer_fabric_ip: Some("192.168.100.10".to_owned()),
            ..FirewallInputs::default()
        };
        let mut prompt = Values(VecDeque::new());
        assert!(matches!(
            FirewallConfig::collect(&base, None, &mut prompt),
            Err(SetupError::UnsafeInput("Spark network topology"))
        ));

        let invalid_ports = FirewallInputs {
            peer_fabric_ip: Some("192.168.100.11".to_owned()),
            endpoint_host_ports: Some("08000".to_owned()),
            ..base
        };
        assert!(matches!(
            FirewallConfig::collect(&invalid_ports, None, &mut prompt),
            Err(SetupError::UnsafeInput("endpoint host ports"))
        ));
    }
}
