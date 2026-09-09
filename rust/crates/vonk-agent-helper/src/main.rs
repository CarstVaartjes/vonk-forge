#![forbid(unsafe_code)]

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use ring::signature::{Ed25519KeyPair, KeyPair};
use rustix::net::sockopt::socket_peercred;
use vonk_agent_helper::operations::{
    ManagedRoots, OperationError, OperationExecutor, ProcessCommandRunner,
};
use vonk_agent_helper::protocol::{
    GrantVerifier, HelperError, HostOperation, PeerIdentity, parse_request, read_frame,
    sign_observation_receipt, write_frame,
};
use vonk_agent_protocol::generated::HostHelperResponse as HelperResponse;

const GRANT_KEY: &str = "/etc/vonk-forge-agent/host-helper-authority.pub";
const RELEASE_KEY: &str = "/usr/share/keyrings/vonk-forge-release.pub";
const OBSERVATION_RECEIPT_PRIVATE_KEY: &str = "/var/lib/vonk-forge/helper/observation-receipt.pk8";
const OBSERVATION_RECEIPT_PUBLIC_KEY: &str = "/etc/vonk-forge-agent/observation-receipt.pub";
const AGENT_CONFIG: &str = "/etc/vonk-forge-agent/agent.toml";
const REQUEST_LEDGER: &str = "/var/lib/vonk-forge/helper/requests";
const DATA_ROOT: &str = "/var/lib/vonk-forge";
const AGENT_DATA_ROOT: &str = "/var/lib/vonk-forge-agent";
const RUNTIME_REQUEST_ROOT: &str = "/run/vonk-forge-agent/runtime-requests";
const PACKAGE_CUSTODY_ROOT: &str = "/run/vonk-forge-package-candidates";
const AGENT_GROUP: &str = "vonk-agent";
const MAX_CONCURRENT_REQUESTS: usize = 8;

struct WorkerPermit(Arc<AtomicUsize>);

impl Drop for WorkerPermit {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

fn acquire_worker(counter: &Arc<AtomicUsize>) -> Option<WorkerPermit> {
    counter
        .fetch_update(Ordering::AcqRel, Ordering::Acquire, |current| {
            (current < MAX_CONCURRENT_REQUESTS).then_some(current + 1)
        })
        .ok()
        .map(|_| WorkerPermit(Arc::clone(counter)))
}

struct HelperRejection {
    request_id: Option<String>,
    error_code: &'static str,
    exit_code: Option<i32>,
    detail: String,
}

impl HelperRejection {
    fn new(error_code: &'static str, detail: impl Into<String>) -> Self {
        Self {
            request_id: None,
            error_code,
            exit_code: None,
            detail: detail.into(),
        }
    }

    fn for_request(
        request_id: impl Into<String>,
        error_code: &'static str,
        detail: impl Into<String>,
    ) -> Self {
        Self {
            request_id: Some(request_id.into()),
            error_code,
            exit_code: None,
            detail: detail.into(),
        }
    }

