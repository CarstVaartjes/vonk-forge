use std::{
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Component, Path, PathBuf},
};

use serde::Deserialize;
use thiserror::Error;
use url::Url;

pub const DEFAULT_CONFIG_PATH: &str = "/etc/vonk-forge-agent/agent.toml";
/// Controller poll and retry backoff bounds; fixed for every Spark.
pub const POLL_MIN_SECONDS: u64 = 2;
pub const POLL_MAX_SECONDS: u64 = 60;

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("agent configuration could not be read")]
    Read(#[from] std::io::Error),
    #[error("agent configuration is invalid")]
    Parse(#[from] toml::de::Error),
    #[error("agent configuration field is unsafe: {0}")]
    Unsafe(&'static str),
}

/// The Spark's site configuration. Keys this agent no longer reads (for
/// example the retired poll bounds) are ignored, so a Spark set up by an older
/// release keeps starting after a package upgrade.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct AgentConfig {
    pub enrollment_url: Url,
    pub controller_url: Url,
    pub ca_path: PathBuf,
    pub ca_sha256: String,
    pub data_dir: PathBuf,
    pub node_id: String,
    #[serde(default = "default_renewal_fraction")]
    pub renewal_fraction_basis_points: u16,
    pub fabric_address: Option<std::net::IpAddr>,
    pub fabric_bandwidth_mbps: Option<u64>,
}

fn default_renewal_fraction() -> u16 {
    5000
}

impl AgentConfig {
    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let metadata = fs::symlink_metadata(path)?;
        let effective_uid = rustix::process::geteuid().as_raw();
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() > 64 * 1024
            || !matches!(metadata.uid(), 0) && metadata.uid() != effective_uid
            || metadata.permissions().mode() & 0o022 != 0
        {
            return Err(ConfigError::Unsafe("configuration path"));
        }
        Self::parse(&fs::read_to_string(path)?)
    }

    pub fn parse(document: &str) -> Result<Self, ConfigError> {
        let config: Self = toml::from_str(document)?;
        config.validate()?;
        Ok(config)
    }

    fn validate(&self) -> Result<(), ConfigError> {
        if !(2500..=7500).contains(&self.renewal_fraction_basis_points) {
            return Err(ConfigError::Unsafe("renewal_fraction_basis_points"));
        }
        validate_origin(&self.enrollment_url, "enrollment_url")?;
        validate_origin(&self.controller_url, "controller_url")?;
        for (path, name) in [(&self.ca_path, "ca_path"), (&self.data_dir, "data_dir")] {
            if !canonical_absolute(path) {
                return Err(ConfigError::Unsafe(name));
            }
        }
        if self.ca_sha256.len() != 64
            || !self
                .ca_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(ConfigError::Unsafe("ca_sha256"));
        }
        if self.node_id.len() != 36
            || !self.node_id.starts_with("spk_")
            || !self.node_id[4..]
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(ConfigError::Unsafe("node_id"));
        }
        if self.fabric_address.is_some() != self.fabric_bandwidth_mbps.is_some()
            || self
                .fabric_bandwidth_mbps
                .is_some_and(|value| value == 0 || value > 1_000_000)
            || self.fabric_address.is_some_and(|address| {
                address.is_loopback()
                    || address.is_unspecified()
                    || address.is_multicast()
                    || match address {
                        std::net::IpAddr::V4(value) => {
                            value.is_link_local() || value.is_broadcast()
                        }
                        std::net::IpAddr::V6(value) => value.is_unicast_link_local(),
                    }
            })
        {
            return Err(ConfigError::Unsafe("fabric settings"));
        }
        Ok(())
    }
}

fn validate_origin(url: &Url, field: &'static str) -> Result<(), ConfigError> {
    if url.scheme() != "https"
        || url.host_str().is_none()
        || url.username() != ""
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return Err(ConfigError::Unsafe(field));
    }
    Ok(())
}

fn canonical_absolute(path: &Path) -> bool {
    path.is_absolute()
        && path
            .components()
            .all(|component| matches!(component, Component::RootDir | Component::Normal(_)))
}
