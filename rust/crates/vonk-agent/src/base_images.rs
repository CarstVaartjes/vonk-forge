//! Exact registry-to-OCI-archive staging for offline recipe builds.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom},
    net::IpAddr,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
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
const REGISTRY_TRANSFER_ATTEMPTS: usize = 3;
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
    #[error("base-image manifest transfer failed")]
    ManifestTransfer,
    #[error("base-image manifest transfer failed")]
    ManifestProcess(#[source] ProcessError),
    #[error("base-image manifest evidence is invalid")]
    ManifestEvidence,
    #[error("base-image blob transfer failed")]
    BlobTransfer,
    #[error("base-image blob transfer failed")]
    BlobProcess(#[source] ProcessError),
    #[error("base-image blob evidence is invalid")]
    BlobEvidence,
    #[error("base-image OCI archive evidence is invalid")]
    ArchiveEvidence,
    #[error("base-image storage is unavailable")]
    Io(#[from] std::io::Error),
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

    #[allow(clippy::too_many_arguments)] // One immutable transfer budget and its cancellation travel together.
    pub(crate) fn materialize_cancellable<R: ProcessRunner + ?Sized>(
        &self,
        runner: &R,
        image: &RecipeBuildBaseImage,
        platform: &str,
        maximum_archive_bytes: u64,
        maximum_temporary_bytes: u64,
        deadline: Instant,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<StoredBaseImage, BaseImageError> {
        let digest = exact_manifest_digest(image)?;
        // Serialize cache publication without waiting. Contention ends this
        // observation; the next accepted attempt re-observes completed bytes.
        let lock_name = format!("{digest}-lock");
        // A damaged generated lock entry is bookkeeping, not an admission gate.
        if open_regular_at(&self.sha256_root, &lock_name).is_err() {
            isolate_entry(&self.sha256_root, &lock_name)?;
        }
        let lock = File::from(
            openat2(
                &self.sha256_root,
                lock_name.as_str(),
                OFlags::RDWR | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                Mode::RUSR | Mode::WUSR,
                SAFE_RESOLUTION,
            )
            .map_err(std::io::Error::from)?,
        );
        rustix::fs::flock(&lock, rustix::fs::FlockOperation::NonBlockingLockExclusive)
            .map_err(std::io::Error::from)?;
        let digest_root = open_or_create_directory(&self.sha256_root, digest)?;
        let budget = TransferBudget {
            deadline,
            cancelled,
        };
        budget.check()?;
        match open_regular_at(&digest_root, "image.oci.tar") {
            Ok(Some(file)) => {
                if let Ok(stored) =
                    verified_stored_image(file, image, platform, maximum_archive_bytes, &budget)
                {
                    return Ok(stored);
                }
                budget.check()?;
                isolate_entry(&digest_root, "image.oci.tar")?;
            }
            Ok(None) => {}
            Err(_) => isolate_entry(&digest_root, "image.oci.tar")?,
        }
        produce_archive(
            runner,
            &digest_root,
            image,
            platform,
            maximum_archive_bytes,
            maximum_temporary_bytes,
            &budget,
        )?;
        let file =
            open_regular_at(&digest_root, "image.oci.tar")?.ok_or(BaseImageError::Invalid)?;
        verified_stored_image(file, image, platform, maximum_archive_bytes, &budget)
            .map_err(base_archive_error)
    }
}

// Rename the directory entry itself through an already owned descriptor. Never
// follow or delete uncertain cache targets; valid external bytes remain intact.
fn isolate_entry(parent: &File, name: &str) -> Result<(), BaseImageError> {
    let quarantine = format!(".damaged-{}", uuid::Uuid::new_v4());
    match renameat_with(
        parent,
        name,
        parent,
        quarantine.as_str(),
        RenameFlags::NOREPLACE,
    ) {
        Ok(()) => {
            parent.sync_all()?;
            Ok(())
        }
        Err(error) if error == rustix::io::Errno::NOENT => Ok(()),
        Err(error) => Err(std::io::Error::from(error).into()),
    }
}

struct TransferBudget<'a> {
    deadline: Instant,
    cancelled: &'a dyn Fn() -> bool,
}
impl TransferBudget<'_> {
    fn check(&self) -> Result<(), BaseImageError> {
        if (self.cancelled)() {
            return Err(BaseImageError::ManifestProcess(ProcessError::Cancelled));
        }
        if Instant::now() >= self.deadline {
            return Err(BaseImageError::ManifestProcess(ProcessError::Timeout));
        }
        Ok(())
    }
    fn remaining(&self) -> Result<Duration, BaseImageError> {
        self.check()?;
        Ok(self.deadline.saturating_duration_since(Instant::now()))
    }
}
struct BudgetReader<'a, R> {
    reader: R,
    budget: &'a TransferBudget<'a>,
}
impl<R: Read> Read for BudgetReader<'_, R> {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        self.budget
            .check()
            .map_err(|_| std::io::Error::from(std::io::ErrorKind::TimedOut))?;
        self.reader.read(buffer)
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
        Err(_) => {
            isolate_entry(parent, name)?;
            mkdirat(parent, name, Mode::RUSR | Mode::WUSR | Mode::XUSR)
                .map_err(std::io::Error::from)?;
            openat2(parent, name, flags, Mode::empty(), SAFE_RESOLUTION)
                .map_err(std::io::Error::from)?
        }
    };
    let file = File::from(descriptor);
    let metadata = file.metadata().map_err(BaseImageError::Io)?;
    if !metadata.is_dir()
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.permissions().mode() & 0o022 != 0
    {
        isolate_entry(parent, name)?;
        mkdirat(parent, name, Mode::RUSR | Mode::WUSR | Mode::XUSR)
            .map_err(std::io::Error::from)?;
        let descriptor = openat2(parent, name, flags, Mode::empty(), SAFE_RESOLUTION)
            .map_err(std::io::Error::from)?;
        return Ok(File::from(descriptor));
    }
    Ok(file)
}