    fn for_operation(
        request_id: impl Into<String>,
        operation: &HostOperation,
        error: OperationError,
    ) -> Self {
        let package_install = matches!(operation, HostOperation::InstallVonkDebOperation(_));
        let (error_code, exit_code) = match error {
            OperationError::InvalidArtifact if package_install => {
                ("package_verification_failed", None)
            }
            OperationError::PackagePreflightFailed if package_install => {
                ("package_preflight_failed", None)
            }
            OperationError::PackageMetadataInvalid if package_install => {
                ("package_metadata_failed", None)
            }
            OperationError::PackageInstallFailed { exit_code, .. } if package_install => (
                "package_install_failed",
                exit_code.filter(|code| (0..=255).contains(code)),
            ),
            OperationError::UnsafePath | OperationError::Io(_) if package_install => {
                ("package_custody_failed", None)
            }
            OperationError::RuntimeImageLoadFailed => ("runtime_image_load_failed", None),
            OperationError::RuntimeImageInspectFailed => ("runtime_image_inspect_failed", None),
            OperationError::RuntimeImageIdentityInvalid => ("runtime_image_identity_invalid", None),
            OperationError::RuntimeImageReceiptFailed => ("runtime_image_receipt_failed", None),
            OperationError::InvalidOperation => ("operation_invalid", None),
            OperationError::UnsafePath => ("operation_unsafe_path", None),
            OperationError::InvalidArtifact => ("operation_invalid_artifact", None),
            OperationError::CommandFailed => ("operation_command_failed", None),
            OperationError::StopUncertain => ("operation_stop_uncertain", None),
            OperationError::Io(_) => ("operation_io", None),
            _ => ("operation_failed", None),
        };
        Self {
            request_id: Some(request_id.into()),
            error_code,
            exit_code,
            detail: error.safe_detail().to_owned(),
        }
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("vonk-agent-helper: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let arguments: Vec<_> = std::env::args().skip(1).collect();
    if let [operation, version, agent, helper] = arguments.as_slice()
        && operation == "--validate-package-rollback"
    {
        return vonk_agent_helper::package_rollback::Store::system()
            .validate_maintainer_rollback(version, agent, helper);
    }
    if arguments == ["--package-rollback-watchdog"] {
        return vonk_agent_helper::package_rollback::Store::system().watch();
    }
    if !arguments.is_empty() {
        return Err("unknown helper operation".into());
    }

    let grant_key = load_root_public_key(Path::new(GRANT_KEY))?;
    let release_key = load_root_public_key(Path::new(RELEASE_KEY))?;
    let group_gid = group_gid(Path::new("/etc/group"), AGENT_GROUP)?;
    let observation_receipt_public_key =
        load_root_binary_public_key(Path::new(OBSERVATION_RECEIPT_PUBLIC_KEY), group_gid)?;
    let observation_receipt_signer =
        load_root_private_key(Path::new(OBSERVATION_RECEIPT_PRIVATE_KEY))?;
    if observation_receipt_signer.public_key().as_ref() != observation_receipt_public_key {
        return Err("observation receipt key pair does not match".to_owned());
    }
    let agent_uid = user_uid(Path::new("/etc/passwd"), AGENT_GROUP)?;
    let node_id = node_id_from_config(&read_root_text(Path::new(AGENT_CONFIG), 64 * 1024)?)?;
    let verifier = Arc::new(GrantVerifier::new(&grant_key, group_gid).map_err(display)?);
    let executor = OperationExecutor::new(
        ManagedRoots::under(Path::new(DATA_ROOT))
            .with_agent_data(Path::new(AGENT_DATA_ROOT))
            .with_runtime_requests(Path::new(RUNTIME_REQUEST_ROOT))
            .with_package_custody(Path::new(PACKAGE_CUSTODY_ROOT)),
        &release_key,
        ProcessCommandRunner,
        Some(0),
    )
    .map_err(display)?
    .with_package_owner(agent_uid)
    .with_runtime_request_owner(agent_uid);
    executor.prepare_package_custody().map_err(display)?;
    let executor = Arc::new(executor);
    let observation_receipt_signer = Arc::new(observation_receipt_signer);

    let mut sockets = sd_listen_fds::get().map_err(display)?;
    if sockets.len() != 1 {
        return Err("exactly one systemd socket is required".to_owned());
    }
    let (name, descriptor) = sockets.pop().expect("length checked");
    if name.as_deref().is_some_and(|value| value != "helper") {
        return Err("systemd socket name is invalid".to_owned());
    }
    let listener: UnixListener = descriptor.into();
    let workers = Arc::new(AtomicUsize::new(0));
    let node_id: Arc<str> = Arc::from(node_id);
    for connection in listener.incoming() {
        match connection {
            Ok(mut stream) => {
                let Some(permit) = acquire_worker(&workers) else {
                    reject(
                        &mut stream,
                        &HelperRejection::new(
                            "concurrency_limit",
                            "concurrent request limit reached",
                        ),
                    );
                    continue;
                };
                let verifier = Arc::clone(&verifier);
                let executor = Arc::clone(&executor);
                let node_id = Arc::clone(&node_id);
                let observation_receipt_signer = Arc::clone(&observation_receipt_signer);
                if let Err(error) = thread::Builder::new()
                    .name("vonk-helper-request".to_owned())
                    .spawn(move || {
                        let _permit = permit;
                        if let Err(error) = handle(
                            &mut stream,
                            &verifier,
                            &executor,
                            &node_id,
                            &observation_receipt_signer,
                        ) {
                            reject(&mut stream, &error);
                        }
                    })
                {
                    eprintln!("vonk-agent-helper: request worker failed: {error}");
                }
            }
            Err(error) => eprintln!("vonk-agent-helper: accept failed: {error}"),
        }
    }
    Ok(())
}

fn reject(stream: &mut UnixStream, error: &HelperRejection) {
    let Ok(request_id) = error.request_id.as_deref().map(str::parse).transpose() else {
        eprintln!("vonk-agent-helper: invalid rejection request identity");
        return;
    };
    let Ok(exit_code) = error.exit_code.map(u32::try_from).transpose() else {
        eprintln!("vonk-agent-helper: invalid rejection exit code");
        return;
    };
    let response = HelperResponse {
        diagnostic: (error.error_code == "package_install_failed")
            .then(|| error.detail.chars().take(8192).collect()),
        schema_version: 1,
        request_id,
        status: "rejected".parse().expect("declared helper response status"),
        evidence_sha256: None,
        exit_code,
        error_code: Some(error.error_code.to_owned()),
        observation_receipt: None,
    };
    if let Ok(body) = vonk_agent_protocol::canonical_generated_json(&response) {
        let _ = write_frame(stream, &body);
    }
    eprintln!("vonk-agent-helper: request rejected: {}", error.detail);
}

fn handle(
    stream: &mut UnixStream,
    verifier: &GrantVerifier,
    executor: &OperationExecutor<ProcessCommandRunner>,
    node_id: &str,
    observation_receipt_signer: &Ed25519KeyPair,
) -> Result<(), HelperRejection> {
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .map_err(|_| {
            HelperRejection::new("request_invalid", "helper socket configuration failed")
        })?;
    stream
        .set_write_timeout(Some(Duration::from_secs(10)))
        .map_err(|_| {
            HelperRejection::new("request_invalid", "helper socket configuration failed")
        })?;
    let peer = peer_identity(stream)
        .map_err(|error| HelperRejection::new("peer_identity_invalid", error))?;
    let raw = read_frame(stream)
        .map_err(|error| HelperRejection::new("request_invalid", error.safe_detail()))?;
    let request = parse_request(&raw)
        .map_err(|error| HelperRejection::new("grant_invalid", error.safe_detail()))?;
    let request_id = request.claims.request_id.to_string();
    if request.claims.node_id != node_id {
        return Err(HelperRejection::new(
            "grant_node_mismatch",
            "grant is for a different node",
        ));
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| HelperRejection::new("grant_invalid", "system clock is unavailable"))?
        .as_secs() as i64;
    verifier
        .authorize(&request, &peer, now)
        .map_err(|error| HelperRejection::new("grant_unauthorized", error.safe_detail()))?;
    claim_once(&request_id).map_err(|error| {
        let error_code = if error == "request grant was already consumed" {
            "request_replayed"
        } else {
            "request_ledger_failed"
        };
        HelperRejection::for_request(&request_id, error_code, error)
    })?;
    let outcome = executor
        .execute_for_node(&request.claims.operation, Some(node_id))
        .map_err(|error| {
            HelperRejection::for_operation(&request_id, &request.claims.operation, error)
        })?;
    let observation_receipt = match (&request.claims.operation, outcome.recipe_run_observation) {
        (
            vonk_agent_helper::protocol::HostOperation::ExecuteContainerRuntimeRequestOperation(
                vonk_agent_protocol::generated::ExecuteContainerRuntimeRequestOperation {
                    action: vonk_agent_helper::protocol::ContainerRuntimeAction::RunInspect,
                    request_sha256,
                    observation_identity_sha256: Some(observation_identity_sha256),
                    ..
                },
            ),
            Some(observation_outcome),
        ) => Some(
            sign_observation_receipt(
                observation_receipt_signer,
                node_id,
                request.claims.request_id,
                request_sha256,
                observation_identity_sha256,
                observation_outcome,
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_err(|_error| {
                        HelperRejection::for_request(
                            &request_id,
                            "operation_failed",
                            "system clock is unavailable",
                        )
                    })?
                    .as_secs() as i64,
            )
            .map_err(|error| {
                HelperRejection::for_request(&request_id, "operation_failed", error.safe_detail())
            })?,
        ),
        (_, None) => None,
        _ => {
            return Err(HelperRejection::for_request(
                &request_id,
                "operation_failed",
                "runtime inspection outcome did not match its grant",
            ));
        }
    };
    let response = HelperResponse {
        diagnostic: None,
        schema_version: 1,
        request_id: Some(request.claims.request_id),
        status: outcome.status.parse().map_err(|_| {
            HelperRejection::for_request(
                &request_id,
                "operation_failed",
                "invalid operation response status",
            )
        })?,
        evidence_sha256: Some(outcome.evidence_sha256),
        exit_code: outcome
            .exit_code
            .map(u32::try_from)
            .transpose()
            .map_err(|_| {
                HelperRejection::for_request(
                    &request_id,
                    "operation_failed",
                    "invalid operation exit code",
                )
            })?,
        error_code: None,
        observation_receipt,
    };
    let body = vonk_agent_protocol::canonical_generated_json(&response).map_err(|_error| {
        HelperRejection::for_request(
            &request_id,
            "operation_failed",
            "helper response encoding failed",
        )
    })?;
    write_frame(stream, &body).map_err(|error| {
        HelperRejection::for_request(&request_id, "operation_failed", error.safe_detail())
    })
}

fn claim_once(request_id: &str) -> Result<(), String> {
    let root = Path::new(REQUEST_LEDGER);
    let metadata = fs::symlink_metadata(root)
        .map_err(|_| "request ledger could not be inspected".to_owned())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() || metadata.uid() != 0 {
        return Err("request ledger is unsafe".to_owned());
    }
    let marker = root.join(request_id);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&marker)
        .map_err(|_| "request grant was already consumed".to_owned())?;
    file.write_all(b"pending\n")
        .map_err(|_| "request ledger could not be updated".to_owned())?;
    file.sync_all()
        .map_err(|_| "request ledger could not be synchronized".to_owned())?;
    OpenOptions::new()
        .read(true)
        .open(root)
        .and_then(|directory| directory.sync_all())
        .map_err(|_| "request ledger could not be synchronized".to_owned())
}

fn peer_identity(stream: &UnixStream) -> Result<PeerIdentity, String> {
    let credentials = socket_peercred(stream).map_err(display)?;
    let pid = credentials.pid.as_raw_pid();
    let status = fs::read_to_string(format!("/proc/{pid}/status")).map_err(display)?;
    let mut observed_uid = None;
    let mut groups = None;
    for line in status.lines() {
        if let Some(value) = line.strip_prefix("Uid:") {
            let values = parse_ids(value)?;
            if values.len() != 4
                || values
                    .iter()
                    .any(|value| *value != credentials.uid.as_raw())
            {
                return Err("peer credentials changed".to_owned());
            }
            observed_uid = values.first().copied();
        } else if let Some(value) = line.strip_prefix("Groups:") {
            groups = Some(parse_ids(value)?);
        }
    }
    Ok(PeerIdentity {
        uid: observed_uid.ok_or_else(|| "peer UID is unavailable".to_owned())?,
        primary_gid: credentials.gid.as_raw(),
        supplementary_gids: groups.ok_or_else(|| "peer groups are unavailable".to_owned())?,
    })
}

fn parse_ids(value: &str) -> Result<Vec<u32>, String> {
    value
        .split_ascii_whitespace()
        .map(|value| value.parse::<u32>().map_err(display))
        .collect()
}

fn load_root_public_key(path: &Path) -> Result<[u8; 32], String> {
    let text = read_root_text(path, 128)?;
    let value = hex::decode(text).map_err(display)?;
    value
        .try_into()
        .map_err(|_| "public key must contain 32 bytes".to_owned())
}

fn load_root_binary_public_key(path: &Path, group_gid: u32) -> Result<[u8; 32], String> {
    let raw = read_root_bytes(path, 32, 32, 0, group_gid, 0o640)?;
    raw.try_into()
        .map_err(|_| "public key must contain 32 bytes".to_owned())
}

fn load_root_private_key(path: &Path) -> Result<Ed25519KeyPair, String> {
    let raw = read_root_bytes(path, 1, 128, 0, 0, 0o600)?;
    parse_observation_private_key(&raw)
}

fn parse_observation_private_key(raw: &[u8]) -> Result<Ed25519KeyPair, String> {
    // The Debian maintainer scripts deliberately use the system OpenSSL to
    // generate this root-owned key. OpenSSL emits an Ed25519 PKCS#8 v1
    // document, while ring's checked `from_pkcs8` accepts only v2 documents
    // that embed a public key. Accept both encodings here, then compare the
    // derived public key with the separately root-owned public-key file during
    // helper startup. That preserves the key-pair consistency check without
    // making helper availability depend on the host OpenSSL version.
    Ed25519KeyPair::from_pkcs8_maybe_unchecked(raw)
        .map_err(|_| "observation receipt private key is invalid".to_owned())
}

fn read_root_bytes(
    path: &Path,
    minimum_bytes: u64,
    maximum_bytes: u64,
    expected_uid: u32,
    expected_gid: u32,
    required_mode: u32,
) -> Result<Vec<u8>, String> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(path)
        .map_err(display)?;
    let metadata = file.metadata().map_err(display)?;
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.uid() != expected_uid
        || metadata.gid() != expected_gid
        || metadata.permissions().mode() & 0o777 != required_mode
        || metadata.len() < minimum_bytes
        || metadata.len() > maximum_bytes
    {
        return Err(format!("{} is unsafe", path.display()));
    }
    let mut raw = Vec::with_capacity(metadata.len() as usize);
    Read::by_ref(&mut file)
        .take(maximum_bytes + 1)
        .read_to_end(&mut raw)
        .map_err(display)?;
    if raw.len() as u64 != metadata.len() {
        return Err(format!("{} changed while being read", path.display()));
    }
    Ok(raw)
}

fn read_root_text(path: &Path, maximum_bytes: u64) -> Result<String, String> {
    let metadata = fs::symlink_metadata(path).map_err(display)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.uid() != 0
        || metadata.permissions().mode() & 0o022 != 0
        || metadata.len() == 0
        || metadata.len() > maximum_bytes
    {
        return Err(format!("{} is unsafe", path.display()));
    }
    let value = fs::read_to_string(path).map_err(display)?;
    Ok(value.trim_end().to_owned())
}

fn group_gid(path: &Path, name: &str) -> Result<u32, String> {
    let groups = read_root_text(path, 1024 * 1024)?;
    for line in groups.lines() {
        let fields: Vec<_> = line.split(':').collect();
        if fields.len() == 4 && fields[0] == name {
            return fields[2].parse().map_err(display);
        }
    }
    Err(format!("required group {name} does not exist"))
}

fn user_uid(path: &Path, name: &str) -> Result<u32, String> {
    let users = read_root_text(path, 1024 * 1024)?;
    for line in users.lines() {
        let fields: Vec<_> = line.split(':').collect();
        if fields.len() == 7 && fields[0] == name {
            return fields[2].parse().map_err(display);
        }
    }
    Err(format!("required user {name} does not exist"))
}

fn node_id_from_config(config: &str) -> Result<String, String> {
    let mut node_id = None;
    for line in config.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        let Some((name, value)) = line.split_once('=') else {
            continue;
        };
        if name.trim() != "node_id" {
            continue;
        }
        if node_id.is_some() {
            return Err("agent configuration has duplicate node ID".to_owned());
        }
        let value = value.trim();
        let value = value
            .strip_prefix('"')
            .and_then(|value| value.strip_suffix('"'))
            .ok_or_else(|| "agent node ID is invalid".to_owned())?;
        if value.len() != 36
            || !value.starts_with("spk_")
            || !value[4..]
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err("agent node ID is invalid".to_owned());
        }
        node_id = Some(value.to_owned());
    }
    node_id.ok_or_else(|| "agent configuration has no node ID".to_owned())
}

