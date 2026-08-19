use std::{
    fs,
    io::Write,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
};

use tempfile::NamedTempFile;
use thiserror::Error;
use url::Url;
use uuid::Uuid;

use crate::config::AgentConfig;

pub const DEFAULT_CA_PATH: &str = "/etc/vonk-forge-agent/controller-ca.pem";
pub const DEFAULT_DATA_DIR: &str = "/var/lib/vonk-forge-agent";

const PLACEHOLDER_ENROLLMENT_URL: &str = "https://enroll.vonkforge.invalid/";
const PLACEHOLDER_CONTROLLER_URL: &str = "https://controller.vonkforge.invalid/";
const PLACEHOLDER_FINGERPRINT: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";
const PLACEHOLDER_NODE_ID: &str = "spk_00000000000000000000000000000000";
const MAX_CONFIG_BYTES: u64 = 64 * 1024;

#[derive(Debug, Error)]
pub enum BootstrapError {
    #[error("bootstrap {0} is invalid")]
    Invalid(&'static str),
    #[error("existing agent configuration conflicts with the bootstrap request")]
    Conflict,
    #[error("agent configuration target is unsafe")]
    UnsafeConfig,
    #[error("agent data directory is unsafe")]
    UnsafeDataDirectory,
    #[error("agent configuration could not be written")]
    Write(#[source] std::io::Error),
}

pub fn data_directory_owner(config: &AgentConfig) -> Result<(u32, u32), BootstrapError> {
    let metadata = fs::symlink_metadata(&config.data_dir).map_err(BootstrapError::Write)?;
    if !metadata.file_type().is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() == 0
        || metadata.gid() == 0
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(BootstrapError::UnsafeDataDirectory);
    }
    Ok((metadata.uid(), metadata.gid()))
}

pub struct BootstrapRequest {
    token: String,
    controller_endpoint: Url,
    enrollment_endpoint: Url,
    ca_fingerprint: String,
}

impl BootstrapRequest {
    pub fn new(
        token: String,
        controller_endpoint: Url,
        enrollment_endpoint: Url,
        ca_fingerprint: String,
    ) -> Result<Self, BootstrapError> {
        validate_token(&token)?;
        validate_origin(&controller_endpoint, "controller endpoint")?;
        validate_origin(&enrollment_endpoint, "enrollment endpoint")?;
        validate_fingerprint(&ca_fingerprint)?;
        Ok(Self {
            token,
            controller_endpoint,
            enrollment_endpoint,
            ca_fingerprint,
        })
    }

    pub fn token(&self) -> &str {
        &self.token
    }

    pub fn controller_endpoint(&self) -> &Url {
        &self.controller_endpoint
    }

    pub fn enrollment_endpoint(&self) -> &Url {
        &self.enrollment_endpoint
    }

    pub fn ca_fingerprint(&self) -> &str {
        &self.ca_fingerprint
    }
}

pub fn parse_bootstrap_token(value: &str) -> Result<String, String> {
    validate_token(value)
        .map(|()| value.to_owned())
        .map_err(|error| error.to_string())
}

pub fn parse_bootstrap_origin(value: &str) -> Result<Url, String> {
    let url = Url::parse(value).map_err(|_| "bootstrap endpoint is invalid".to_owned())?;
    validate_origin(&url, "endpoint")
        .map(|()| url)
        .map_err(|error| error.to_string())
}

pub fn parse_bootstrap_fingerprint(value: &str) -> Result<String, String> {
    validate_fingerprint(value)
        .map(|()| value.to_owned())
        .map_err(|error| error.to_string())
}

pub fn generate_node_id() -> String {
    format!("spk_{}", Uuid::new_v4().simple())
}

pub fn materialize_config(
    path: &Path,
    request: &BootstrapRequest,
    generated_node_id: &str,
) -> Result<AgentConfig, BootstrapError> {
    let metadata = safe_config_metadata(path)?;
    let document = fs::read_to_string(path).map_err(BootstrapError::Write)?;
    let after = safe_config_metadata(path)?;
    if file_identity(&metadata) != file_identity(&after) {
        return Err(BootstrapError::UnsafeConfig);
    }
    let existing = AgentConfig::parse(&document).map_err(|_| BootstrapError::UnsafeConfig)?;
    if is_packaged_placeholder(&existing) {
        let config = AgentConfig {
            enrollment_url: request.enrollment_endpoint.clone(),
            controller_url: request.controller_endpoint.clone(),
            ca_path: PathBuf::from(DEFAULT_CA_PATH),
            ca_sha256: request.ca_fingerprint.clone(),
            data_dir: PathBuf::from(DEFAULT_DATA_DIR),
            node_id: generated_node_id.to_owned(),
            poll_min_seconds: 2,
            poll_max_seconds: 60,
            fabric_address: None,
            fabric_bandwidth_mbps: None,
            huggingface_curl_config: None,
        };
        let rendered = render_config(&config);
        let config = AgentConfig::parse(&rendered).map_err(|_| BootstrapError::UnsafeConfig)?;
        replace_config(path, rendered.as_bytes())?;
        return Ok(config);
    }
    if matches_request(&existing, request) {
        return Ok(existing);
    }
    Err(BootstrapError::Conflict)
}

fn validate_token(value: &str) -> Result<(), BootstrapError> {
    if value.len() != 43
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    {
        return Err(BootstrapError::Invalid("token"));
    }
    Ok(())
}

fn validate_origin(url: &Url, field: &'static str) -> Result<(), BootstrapError> {
    if url.scheme() != "https"
        || url.host_str().is_none()
        || url.username() != ""
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return Err(BootstrapError::Invalid(field));
    }
    Ok(())
}