fn open_regular_at(parent: &File, name: &str) -> Result<Option<File>, BaseImageError> {
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK;
    match openat2(parent, name, flags, Mode::empty(), SAFE_RESOLUTION) {
        Ok(descriptor) => {
            let file = File::from(descriptor);
            if !file.metadata()?.is_file() {
                return Err(BaseImageError::Invalid);
            }
            Ok(Some(file))
        }
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

fn registry_source<R: ProcessRunner + ?Sized>(
    runner: &R,
    image: &RecipeBuildBaseImage,
    budget: &TransferBudget<'_>,
) -> Result<RegistrySource, BaseImageError> {
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
    let answer = runner
        .run_cancellable(
            Program::Getent,
            &["ahosts".to_owned(), hostname.to_owned()],
            budget.remaining()?.min(Duration::from_secs(15)),
            budget.cancelled,
        )
        .map_err(BaseImageError::ManifestProcess)?;
    if !answer.success {
        return Err(BaseImageError::ManifestTransfer);
    }
    let text = std::str::from_utf8(&answer.stdout).map_err(|_| BaseImageError::ManifestTransfer)?;
    let addresses = text
        .lines()
        .map(|line| {
            line.split_whitespace()
                .next()
                .ok_or(BaseImageError::ManifestTransfer)?
                .parse::<IpAddr>()
                .map_err(|_| BaseImageError::ManifestTransfer)
        })
        .collect::<Result<Vec<_>, _>>()?;
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

fn produce_archive<R: ProcessRunner + ?Sized>(
    runner: &R,
    digest_root: &File,
    image: &RecipeBuildBaseImage,
    platform: &str,
    maximum_archive_bytes: u64,
    maximum_temporary_bytes: u64,
    budget: &TransferBudget<'_>,
) -> Result<(), BaseImageError> {
    if maximum_archive_bytes == 0 || maximum_temporary_bytes == 0 {
        return Err(BaseImageError::Limit);
    }
    let source = registry_source(runner, image, budget)?;
    let mut manifest_file = TemporaryAt::create(digest_root, "manifest")?;
    let manifest_arguments =
        oras_arguments(&["manifest", "fetch"], &source, &source.exact_reference);
    let mut manifest_transferred = false;
    for attempt in 0..REGISTRY_TRANSFER_ATTEMPTS {
        let manifest_output = runner
            .run_to_file_cancellable(
                Program::Oras,
                &manifest_arguments,
                budget.remaining()?,
                &mut manifest_file.file,
                MAX_JSON_BYTES.min(maximum_temporary_bytes),
                budget.cancelled,
            )
            .map_err(BaseImageError::ManifestProcess);
        budget.check()?;
        if manifest_output.is_ok_and(|output| output.success) {
            manifest_transferred = true;
            break;
        }
        if attempt + 1 < REGISTRY_TRANSFER_ATTEMPTS {
            std::thread::sleep(
                Duration::from_millis(50 * (attempt as u64 + 1)).min(budget.remaining()?),
            );
        }
    }
    if !manifest_transferred {
        return Err(BaseImageError::ManifestTransfer);
    }
    let manifest_bytes =
        read_bounded(&manifest_file.file, MAX_JSON_BYTES).map_err(manifest_evidence_error)?;
    drop(manifest_file);
    if format!("sha256:{}", hex_digest(&manifest_bytes)) != image.manifest_digest {
        return Err(BaseImageError::ManifestEvidence);
    }
    let manifest: Manifest =
        serde_json::from_slice(&manifest_bytes).map_err(|_| BaseImageError::ManifestEvidence)?;
    let descriptors = validate_manifest(&manifest).map_err(manifest_evidence_error)?;
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
            // Completed blobs are content checkpoints for an interrupted
            // multi-layer image. Archive retries reuse them without refetching.
            let blob_name = format!(
                "blob-{}",
                digest_hex(&descriptor.digest).ok_or(BaseImageError::Invalid)?
            );
            let mut retained = match open_regular_at(digest_root, &blob_name) {
                Ok(Some(file))
                    if file.metadata().is_ok_and(|metadata| {
                        metadata.is_file() && metadata.len() == descriptor.size
                    }) && sha256_file(&file, budget).ok().as_deref()
                        == Some(descriptor.digest.as_str()) =>
                {
                    Some(file)
                }
                Ok(None) => None,
                _ => {
                    budget.check()?;
                    isolate_entry(digest_root, &blob_name)?;
                    None
                }
            };
            if retained.is_none() {
                let mut transfer = TemporaryAt::create(digest_root, "blob")?;
                let reference = format!("{}@{}", source.repository, descriptor.digest);
                let arguments = oras_arguments(&["blob", "fetch"], &source, &reference);
                let mut transferred = false;
                for attempt in 0..REGISTRY_TRANSFER_ATTEMPTS {
                    let fetched = runner.run_to_file_cancellable(
                        Program::Oras,
                        &arguments,
                        budget.remaining()?,
                        &mut transfer.file,
                        descriptor.size,
                        budget.cancelled,
                    );
                    budget.check()?;
                    if fetched.is_ok_and(|output| output.success) {
                        transferred = true;
                        break;
                    }
                    if attempt + 1 < REGISTRY_TRANSFER_ATTEMPTS {
                        std::thread::sleep(
                            Duration::from_millis(50 * (attempt as u64 + 1))
                                .min(budget.remaining()?),
                        );
                    }
                }
                if !transferred {
                    return Err(BaseImageError::BlobTransfer);
                }
                if transfer.file.metadata()?.len() != descriptor.size
                    || sha256_file(&transfer.file, budget)? != descriptor.digest
                {
                    return Err(BaseImageError::BlobEvidence);
                }
                budget.check()?;
                transfer.file.sync_all()?;
                renameat_with(
                    digest_root,
                    transfer.name.as_str(),
                    digest_root,
                    blob_name.as_str(),
                    RenameFlags::NOREPLACE,
                )
                .map_err(std::io::Error::from)?;
                transfer.retained = true;
                digest_root.sync_all()?;
                retained = Some(transfer.file.try_clone()?);
            }
            let mut blob = retained.ok_or(BaseImageError::BlobTransfer)?;
            if descriptor.digest == manifest.config.digest {
                let config = read_bounded(&blob, MAX_JSON_BYTES).map_err(blob_evidence_error)?;
                validate_platform(&config, platform).map_err(blob_evidence_error)?;
            }
            blob.seek(SeekFrom::Start(0))?;
            append_file(
                &mut archive,
                &blob_path(&descriptor.digest)?,
                descriptor.size,
                &mut BudgetReader {
                    reader: &mut blob,
                    budget,
                },
            )?;
        }
        archive.finish()?;
    }
    output.file.sync_all()?;
    let stored_bytes = output.file.metadata()?.len();
    if stored_bytes != archive_bytes {
        return Err(BaseImageError::ArchiveEvidence);
    }
    if stored_bytes > maximum_archive_bytes {
        return Err(BaseImageError::Limit);
    }
    verify_archive(&output.file, image, platform, maximum_archive_bytes, budget)
        .map_err(base_archive_error)?;
    budget.check()?;
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

fn manifest_evidence_error(error: BaseImageError) -> BaseImageError {
    match error {
        BaseImageError::Limit => BaseImageError::Limit,
        BaseImageError::Io(error) => BaseImageError::Io(error),
        _ => BaseImageError::ManifestEvidence,
    }
}

fn blob_evidence_error(error: BaseImageError) -> BaseImageError {
    match error {
        BaseImageError::Limit => BaseImageError::Limit,
        BaseImageError::Io(error) => BaseImageError::Io(error),
        _ => BaseImageError::BlobEvidence,
    }
}

fn base_archive_error(error: BaseImageError) -> BaseImageError {
    match error {
        BaseImageError::Limit => BaseImageError::Limit,
        BaseImageError::Io(error) => BaseImageError::Io(error),
        _ => BaseImageError::ArchiveEvidence,
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

fn validate_manifest(manifest: &Manifest) -> Result<Vec<Descriptor>, BaseImageError> {
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
    let mut descriptors = Vec::new();
    let mut digest_indexes = BTreeMap::new();
    for descriptor in std::iter::once(&manifest.config).chain(manifest.layers.iter()) {
        if descriptor.size == 0 || digest_hex(&descriptor.digest).is_none() {
            return Err(BaseImageError::Invalid);
        }
        if let Some(index) = digest_indexes.get(&descriptor.digest) {
            let existing: &Descriptor = &descriptors[*index];
            if existing.size != descriptor.size || existing.media_type != descriptor.media_type {
                return Err(BaseImageError::Invalid);
            }
            continue;
        }
        digest_indexes.insert(descriptor.digest.clone(), descriptors.len());
        descriptors.push(descriptor.clone());
    }
    Ok(descriptors)
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
    budget: &TransferBudget<'_>,
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
    verify_archive(&file, image, platform, maximum_bytes, budget)?;
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

/// The exact `oci-layout` document of an image layout: one known key and no other.
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct OciLayout {
    image_layout_version: String,
}

fn verify_archive(
    file: &File,
    image: &RecipeBuildBaseImage,
    platform: &str,
    maximum_bytes: u64,
    budget: &TransferBudget<'_>,
) -> Result<(), BaseImageError> {
    let metadata = file.metadata()?;
    if metadata.len() == 0 || metadata.len() > maximum_bytes {
        return Err(BaseImageError::Limit);
    }
    let entries = scan_archive(file, budget)?;
    if entries.len() > MAX_LAYERS + 5 {
        return Err(BaseImageError::Invalid);
    }
    let layout = read_archive_entry(file, "oci-layout", MAX_JSON_BYTES, budget)?;
    let layout: OciLayout = serde_json::from_slice(&layout).map_err(|_| BaseImageError::Invalid)?;
    if layout.image_layout_version != "1.0.0" {
        return Err(BaseImageError::Invalid);
    }
    let index: Index = serde_json::from_slice(&read_archive_entry(
        file,
        "index.json",
        MAX_JSON_BYTES,
        budget,
    )?)
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
    let manifest_raw = read_archive_entry(file, &manifest_path, MAX_JSON_BYTES, budget)?;
    let manifest: Manifest =
        serde_json::from_slice(&manifest_raw).map_err(|_| BaseImageError::Invalid)?;
    validate_manifest(&manifest)?;
    if manifest.media_type != descriptor.media_type {
        return Err(BaseImageError::Invalid);
    }
    let config_path = blob_path(&manifest.config.digest)?;
    require_record(&entries, &config_path, &manifest.config)?;
    validate_platform(
        &read_archive_entry(file, &config_path, MAX_JSON_BYTES, budget)?,
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

fn scan_archive(
    file: &File,
    budget: &TransferBudget<'_>,
) -> Result<BTreeMap<String, EntryRecord>, BaseImageError> {
    let mut source = file.try_clone()?;
    source.seek(SeekFrom::Start(0))?;
    let mut archive = tar::Archive::new(BudgetReader {
        reader: source,
        budget,
    });
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
    budget: &TransferBudget<'_>,
) -> Result<Vec<u8>, BaseImageError> {
    let mut source = file.try_clone()?;
    source.seek(SeekFrom::Start(0))?;
    let mut archive = tar::Archive::new(BudgetReader {
        reader: source,
        budget,
    });
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

fn sha256_file(file: &File, budget: &TransferBudget<'_>) -> Result<String, BaseImageError> {
    let mut source = file.try_clone()?;
    source.seek(SeekFrom::Start(0))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        budget.check()?;
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