fn display(_error: impl std::fmt::Display) -> String {
    // Startup and local I/O errors can carry credential paths or command
    // arguments. Keep the service boundary useful without serializing them.
    "helper local operation failed".to_owned()
}

#[allow(dead_code)]
fn _classify_protocol_error(error: HelperError) -> String {
    error.to_string()
}

#[cfg(test)]
mod tests {
    use super::{
        HelperRejection, HelperResponse, MAX_CONCURRENT_REQUESTS, acquire_worker,
        parse_observation_private_key,
    };
    use ring::signature::KeyPair;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    use vonk_agent_helper::{
        operations::OperationError,
        protocol::{ContainerRuntimeAction, HostOperation},
    };

    #[test]
    fn helper_request_concurrency_is_bounded_and_reusable() {
        let counter = Arc::new(AtomicUsize::new(0));
        let permits = (0..MAX_CONCURRENT_REQUESTS)
            .map(|_| acquire_worker(&counter).unwrap())
            .collect::<Vec<_>>();

        assert!(acquire_worker(&counter).is_none());
        drop(permits);
        assert_eq!(counter.load(Ordering::Acquire), 0);
        assert!(acquire_worker(&counter).is_some());
    }

    #[test]
    fn openssl_ed25519_pkcs8_v1_key_is_accepted() {
        // RFC 8410 PKCS#8 v1 wrapper around a deterministic 32-byte seed, the
        // same document shape produced by `openssl genpkey -algorithm ED25519
        // -outform DER` in the package post-install script.
        let mut document = hex::decode("302e020100300506032b657004220420").unwrap();
        document.extend(0_u8..32);

        let key = parse_observation_private_key(&document).unwrap();

        assert_eq!(key.public_key().as_ref().len(), 32);
        assert!(parse_observation_private_key(b"not-pkcs8").is_err());
    }