fn validate_fingerprint(value: &str) -> Result<(), BootstrapError> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(BootstrapError::Invalid("CA fingerprint"));
    }
    Ok(())
}

fn safe_config_metadata(path: &Path) -> Result<fs::Metadata, BootstrapError> {
    let metadata = fs::symlink_metadata(path).map_err(BootstrapError::Write)?;
    let effective_uid = rustix::process::geteuid().as_raw();
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() > MAX_CONFIG_BYTES
        || metadata.nlink() != 1
        || metadata.uid() != 0 && metadata.uid() != effective_uid
        || metadata.permissions().mode() & 0o022 != 0
    {
        return Err(BootstrapError::UnsafeConfig);
    }
    Ok(metadata)
}

fn file_identity(metadata: &fs::Metadata) -> (u64, u64, u64, i64, i64) {
    (
        metadata.dev(),
        metadata.ino(),
        metadata.len(),
        metadata.mtime(),
        metadata.mtime_nsec(),
    )
}

fn is_packaged_placeholder(config: &AgentConfig) -> bool {
    config.enrollment_url.as_str() == PLACEHOLDER_ENROLLMENT_URL
        && config.controller_url.as_str() == PLACEHOLDER_CONTROLLER_URL
        && config.ca_path == Path::new(DEFAULT_CA_PATH)
        && config.ca_sha256 == PLACEHOLDER_FINGERPRINT
        && config.data_dir == Path::new(DEFAULT_DATA_DIR)
        && config.node_id == PLACEHOLDER_NODE_ID
        && config.poll_min_seconds == 2
        && config.poll_max_seconds == 60
        && config.fabric_address.is_none()
        && config.fabric_bandwidth_mbps.is_none()
        && config.huggingface_curl_config.is_none()
}

fn matches_request(config: &AgentConfig, request: &BootstrapRequest) -> bool {
    config.enrollment_url == request.enrollment_endpoint
        && config.controller_url == request.controller_endpoint
        && config.ca_path == Path::new(DEFAULT_CA_PATH)
        && config.ca_sha256 == request.ca_fingerprint
        && config.data_dir == Path::new(DEFAULT_DATA_DIR)
        && config.node_id != PLACEHOLDER_NODE_ID
}

fn render_config(config: &AgentConfig) -> String {
    format!(
        "enrollment_url = \"{}\"\n\
         controller_url = \"{}\"\n\
         ca_path = \"{}\"\n\
         ca_sha256 = \"{}\"\n\
         data_dir = \"{}\"\n\
         node_id = \"{}\"\n\
         poll_min_seconds = {}\n\
         poll_max_seconds = {}\n",
        config.enrollment_url,
        config.controller_url,
        config.ca_path.display(),
        config.ca_sha256,
        config.data_dir.display(),
        config.node_id,
        config.poll_min_seconds,
        config.poll_max_seconds,
    )
}

fn replace_config(path: &Path, document: &[u8]) -> Result<(), BootstrapError> {
    let parent = path.parent().ok_or(BootstrapError::UnsafeConfig)?;
    let mut temporary = NamedTempFile::new_in(parent).map_err(BootstrapError::Write)?;
    temporary
        .as_file()
        .set_permissions(fs::Permissions::from_mode(0o644))
        .map_err(BootstrapError::Write)?;
    temporary
        .write_all(document)
        .map_err(BootstrapError::Write)?;
    temporary
        .as_file()
        .sync_all()
        .map_err(BootstrapError::Write)?;
    temporary
        .persist(path)
        .map_err(|error| BootstrapError::Write(error.error))?;
    fs::File::open(parent)
        .and_then(|directory| directory.sync_all())
        .map_err(BootstrapError::Write)
}
