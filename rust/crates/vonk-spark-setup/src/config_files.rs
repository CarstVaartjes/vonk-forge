//! Config files.

use super::*;

#[derive(Clone)]
pub(super) struct GeneratedConfig {
    pub(super) enrollment_url: Url,
    pub(super) controller_url: Url,
    pub(super) ca_path: PathBuf,
    pub(super) ca_sha256: String,
    pub(super) node_id: String,
    pub(super) fabric_address: Ipv4Addr,
    pub(super) fabric_bandwidth_mbps: u64,
}

impl GeneratedConfig {
    pub(super) fn to_toml(&self) -> String {
        format!(
            "enrollment_url = \"{}\"\ncontroller_url = \"{}\"\nca_path = \"{}\"\nca_sha256 = \"{}\"\ndata_dir = \"{DATA_DIR}\"\nnode_id = \"{}\"\nfabric_address = \"{}\"\nfabric_bandwidth_mbps = {}\n",
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

// Unknown keys are tolerated so keys retired by a newer release do not make an
// existing install look unsafe; `refresh_configuration` drops them on rerun.
#[derive(Deserialize)]
pub(super) struct WrittenConfig {
    pub(super) enrollment_url: Url,
    pub(super) controller_url: Url,
    pub(super) ca_path: PathBuf,
    pub(super) ca_sha256: String,
    pub(super) data_dir: PathBuf,
    pub(super) node_id: String,
    pub(super) fabric_address: Ipv4Addr,
    pub(super) fabric_bandwidth_mbps: u64,
}

pub(super) fn valid_written_config(config: &WrittenConfig, paths: &InstallPaths) -> bool {
    valid_origin(&config.enrollment_url)
        && valid_origin(&config.controller_url)
        && config.ca_path == paths.ca
        && config.data_dir == Path::new(DATA_DIR)
        && valid_sha256(&config.ca_sha256)
        && valid_node_id(&config.node_id)
        && valid_site_ipv4(config.fabric_address)
        && (1..=1_000_000).contains(&config.fabric_bandwidth_mbps)
}

pub(super) fn valid_node_id(value: &str) -> bool {
    value.len() == 36
        && value.starts_with("spk_")
        && value[4..]
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(super) fn atomic_root_write(
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