    #[test]
    fn framed_rejection_uses_the_shared_response_contract() {
        let (mut client, mut server) = std::os::unix::net::UnixStream::pair().unwrap();
        super::reject(
            &mut server,
            &HelperRejection::new("request_invalid", "private diagnostic"),
        );
        let bytes = vonk_agent_helper::protocol::read_frame(&mut client).unwrap();
        let response: HelperResponse = vonk_agent_protocol::parse_strict(&bytes).unwrap();
        assert_eq!(response.status, "rejected");
        assert!(response.request_id.is_none());
        assert!(response.evidence_sha256.is_none());
        assert_eq!(response.error_code.as_deref(), Some("request_invalid"));
        assert_eq!(
            vonk_agent_protocol::canonical_generated_json(&response).unwrap(),
            bytes
        );
        assert!(
            !String::from_utf8(bytes)
                .unwrap()
                .contains("private diagnostic")
        );
    }

    #[test]
    fn rejection_response_contains_only_stable_diagnostics() {
        let response = HelperResponse {
            diagnostic: None,
            schema_version: 1,
            request_id: Some("10000000-0000-4000-8000-000000000001".parse().unwrap()),
            status: "rejected".parse().expect("declared helper response status"),
            evidence_sha256: None,
            exit_code: None,
            error_code: Some("operation_failed".to_owned()),
            observation_receipt: None,
        };
        let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
        let value: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(value["request_id"], "10000000-0000-4000-8000-000000000001");
        assert_eq!(value["error_code"], "operation_failed");
        assert!(value.get("detail").is_none());
        assert!(value.get("stderr").is_none());
    }

