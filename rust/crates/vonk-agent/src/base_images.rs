//! Exact registry-to-OCI-archive staging for offline recipe builds.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom},
    net::{IpAddr, ToSocketAddrs},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path},
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

use rustix::fs::{
    AtFlags, Mode, OFlags, RenameFlags, ResolveFlags, mkdirat, openat2, renameat_with, unlinkat,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use url::Url;
use vonk_agent_protocol::RecipeBuildBaseImage;

use crate::process::{ProcessError, ProcessRunner, Program};

const OCI_LAYOUT: &[u8] = br#"{"imageLayoutVersion":"1.0.0"}"#;
const OCI_MANIFEST: &str = "application/vnd.oci.image.manifest.v1+json";
const DOCKER_MANIFEST: &str = "application/vnd.docker.distribution.manifest.v2+json";
const OCI_CONFIG: &str = "application/vnd.oci.image.config.v1+json";
const DOCKER_CONFIG: &str = "application/vnd.docker.container.image.v1+json";
const MAX_JSON_BYTES: u64 = 16 * 1024 * 1024;
const MAX_LAYERS: usize = 256;
const ORAS_AUTH: &str = "/var/lib/vonk-forge-agent/registry-auth.json";
const SAFE_RESOLUTION: ResolveFlags = ResolveFlags::BENEATH
    .union(ResolveFlags::NO_MAGICLINKS)
    .union(ResolveFlags::NO_SYMLINKS);
static TEMPORARY_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Error)]
pub(crate) enum BaseImageError {
    #[error("base-image authority is invalid")]
    Invalid,
    #[error("base-image storage exceeded its signed bound")]
    Limit,
    #[error("base-image storage is unavailable")]
    Io(#[from] std::io::Error),
    #[error("base-image registry operation failed")]
    Process(#[from] ProcessError),
}

pub(crate) struct StoredBaseImage {
    pub(crate) bytes: u64,
    pub(crate) file: File,
}

pub(crate) struct BaseImageStore {
    _data_root: File,
    _supply_root: File,
    sha256_root: File,
}

impl BaseImageStore {
    pub(crate) fn open(data_root: &Path) -> Result<Self, BaseImageError> {
        let before = fs::symlink_metadata(data_root).map_err(BaseImageError::Io)?;
        let canonical = fs::canonicalize(data_root).map_err(BaseImageError::Io)?;
        if !data_root.is_absolute()
            || before.file_type().is_symlink()
            || !before.is_dir()
            || canonical != data_root
        {
            return Err(BaseImageError::Invalid);
        }
        let data_root_file = OpenOptions::new()
            .read(true)
            .custom_flags((OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC).bits() as i32)
            .open(data_root)
            .map_err(BaseImageError::Io)?;
        let after = data_root_file.metadata().map_err(BaseImageError::Io)?;
        if !after.is_dir() || before.dev() != after.dev() || before.ino() != after.ino() {
            return Err(BaseImageError::Invalid);
        }
        let supply_root = open_or_create_directory(&data_root_file, "base-images")?;
        let sha256_root = open_or_create_directory(&supply_root, "sha256")?;
        Ok(Self {
            _data_root: data_root_file,
            _supply_root: supply_root,
            sha256_root,
        })
    }

    pub(crate) fn materialize<R: ProcessRunner>(
        &self,
        runner: &R,
        image: &RecipeBuildBaseImage,
        platform: &str,
        maximum_archive_bytes: u64,
        maximum_temporary_bytes: u64,
    ) -> Result<StoredBaseImage, BaseImageError> {
        let digest = exact_manifest_digest(image)?;
        let digest_root = open_or_create_directory(&self.sha256_root, digest)?;
        if let Some(file) = open_regular_at(&digest_root, "image.oci.tar")? {
            return verified_stored_image(file, image, platform, maximum_archive_bytes);
        }
        produce_archive(
            runner,
            &digest_root,
            image,
            platform,
            maximum_archive_bytes,
            maximum_temporary_bytes,
        )?;
        let file =
            open_regular_at(&digest_root, "image.oci.tar")?.ok_or(BaseImageError::Invalid)?;
        verified_stored_image(file, image, platform, maximum_archive_bytes)
    }
}

fn open_or_create_directory(parent: &File, name: &str) -> Result<File, BaseImageError> {
    if !safe_component(name) {
        return Err(BaseImageError::Invalid);
    }
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let descriptor = match openat2(parent, name, flags, Mode::empty(), SAFE_RESOLUTION) {
        Ok(value) => value,
        Err(error) if error == rustix::io::Errno::NOENT => {
            mkdirat(parent, name, Mode::RUSR | Mode::WUSR | Mode::XUSR)
                .map_err(std::io::Error::from)?;
            openat2(parent, name, flags, Mode::empty(), SAFE_RESOLUTION)
                .map_err(std::io::Error::from)?
        }
        Err(error) => return Err(classify_open_error(error)),
    };
    let file = File::from(descriptor);
    let metadata = file.metadata().map_err(BaseImageError::Io)?;
    if !metadata.is_dir()
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.permissions().mode() & 0o022 != 0
    {
        return Err(BaseImageError::Invalid);
    }
    Ok(file)
}

fn open_regular_at(parent: &File, name: &str) -> Result<Option<File>, BaseImageError> {
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    match openat2(parent, name, flags, Mode::empty(), SAFE_RESOLUTION) {
        Ok(descriptor) => Ok(Some(File::from(descriptor))),
        Err(error) if error == rustix::io::Errno::NOENT => Ok(None),
        Err(error) => Err(classify_open_error(error)),
    }
}

fn classify_open_error(error: rustix::io::Errno) -> BaseImageError {
    if matches!(
        error,
        rustix::io::Errno::LOOP
            | rustix::io::Errno::NOTDIR
            | rustix::io::Errno::XDEV
            | rustix::io::Errno::PERM
    ) {
        BaseImageError::Invalid
    } else {
        BaseImageError::Io(std::io::Error::from(error))
    }
}

fn safe_component(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

fn exact_manifest_digest(image: &RecipeBuildBaseImage) -> Result<&str, BaseImageError> {
    let digest = image
        .manifest_digest
        .strip_prefix("sha256:")
        .ok_or(BaseImageError::Invalid)?;
    let reference_digest = image
        .reference
        .rsplit_once('@')
        .map(|(_, value)| value)
        .ok_or(BaseImageError::Invalid)?;
    if reference_digest != image.manifest_digest
        || digest.len() != 64
        || !digest
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(BaseImageError::Invalid);
    }
    Ok(digest)
}

struct TemporaryAt<'a> {
    parent: &'a File,
    name: String,
    file: File,
    retained: bool,
}

impl<'a> TemporaryAt<'a> {
    fn create(parent: &'a File, purpose: &str) -> Result<Self, BaseImageError> {
        let sequence = TEMPORARY_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let name = format!(".{purpose}-{}-{sequence}.part", std::process::id());
        let descriptor = rustix::fs::openat(
            parent,
            &name,
            OFlags::RDWR | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::RUSR | Mode::WUSR,
        )
        .map_err(std::io::Error::from)?;
        Ok(Self {
            parent,
            name,
            file: File::from(descriptor),
            retained: false,
        })
    }
}

impl Drop for TemporaryAt<'_> {
    fn drop(&mut self) {
        if !self.retained {
            let _ = unlinkat(self.parent, self.name.as_str(), AtFlags::empty());
        }
    }
}

