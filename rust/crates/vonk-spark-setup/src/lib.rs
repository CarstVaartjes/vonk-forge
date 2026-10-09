#![forbid(unsafe_code)]

use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    net::Ipv4Addr,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Command as ProcessCommand, Stdio},
    thread,
    time::{Duration, Instant},
};

use serde::Deserialize;
use sha2::{Digest, Sha256};
use tempfile::TempDir;
use thiserror::Error;
use url::Url;
use uuid::Uuid;
use vonk_agent_protocol::generated::{
    EnrollmentBootstrapResponse, SparkApplyEnvelope, SparkApplyFresh, SparkApplyFreshOperation,
    SparkApplyOperation, SparkApplyPair, SparkApplyPairOperation, SparkApplyRecover,
    SparkApplyRecoverOperation, SparkApplyReenroll, SparkApplyReenrollOperation, SparkApplyUpgrade,
    SparkApplyUpgradeOperation, SparkFirewallConfig, SparkHostMapping,
};
use wait_timeout::ChildExt;

const DEFAULT_COMMAND_TIMEOUT: Duration = Duration::from_secs(30);
const PACKAGE_COMMAND_TIMEOUT: Duration = Duration::from_secs(180);
const ROOT_HANDOFF_TIMEOUT: Duration = Duration::from_secs(300);
const TERMINATION_GRACE: Duration = Duration::from_secs(2);

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
const MONITOR_SERVICE: &str = "vonk-forge-monitor.service";
const HELPER_SOCKET: &str = "vonk-forge-package-helper.socket";
const FIREWALL_SERVICE: &str = "vonk-forge-docker-firewall.service";
const DATA_DIR: &str = "/var/lib/vonk-forge-agent";
/// The ports the Spark firewall authorises. The Controller reads the same file
/// to decide which port every run uses, so the two cannot drift.
const SITE_PORTS: &str =
    include_str!("../../../../control/src/vonk_control/resources/site-ports.json");

fn site_ports() -> vonk_agent_protocol::generated::SitePorts {
    serde_json::from_str(SITE_PORTS).expect("packaged site ports are valid")
}
const FABRIC_BANDWIDTH_MBPS: u64 = 200_000;
const IP_PATH: &str = "/usr/sbin/ip";
const RDMA_PATH: &str = "/usr/bin/rdma";
// A fresh Spark may spend well over a minute bootstrapping its controller
// connection (especially while systemd starts the helper and inventory
// dependencies).  Keep the readiness proof strict, but give that startup
// phase a bounded budget that still fits inside the bootstrap's 300-second
// command timeout.
const READINESS_MAX_WAIT: Duration = Duration::from_secs(180);
const READINESS_SAMPLE_INTERVAL: Duration = Duration::from_secs(2);
const READINESS_REQUIRED_SAMPLES: u8 = 3;
const APPLY_FRAME_MAGIC: &[u8] = b"VONK-SPARK-APPLY-V1\0";
const MAX_APPLY_FRAME_BYTES: usize = 2 * 1024 * 1024;
const INSTALLER_RELEASE_PUBLIC_KEY: &[u8] =
    include_bytes!("../../../../install/installer-release-public.pem");

mod apply;
mod commands;
mod config_files;
mod configuration;
mod enrollment;
mod firewall;
mod handoff;
mod installation;
mod package;
mod plan;
mod prepare;
mod process;
mod readiness;
mod request;
mod staging;

pub use apply::{apply_setup_from, apply_setup_from_with_authority};

pub use commands::{Command, CommandOutput, CommandRunner, CommandStderr, Prompt};
use config_files::{
    GeneratedConfig, WrittenConfig, atomic_root_write, valid_node_id, valid_written_config,
};
use configuration::{
    enable_runtime_units, install_configuration, install_host_mapping, pair_agent,
    refresh_configuration, reset_agent_failure, setup_state_path, start_and_verify,
    stop_agent_for_identity_reload, write_setup_state,
};
pub use enrollment::parse_enrollment_bootstrap;
use enrollment::{
    discover_enrollment, required_origin, required_sha256, valid_host_mapping, valid_origin,
    valid_package_version, valid_sha256, valid_token, verify_ca, verify_reenroll_controller_ca,
};
use firewall::valid_site_ipv4;
use handoff::authenticate_sudo_foreground;
pub use handoff::{handoff_to_root, handoff_to_root_with_authority};
use installation::{
    InstallState, install_state, installed_firewall_configuration, installed_helper_authority,
    paired_configuration, safe_existing_file, valid_helper_authority,
};
use package::{ensure_package_installed, install_package, upgrade_existing};
pub use plan::PreparedSetup;
use plan::{
    ApplyEnvelope, ApplyOperation, EnrollmentDiscovery, FirewallConfig, HostMapping,
    VerifiedRelease,
};
pub use prepare::{
    InstallPaths, SetupError, prepare_setup, prepare_setup_with_authority, validate_system_host,
};
use prepare::{decode_apply_frame, validate_native_architecture, verified_release};
#[cfg(test)]
use process::run_process;
pub use process::{SystemCommandRunner, TtyPrompt};
use readiness::{run_checked, verify_sustained_readiness};
pub use request::{CallerIdentity, FirewallInputs, ReleaseAuthority, SetupRequest};
use staging::{
    StagedPackage, libc_nofollow, same_file, secure_tempdir, stage_verified_package_from,
    validated_staging_session, verify_regular_file_digest, verify_release_artifact_size,
};