    #[test]
    fn success_response_omits_unused_optional_fields() {
        let response = HelperResponse {
            diagnostic: None,
            schema_version: 1,
            request_id: Some("10000000-0000-4000-8000-000000000001".parse().unwrap()),
            status: "package-installed".parse().unwrap(),
            evidence_sha256: Some("a".repeat(64)),
            exit_code: None,
            error_code: None,
            observation_receipt: None,
        };
        let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
        let value: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert!(value.get("error_code").is_none());
    }

    #[test]
    fn request_id_is_only_attached_after_authorization() {
        let before_authorization =
            HelperRejection::new("grant_unauthorized", "signature was invalid");
        assert!(before_authorization.request_id.is_none());

        let after_authorization =
            HelperRejection::for_request("request-1", "operation_failed", "dpkg failed");
        assert_eq!(after_authorization.request_id.as_deref(), Some("request-1"));
    }

    #[test]
    fn package_failures_are_stage_specific_and_exit_codes_are_bounded() {
        let operation = HostOperation::InstallVonkDebOperation(
            vonk_agent_protocol::generated::InstallVonkDebOperation {
                type_: "install-vonk-deb".into(),
                rollback: vonk_agent_protocol::PackageRollbackAuthority {
                    source: vonk_agent_protocol::PackageRollbackSource {
                        package_sha256: "a".repeat(64),
                        package_signature: "b".repeat(128),
                        package_version: "0.1.0".into(),
                        binary_sha256: "c".repeat(64),
                        helper_sha256: "d".repeat(64),
                    },
                    attempt_nonce: "e".repeat(64),
                    activation_deadline: 2100000000,
                },
                package_sha256: "a".repeat(64),
                package_signature: "b".repeat(128),
            },
        );
        let install = HelperRejection::for_operation(
            "request-1",
            &operation,
            OperationError::PackageInstallFailed {
                exit_code: Some(75),
                diagnostic: "configuration failed".into(),
            },
        );
        assert_eq!(install.error_code, "package_install_failed");
        assert_eq!(install.exit_code, Some(75));
        assert_eq!(install.detail, "package installation failed");
        assert!(!install.detail.contains("configuration"));

        let unbounded = HelperRejection::for_operation(
            "request-1",
            &operation,
            OperationError::PackageInstallFailed {
                exit_code: Some(512),
                diagnostic: "configuration failed".into(),
            },
        );
        assert_eq!(unbounded.error_code, "package_install_failed");
        assert_eq!(unbounded.exit_code, None);

        let metadata = HelperRejection::for_operation(
            "request-1",
            &operation,
            OperationError::PackageMetadataInvalid,
        );
        assert_eq!(metadata.error_code, "package_metadata_failed");
    }