#[derive(Clone)]
struct RegistrySource {
    exact_reference: String,
    repository: String,
    reference_name: String,
    resolve: String,
}

fn registry_source(image: &RecipeBuildBaseImage) -> Result<RegistrySource, BaseImageError> {
    exact_manifest_digest(image)?;
    let (reference_name, _) = image
        .reference
        .rsplit_once('@')
        .ok_or(BaseImageError::Invalid)?;
    let slash = reference_name.find('/').ok_or(BaseImageError::Invalid)?;
    let registry = &reference_name[..slash];
    let path = &reference_name[slash + 1..];
    if path.is_empty() || path.contains("//") || path.split('/').any(|part| part == "..") {
        return Err(BaseImageError::Invalid);
    }
    let last_slash = path.rfind('/').map_or(0, |index| index + 1);
    let last = &path[last_slash..];
    let untagged_last = last.rsplit_once(':').map_or(last, |(name, _)| name);
    if untagged_last.is_empty() {
        return Err(BaseImageError::Invalid);
    }
    let repository_path = format!("{}{}", &path[..last_slash], untagged_last);
    let repository = format!("{registry}/{repository_path}");
    let url = Url::parse(&format!("https://{repository}")).map_err(|_| BaseImageError::Invalid)?;
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(BaseImageError::Invalid);
    }
    let hostname = url.host_str().ok_or(BaseImageError::Invalid)?;
    let port = url.port_or_known_default().ok_or(BaseImageError::Invalid)?;
    let addresses = (hostname, port)
        .to_socket_addrs()
        .map_err(|_| BaseImageError::Invalid)?
        .map(|address| address.ip())
        .collect::<Vec<_>>();
    if addresses.is_empty() || addresses.iter().any(|address| !public_ip(*address)) {
        return Err(BaseImageError::Invalid);
    }
    let address = match addresses[0] {
        IpAddr::V4(value) => value.to_string(),
        IpAddr::V6(value) => format!("[{value}]"),
    };
    Ok(RegistrySource {
        exact_reference: format!("{repository}@{}", image.manifest_digest),
        repository,
        reference_name: reference_name.to_owned(),
        resolve: format!("{hostname}:{port}:{address}"),
    })
}

