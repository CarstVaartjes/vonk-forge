//! Plan.

use super::*;

/// The privileged phase's plan with parsed values. It crosses the process
/// boundary as `SparkApplyOperation`, the generated wire form, through
/// [`ApplyEnvelope::to_wire`] and [`ApplyEnvelope::from_wire`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum ApplyOperation {
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
    Reenroll {
        enrollment_url: Url,
        ca_sha256: String,
        pairing_token: String,
    },
    Recover,
    Upgrade,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct HostMapping {
    pub(super) address: String,
    pub(super) hostnames: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct EnrollmentDiscovery {
    pub(super) controller_url: Url,
    pub(super) ca_pem: Vec<u8>,
    pub(super) host_mapping: Option<HostMapping>,
    pub(super) helper_authority: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct FirewallConfig {
    pub(super) nas_management_ip: Ipv4Addr,
    pub(super) node_management_ip: Ipv4Addr,
    pub(super) node_fabric_ip: Ipv4Addr,
    pub(super) peer_fabric_ip: Ipv4Addr,
    pub(super) endpoint_host_ports: Vec<u16>,
    pub(super) host_endpoint_ports: Vec<u16>,
    pub(super) rendezvous_port: u16,
    pub(super) fabric_bandwidth_mbps: u64,
}

#[derive(Debug, Clone)]
pub(super) struct ApplyEnvelope {
    pub(super) schema_version: u8,
    pub(super) caller_uid: u32,
    pub(super) release_manifest: Vec<u8>,
    pub(super) release_signature: Vec<u8>,
    pub(super) plan: ApplyOperation,
}

impl ApplyEnvelope {
    pub(super) fn to_wire(&self) -> SparkApplyEnvelope {
        let plan = match &self.plan {
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
            } => SparkApplyOperation::Fresh(SparkApplyFresh {
                operation: SparkApplyFreshOperation::Fresh,
                enrollment_url: enrollment_url.to_string(),
                controller_url: controller_url.to_string(),
                ca_sha256: ca_sha256.clone(),
                ca_pem: hex::encode(ca_pem),
                node_id: node_id.clone(),
                pairing_token: pairing_token.clone(),
                host_mapping: host_mapping.as_ref().map(|mapping| SparkHostMapping {
                    address: mapping.address.clone(),
                    hostnames: mapping.hostnames.clone(),
                }),
                firewall: SparkFirewallConfig {
                    nas_management_ip: firewall.nas_management_ip.into(),
                    node_management_ip: firewall.node_management_ip.into(),
                    node_fabric_ip: firewall.node_fabric_ip.into(),
                    peer_fabric_ip: firewall.peer_fabric_ip.into(),
                    endpoint_host_ports: firewall.endpoint_host_ports.clone(),
                    host_endpoint_ports: firewall.host_endpoint_ports.clone(),
                    rendezvous_port: firewall.rendezvous_port,
                    fabric_bandwidth_mbps: firewall.fabric_bandwidth_mbps,
                },
                helper_authority: hex::encode(helper_authority),
            }),
            ApplyOperation::Pair {
                enrollment_url,
                ca_sha256,
                pairing_token,
            } => SparkApplyOperation::Pair(SparkApplyPair {
                operation: SparkApplyPairOperation::Pair,
                enrollment_url: enrollment_url.to_string(),
                ca_sha256: ca_sha256.clone(),
                pairing_token: pairing_token.clone(),
            }),
            ApplyOperation::Reenroll {
                enrollment_url,
                ca_sha256,
                pairing_token,
            } => SparkApplyOperation::Reenroll(SparkApplyReenroll {
                operation: SparkApplyReenrollOperation::Reenroll,
                enrollment_url: enrollment_url.to_string(),
                ca_sha256: ca_sha256.clone(),
                pairing_token: pairing_token.clone(),
            }),
            ApplyOperation::Recover => SparkApplyOperation::Recover(SparkApplyRecover {
                operation: SparkApplyRecoverOperation::Recover,
            }),
            ApplyOperation::Upgrade => SparkApplyOperation::Upgrade(SparkApplyUpgrade {
                operation: SparkApplyUpgradeOperation::Upgrade,
            }),
        };
        SparkApplyEnvelope {
            schema_version: self.schema_version,
            caller_uid: self.caller_uid,
            release_manifest: hex::encode(&self.release_manifest),
            release_signature: hex::encode(&self.release_signature),
            plan,
        }
    }

    pub(super) fn from_wire(wire: SparkApplyEnvelope) -> Result<Self, SetupError> {
        let invalid = |_| SetupError::PrivilegedInput;
        let plan = match wire.plan {
            SparkApplyOperation::Fresh(fresh) => ApplyOperation::Fresh {
                enrollment_url: Box::new(Url::parse(&fresh.enrollment_url).map_err(invalid)?),
                controller_url: Box::new(Url::parse(&fresh.controller_url).map_err(invalid)?),
                ca_sha256: fresh.ca_sha256,
                ca_pem: hex::decode(&fresh.ca_pem).map_err(|_| SetupError::PrivilegedInput)?,
                node_id: fresh.node_id,
                pairing_token: fresh.pairing_token,
                host_mapping: fresh.host_mapping.map(|mapping| HostMapping {
                    address: mapping.address,
                    hostnames: mapping.hostnames,
                }),
                firewall: FirewallConfig {
                    nas_management_ip: ipv4(fresh.firewall.nas_management_ip)?,
                    node_management_ip: ipv4(fresh.firewall.node_management_ip)?,
                    node_fabric_ip: ipv4(fresh.firewall.node_fabric_ip)?,
                    peer_fabric_ip: ipv4(fresh.firewall.peer_fabric_ip)?,
                    endpoint_host_ports: fresh.firewall.endpoint_host_ports,
                    host_endpoint_ports: fresh.firewall.host_endpoint_ports,
                    rendezvous_port: fresh.firewall.rendezvous_port,
                    fabric_bandwidth_mbps: fresh.firewall.fabric_bandwidth_mbps,
                },
                helper_authority: hex::decode(&fresh.helper_authority)
                    .map_err(|_| SetupError::PrivilegedInput)?,
            },
            SparkApplyOperation::Pair(pair) => ApplyOperation::Pair {
                enrollment_url: Url::parse(&pair.enrollment_url).map_err(invalid)?,
                ca_sha256: pair.ca_sha256,
                pairing_token: pair.pairing_token,
            },
            SparkApplyOperation::Reenroll(reenroll) => ApplyOperation::Reenroll {
                enrollment_url: Url::parse(&reenroll.enrollment_url).map_err(invalid)?,
                ca_sha256: reenroll.ca_sha256,
                pairing_token: reenroll.pairing_token,
            },
            SparkApplyOperation::Recover(_) => ApplyOperation::Recover,
            SparkApplyOperation::Upgrade(_) => ApplyOperation::Upgrade,
        };
        Ok(Self {
            schema_version: wire.schema_version,
            caller_uid: wire.caller_uid,
            release_manifest: hex::decode(&wire.release_manifest)
                .map_err(|_| SetupError::PrivilegedInput)?,
            release_signature: hex::decode(&wire.release_signature)
                .map_err(|_| SetupError::PrivilegedInput)?,
            plan,
        })
    }
}

pub(super) fn ipv4(address: std::net::IpAddr) -> Result<Ipv4Addr, SetupError> {
    match address {
        std::net::IpAddr::V4(address) => Ok(address),
        std::net::IpAddr::V6(_) => Err(SetupError::PrivilegedInput),
    }
}

use vonk_agent_protocol::generated::InstallerReleaseObject as ReleaseArtifact;

pub(super) struct VerifiedRelease {
    pub(super) raw: Vec<u8>,
    pub(super) signature: Vec<u8>,
    pub(super) package: ReleaseArtifact,
    pub(super) setup: ReleaseArtifact,
    pub(super) setup_signature: ReleaseArtifact,
    pub(super) version: String,
    pub(super) architecture: String,
}

pub struct PreparedSetup {
    pub(super) executable: PathBuf,
    pub(super) setup_signature: PathBuf,
    pub(super) sudo: PathBuf,
    pub(super) staging_root: PathBuf,
    pub(super) required_owner: Option<u32>,
    pub(super) staged: StagedPackage,
    pub(super) frame: Vec<u8>,
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
            enrollment_url: None,
            ca_sha256: None,
            enroll: false,
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

    /// Enrollment values printed by the Controller's Spark command. Each one
    /// that is absent is asked for interactively instead.
    pub fn with_enrollment_values(
        mut self,
        enrollment_url: Option<&str>,
        ca_sha256: Option<&str>,
    ) -> Result<Self, SetupError> {
        self.enrollment_url = enrollment_url
            .map(|value| {
                Url::parse(value)
                    .ok()
                    .filter(valid_origin)
                    .ok_or(SetupError::UnsafeInput("endpoint URL"))
            })
            .transpose()?;
        self.ca_sha256 = ca_sha256
            .map(|value| {
                if valid_sha256(value) {
                    Ok(value.to_owned())
                } else {
                    Err(SetupError::UnsafeInput("SHA-256"))
                }
            })
            .transpose()?;
        Ok(self)
    }

    pub fn with_firewall_inputs(mut self, inputs: FirewallInputs) -> Self {
        self.firewall_inputs = inputs;
        self
    }

    pub fn with_enroll(mut self, enroll: bool) -> Self {
        self.enroll = enroll;
        self
    }
}