    #[test]
    fn runtime_image_failures_identify_the_failed_stage_without_details() {
        let operation = HostOperation::ExecuteContainerRuntimeRequestOperation(
            vonk_agent_protocol::generated::ExecuteContainerRuntimeRequestOperation {
                type_: "execute-container-runtime-request".into(),
                action: ContainerRuntimeAction::ImageImport,
                job_id: uuid::Uuid::nil(),
                operation_id: uuid::Uuid::nil(),
                attempt: 1,
                fence: uuid::Uuid::nil(),
                request_sha256: "a".repeat(64),
                observation_identity_sha256: None,
                installation_id: None,
            },
        );
        for (error, code) in [
            (
                OperationError::RuntimeImageLoadFailed,
                "runtime_image_load_failed",
            ),
            (
                OperationError::RuntimeImageInspectFailed,
                "runtime_image_inspect_failed",
            ),
            (
                OperationError::RuntimeImageIdentityInvalid,
                "runtime_image_identity_invalid",
            ),
            (
                OperationError::RuntimeImageReceiptFailed,
                "runtime_image_receipt_failed",
            ),
        ] {
            let rejection = HelperRejection::for_operation("request-1", &operation, error);
            assert_eq!(rejection.error_code, code);
            assert!(rejection.detail.contains("runtime image"));
        }
    }
}