fn public_ip(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => {
            let octets = address.octets();
            !address.is_private()
                && !address.is_loopback()
                && !address.is_link_local()
                && !address.is_broadcast()
                && !address.is_documentation()
                && !address.is_multicast()
                && !address.is_unspecified()
                && octets[0] != 0
                && !(octets[0] == 100 && (64..=127).contains(&octets[1]))
                && !(octets[0] == 192 && octets[1] == 0 && octets[2] == 0)
                && !(octets[0] == 198 && matches!(octets[1], 18 | 19))
                && octets[0] < 240
        }
        IpAddr::V6(address) => {
            if let Some(mapped) = address.to_ipv4_mapped() {
                return public_ip(IpAddr::V4(mapped));
            }
            let segments = address.segments();
            segments[0] & 0xe000 == 0x2000
                && !(segments[0] == 0x2001 && segments[1] < 0x0200)
                && !(segments[0] == 0x2001 && segments[1] == 0x0db8)
                && segments[0] != 0x2002
                && segments[0] != 0x3ffe
                && !(segments[0] == 0x3fff && segments[1] & 0xf000 == 0)
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Descriptor {
    #[serde(rename = "mediaType")]
    media_type: String,
    digest: String,
    size: u64,
    #[serde(default)]
    annotations: BTreeMap<String, String>,
}

#[derive(Deserialize)]
struct Manifest {
    #[serde(rename = "schemaVersion")]
    schema_version: u8,
    #[serde(rename = "mediaType")]
    media_type: String,
    config: Descriptor,
    layers: Vec<Descriptor>,
}

#[derive(Deserialize)]
struct ImageConfig {
    architecture: String,
    os: String,
}

#[derive(Serialize, Deserialize)]
struct Index {
    #[serde(rename = "schemaVersion")]
    schema_version: u8,
    manifests: Vec<Descriptor>,
}

fn produce_archive<R: ProcessRunner>(
    runner: &R,
    digest_root: &File,
    image: &RecipeBuildBaseImage,
    platform: &str,
    maximum_archive_bytes: u64,
    maximum_temporary_bytes: u64,
) -> Result<(), BaseImageError> {
    if maximum_archive_bytes == 0 || maximum_temporary_bytes == 0 {
        return Err(BaseImageError::Limit);
    }
    let source = registry_source(image)?;
    let mut manifest_file = TemporaryAt::create(digest_root, "manifest")?;
    let manifest_arguments =
        oras_arguments(&["manifest", "fetch"], &source, &source.exact_reference);
    let manifest_output = runner.run_to_file(
        Program::Oras,
        &manifest_arguments,
        Duration::from_secs(900),
        &mut manifest_file.file,
        MAX_JSON_BYTES.min(maximum_temporary_bytes),
    )?;
    if !manifest_output.success {
        return Err(BaseImageError::Invalid);
    }
    let manifest_bytes = read_bounded(&manifest_file.file, MAX_JSON_BYTES)?;
    drop(manifest_file);
    if format!("sha256:{}", hex_digest(&manifest_bytes)) != image.manifest_digest {
        return Err(BaseImageError::Invalid);
    }
    let manifest: Manifest =
        serde_json::from_slice(&manifest_bytes).map_err(|_| BaseImageError::Invalid)?;
    validate_manifest(&manifest)?;
    let descriptors = std::iter::once(&manifest.config)
        .chain(manifest.layers.iter())
        .cloned()
        .collect::<Vec<_>>();
    if descriptors
        .iter()
        .any(|descriptor| descriptor.size > maximum_temporary_bytes)
    {
        return Err(BaseImageError::Limit);
    }

    let mut annotations = BTreeMap::new();
    annotations.insert(
        "org.opencontainers.image.ref.name".to_owned(),
        source.reference_name.clone(),
    );
    let index = serde_json::to_vec(&Index {
        schema_version: 2,
        manifests: vec![Descriptor {
            media_type: manifest.media_type.clone(),
            digest: image.manifest_digest.clone(),
            size: manifest_bytes.len() as u64,
            annotations,
        }],
    })
    .map_err(|_| BaseImageError::Invalid)?;
    let archive_bytes = checked_archive_size(
        [
            OCI_LAYOUT.len() as u64,
            index.len() as u64,
            manifest_bytes.len() as u64,
        ]
        .into_iter()
        .chain(descriptors.iter().map(|descriptor| descriptor.size)),
    )?;
    if archive_bytes > maximum_archive_bytes {
        return Err(BaseImageError::Limit);
    }

    let mut output = TemporaryAt::create(digest_root, "archive")?;
    {
        let mut archive = tar::Builder::new(output.file.try_clone()?);
        append_bytes(&mut archive, "oci-layout", OCI_LAYOUT)?;
        append_bytes(&mut archive, "index.json", &index)?;
        append_bytes(
            &mut archive,
            &blob_path(&image.manifest_digest)?,
            &manifest_bytes,
        )?;
        for descriptor in &descriptors {
            let mut blob = TemporaryAt::create(digest_root, "blob")?;
            let reference = format!("{}@{}", source.repository, descriptor.digest);
            let arguments = oras_arguments(&["blob", "fetch"], &source, &reference);
            let fetched = runner.run_to_file(
                Program::Oras,
                &arguments,
                Duration::from_secs(3600),
                &mut blob.file,
                descriptor.size,
            )?;
            if !fetched.success
                || blob.file.metadata()?.len() != descriptor.size
                || sha256_file(&blob.file)? != descriptor.digest
            {
                return Err(BaseImageError::Invalid);
            }
            if descriptor.digest == manifest.config.digest {
                let config = read_bounded(&blob.file, MAX_JSON_BYTES)?;
                validate_platform(&config, platform)?;
            }
            blob.file.seek(SeekFrom::Start(0))?;
            append_file(
                &mut archive,
                &blob_path(&descriptor.digest)?,
                descriptor.size,
                &mut blob.file,
            )?;
        }
        archive.finish()?;
    }
    output.file.sync_all()?;
    let stored_bytes = output.file.metadata()?.len();
    if stored_bytes != archive_bytes {
        return Err(BaseImageError::Invalid);
    }
    if stored_bytes > maximum_archive_bytes {
        return Err(BaseImageError::Limit);
    }
    verify_archive(&output.file, image, platform, maximum_archive_bytes)?;
    match renameat_with(
        digest_root,
        output.name.as_str(),
        digest_root,
        "image.oci.tar",
        RenameFlags::NOREPLACE,
    ) {
        Ok(()) => {
            output.retained = true;
            digest_root.sync_all()?;
            Ok(())
        }
        Err(error) if error == rustix::io::Errno::EXIST => Ok(()),
        Err(error) => Err(BaseImageError::Io(std::io::Error::from(error))),
    }
}

fn oras_arguments(command: &[&str], source: &RegistrySource, reference: &str) -> Vec<String> {
    command
        .iter()
        .map(|value| (*value).to_owned())
        .chain(
            ["--output", "-", "--registry-config", ORAS_AUTH, "--resolve"]
                .into_iter()
                .map(str::to_owned),
        )
        .chain([source.resolve.clone(), reference.to_owned()])
        .collect()
}

fn validate_manifest(manifest: &Manifest) -> Result<(), BaseImageError> {
    if manifest.schema_version != 2
        || !matches!(manifest.media_type.as_str(), OCI_MANIFEST | DOCKER_MANIFEST)
        || !matches!(
            manifest.config.media_type.as_str(),
            OCI_CONFIG | DOCKER_CONFIG
        )
        || manifest.config.size == 0
        || manifest.config.size > MAX_JSON_BYTES
        || manifest.layers.len() > MAX_LAYERS
    {
        return Err(BaseImageError::Invalid);
    }
    let mut digests = BTreeSet::new();
    for descriptor in std::iter::once(&manifest.config).chain(manifest.layers.iter()) {
        if descriptor.size == 0
            || digest_hex(&descriptor.digest).is_none()
            || !digests.insert(&descriptor.digest)
        {
            return Err(BaseImageError::Invalid);
        }
    }
    Ok(())
}

fn validate_platform(config: &[u8], platform: &str) -> Result<(), BaseImageError> {
    let config: ImageConfig =
        serde_json::from_slice(config).map_err(|_| BaseImageError::Invalid)?;
    if platform != "linux/arm64" || config.os != "linux" || config.architecture != "arm64" {
        return Err(BaseImageError::Invalid);
    }
    Ok(())
}

fn verified_stored_image(
    file: File,
    image: &RecipeBuildBaseImage,
    platform: &str,
    maximum_bytes: u64,
) -> Result<StoredBaseImage, BaseImageError> {
    let metadata = file.metadata()?;
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.permissions().mode() & 0o022 != 0
        || metadata.len() == 0
    {
        return Err(BaseImageError::Invalid);
    }
    if metadata.len() > maximum_bytes {
        return Err(BaseImageError::Limit);
    }
    verify_archive(&file, image, platform, maximum_bytes)?;
    Ok(StoredBaseImage {
        bytes: metadata.len(),
        file,
    })
}

#[derive(Clone)]
struct EntryRecord {
    digest: String,
    size: u64,
}

fn verify_archive(
    file: &File,
    image: &RecipeBuildBaseImage,
    platform: &str,
    maximum_bytes: u64,
) -> Result<(), BaseImageError> {
    let metadata = file.metadata()?;
    if metadata.len() == 0 || metadata.len() > maximum_bytes {
        return Err(BaseImageError::Limit);
    }
    let entries = scan_archive(file)?;
    if entries.len() > MAX_LAYERS + 5 {
        return Err(BaseImageError::Invalid);
    }
    let layout = read_archive_entry(file, "oci-layout", MAX_JSON_BYTES)?;
    let layout: serde_json::Value =
        serde_json::from_slice(&layout).map_err(|_| BaseImageError::Invalid)?;
    if layout != serde_json::json!({"imageLayoutVersion": "1.0.0"}) {
        return Err(BaseImageError::Invalid);
    }
    let index: Index =
        serde_json::from_slice(&read_archive_entry(file, "index.json", MAX_JSON_BYTES)?)
            .map_err(|_| BaseImageError::Invalid)?;
    if index.schema_version != 2 || index.manifests.len() != 1 {
        return Err(BaseImageError::Invalid);
    }
    let descriptor = &index.manifests[0];
    let reference_name = image
        .reference
        .rsplit_once('@')
        .map(|(name, _)| name)
        .ok_or(BaseImageError::Invalid)?;
    if descriptor.digest != image.manifest_digest
        || descriptor
            .annotations
            .get("org.opencontainers.image.ref.name")
            .map(String::as_str)
            != Some(reference_name)
    {
        return Err(BaseImageError::Invalid);
    }
    let manifest_path = blob_path(&descriptor.digest)?;
    require_record(&entries, &manifest_path, descriptor)?;
    let manifest_raw = read_archive_entry(file, &manifest_path, MAX_JSON_BYTES)?;
    let manifest: Manifest =
        serde_json::from_slice(&manifest_raw).map_err(|_| BaseImageError::Invalid)?;
    validate_manifest(&manifest)?;
    if manifest.media_type != descriptor.media_type {
        return Err(BaseImageError::Invalid);
    }
    let config_path = blob_path(&manifest.config.digest)?;
    require_record(&entries, &config_path, &manifest.config)?;
    validate_platform(
        &read_archive_entry(file, &config_path, MAX_JSON_BYTES)?,
        platform,
    )?;
    let mut expected = BTreeSet::from([
        "oci-layout".to_owned(),
        "index.json".to_owned(),
        manifest_path,
        config_path,
    ]);
    for layer in &manifest.layers {
        let path = blob_path(&layer.digest)?;
        require_record(&entries, &path, layer)?;
        expected.insert(path);
    }
    if entries.keys().cloned().collect::<BTreeSet<_>>() != expected {
        return Err(BaseImageError::Invalid);
    }
    Ok(())
}

fn scan_archive(file: &File) -> Result<BTreeMap<String, EntryRecord>, BaseImageError> {
    let mut source = file.try_clone()?;
    source.seek(SeekFrom::Start(0))?;
    let mut archive = tar::Archive::new(source);
    let mut entries = BTreeMap::new();
    for entry in archive.entries()? {
        let mut entry = entry?;
        if !entry.header().entry_type().is_file() {
            return Err(BaseImageError::Invalid);
        }
        let path = entry.path().map_err(|_| BaseImageError::Invalid)?;
        if path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
        {
            return Err(BaseImageError::Invalid);
        }
        let path = path.to_str().ok_or(BaseImageError::Invalid)?.to_owned();
        if entries.contains_key(&path) {
            return Err(BaseImageError::Invalid);
        }
        let mut hasher = Sha256::new();
        let mut bytes = 0_u64;
        let mut buffer = [0_u8; 1024 * 1024];
        loop {
            let read = entry.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            bytes = bytes
                .checked_add(read as u64)
                .ok_or(BaseImageError::Invalid)?;
            hasher.update(&buffer[..read]);
        }
        entries.insert(
            path,
            EntryRecord {
                digest: format!("sha256:{}", hex::encode(hasher.finalize())),
                size: bytes,
            },
        );
    }
    Ok(entries)
}

fn read_archive_entry(
    file: &File,
    expected: &str,
    maximum_bytes: u64,
) -> Result<Vec<u8>, BaseImageError> {
    let mut source = file.try_clone()?;
    source.seek(SeekFrom::Start(0))?;
    let mut archive = tar::Archive::new(source);
    for entry in archive.entries()? {
        let mut entry = entry?;
        if entry.path().map_err(|_| BaseImageError::Invalid)? == Path::new(expected) {
            if entry.size() > maximum_bytes {
                return Err(BaseImageError::Limit);
            }
            let mut value = Vec::with_capacity(entry.size() as usize);
            entry.read_to_end(&mut value)?;
            return Ok(value);
        }
    }
    Err(BaseImageError::Invalid)
}

fn require_record(
    entries: &BTreeMap<String, EntryRecord>,
    path: &str,
    descriptor: &Descriptor,
) -> Result<(), BaseImageError> {
    let record = entries.get(path).ok_or(BaseImageError::Invalid)?;
    if record.size != descriptor.size || record.digest != descriptor.digest {
        return Err(BaseImageError::Invalid);
    }
    Ok(())
}

fn append_bytes(
    archive: &mut tar::Builder<File>,
    path: &str,
    value: &[u8],
) -> Result<(), BaseImageError> {
    append_file(
        archive,
        path,
        value.len() as u64,
        &mut std::io::Cursor::new(value),
    )
}

fn append_file<R: Read>(
    archive: &mut tar::Builder<File>,
    path: &str,
    size: u64,
    reader: &mut R,
) -> Result<(), BaseImageError> {
    let mut header = tar::Header::new_ustar();
    header.set_path(path).map_err(|_| BaseImageError::Invalid)?;
    header.set_size(size);
    header.set_mode(0o644);
    header.set_uid(0);
    header.set_gid(0);
    header.set_mtime(0);
    header.set_cksum();
    archive.append(&header, reader)?;
    Ok(())
}

fn read_bounded(file: &File, maximum_bytes: u64) -> Result<Vec<u8>, BaseImageError> {
    let size = file.metadata()?.len();
    if size == 0 {
        return Err(BaseImageError::Invalid);
    }
    if size > maximum_bytes || size > usize::MAX as u64 {
        return Err(BaseImageError::Limit);
    }
    let mut source = file.try_clone()?;
    source.seek(SeekFrom::Start(0))?;
    let mut value = Vec::with_capacity(size as usize);
    source
        .take(maximum_bytes.saturating_add(1))
        .read_to_end(&mut value)?;
    if value.len() as u64 != size {
        return Err(BaseImageError::Invalid);
    }
    Ok(value)
}

fn sha256_file(file: &File) -> Result<String, BaseImageError> {
    let mut source = file.try_clone()?;
    source.seek(SeekFrom::Start(0))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = source.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("sha256:{}", hex::encode(hasher.finalize())))
}

fn hex_digest(value: &[u8]) -> String {
    hex::encode(Sha256::digest(value))
}

fn digest_hex(value: &str) -> Option<&str> {
    let digest = value.strip_prefix("sha256:")?;
    (digest.len() == 64
        && digest
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()))
    .then_some(digest)
}

fn blob_path(digest: &str) -> Result<String, BaseImageError> {
    Ok(format!(
        "blobs/sha256/{}",
        digest_hex(digest).ok_or(BaseImageError::Invalid)?
    ))
}

fn checked_archive_size(mut sizes: impl Iterator<Item = u64>) -> Result<u64, BaseImageError> {
    sizes.try_fold(1024_u64, |total, size| {
        let padded = size
            .checked_add(511)
            .map(|value| value / 512 * 512)
            .ok_or(BaseImageError::Limit)?;
        total
            .checked_add(512)
            .and_then(|value| value.checked_add(padded))
            .ok_or(BaseImageError::Limit)
    })
}
