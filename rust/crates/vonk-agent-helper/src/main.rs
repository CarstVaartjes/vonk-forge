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

use rustix::net::sockopt::socket_peercred;
use vonk_agent_helper::operations::{
    ManagedRoots, OperationError, OperationExecutor, ProcessCommandRunner,
};
use vonk_agent_helper::protocol::{
    GrantVerifier, HelperError, HostOperation, PeerIdentity, parse_inspection_request,
    parse_request, read_frame, write_frame,
};
use vonk_agent_protocol::generated::{HostHelperProcessLogs, HostHelperResponse as HelperResponse};

const GRANT_KEY: &str = "/etc/vonk-forge-agent/host-helper-authority.pub";
const RELEASE_KEY: &str = "/usr/share/keyrings/vonk-forge-release.pub";
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
    diagnostic: Option<String>,
    process_logs: Option<Box<HostHelperProcessLogs>>,
}

impl HelperRejection {
    fn new(error_code: &'static str, detail: impl Into<String>) -> Self {
        Self {
            request_id: None,
            error_code,
            exit_code: None,
            detail: detail.into(),
            diagnostic: None,
            process_logs: None,
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
            diagnostic: None,
            process_logs: None,
        }
    }

    fn for_operation(
        request_id: impl Into<String>,
        operation: &HostOperation,
        error: OperationError,
    ) -> Self {
        Self::for_error(
            request_id,
            matches!(operation, HostOperation::InstallVonkDebOperation(_)),
            error,
        )
    }

    fn for_error(
        request_id: impl Into<String>,
        package_install: bool,
        error: OperationError,
    ) -> Self {
        let (diagnostic, process_logs) = match &error {
            // The container's own output is the evidence for an exited
            // workload, so it crosses as its own typed per-stream document
            // rather than as a line of free text.
            OperationError::RuntimeProcessExited {
                logs,
                capture_error,
            } => (capture_error.map(str::to_owned), logs.clone()),
            // The firewall's refusal names the argument and rule, so it travels
            // as the diagnostic instead of collapsing to the stable code.
            OperationError::RuntimeFabricFirewallRejected { reason } => {
                (Some(reason.clone()), None)
            }
            _ => (None, None),
        };
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
            OperationError::RuntimeProcessExited { .. } => ("runtime_process_exited", None),
            OperationError::RuntimeRunMissing => ("runtime_run_missing", None),
            OperationError::RuntimeFabricUnavailable => ("runtime_fabric_unavailable", None),
            OperationError::RuntimeFabricFirewallRejected { .. } => {
                ("runtime_fabric_firewall_rejected", None)
            }
            OperationError::InstallationReconciliationBusy => {
                ("installation_reconciliation_busy", None)
            }
            OperationError::InstallationReconciliationStorageUnavailable => {
                ("installation_reconciliation_storage_unavailable", None)
            }
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
            diagnostic,
            process_logs,
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
                if let Err(error) = thread::Builder::new()
                    .name("vonk-helper-request".to_owned())
                    .spawn(move || {
                        let _permit = permit;
                        if let Err(error) = handle(&mut stream, &verifier, &executor, &node_id) {
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
        diagnostic: error
            .diagnostic
            .as_deref()
            .or_else(|| {
                (error.error_code == "package_install_failed").then_some(error.detail.as_str())
            })
            // A diagnostic is a tail: the newest text is the text that explains
            // the failure, so the bound keeps the end rather than the head.
            .map(|detail| {
                let mut kept: Vec<char> = detail.chars().rev().take(8192).collect();
                kept.reverse();
                kept.into_iter().collect()
            }),
        process_logs: error.process_logs.as_deref().cloned(),
        schema_version: 1,
        request_id,
        status: "rejected".parse().expect("declared helper response status"),
        exit_code,
        error_code: Some(error.error_code.to_owned()),
        process_running: None,
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
    if let Ok(inspection) = parse_inspection_request(&raw) {
        verifier
            .authorize_peer(&peer)
            .map_err(|error| HelperRejection::new("peer_identity_invalid", error.safe_detail()))?;
        let request_id = inspection.request_id.to_string();
        let running = executor
            .inspect_recipe_run(&inspection.request_sha256)
            .map_err(|error| HelperRejection::for_error(&request_id, false, error))?;
        return respond(
            stream,
            &request_id,
            HelperResponse {
                diagnostic: None,
                process_logs: None,
                schema_version: 1,
                request_id: Some(inspection.request_id),
                status: "container-runtime-request-executed"
                    .parse()
                    .expect("declared helper response status"),
                exit_code: None,
                error_code: None,
                process_running: Some(running),
            },
        );
    }
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
    claim_once(&request_id).map_err(|failure| {
        HelperRejection::for_request(&request_id, failure.error_code(), failure.detail())
    })?;
    let outcome = executor
        .execute_for_node(&request.claims.operation, Some(node_id))
        .map_err(|error| {
            HelperRejection::for_operation(&request_id, &request.claims.operation, error)
        })?;
    let response = HelperResponse {
        diagnostic: None,
        process_logs: None,
        schema_version: 1,
        request_id: Some(request.claims.request_id),
        status: outcome.status.parse().map_err(|_| {
            HelperRejection::for_request(
                &request_id,
                "operation_failed",
                "invalid operation response status",
            )
        })?,
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
        process_running: None,
    };
    respond(stream, &request_id, response)
}

fn respond(
    stream: &mut UnixStream,
    request_id: &str,
    response: HelperResponse,
) -> Result<(), HelperRejection> {
    let body = vonk_agent_protocol::canonical_generated_json(&response).map_err(|_error| {
        HelperRejection::for_request(
            request_id,
            "operation_failed",
            "helper response encoding failed",
        )
    })?;
    write_frame(stream, &body).map_err(|error| {
        HelperRejection::for_request(request_id, "operation_failed", error.safe_detail())
    })
}

/// Why the request ledger refused to claim a request identity.
///
/// Only a marker that already exists proves the grant was consumed. A full,
/// read-only or otherwise unwritable ledger is a different failure, and calling
/// it a replay sends the operator after the wrong cause -- so the distinction is
/// carried as a type rather than compared as a string, and it reaches the agent
/// as a distinct error code.
enum ClaimFailure {
    Consumed,
    Ledger,
}

impl ClaimFailure {
    fn from_ledger_io(error: &std::io::Error) -> Self {
        if error.kind() == std::io::ErrorKind::AlreadyExists {
            Self::Consumed
        } else {
            Self::Ledger
        }
    }

    fn error_code(&self) -> &'static str {
        match self {
            Self::Consumed => "request_replayed",
            Self::Ledger => "request_ledger_failed",
        }
    }

    fn detail(&self) -> &'static str {
        match self {
            Self::Consumed => "request grant was already consumed",
            Self::Ledger => "request ledger could not be updated",
        }
    }
}

fn claim_once(request_id: &str) -> Result<(), ClaimFailure> {
    let root = Path::new(REQUEST_LEDGER);
    let metadata = fs::symlink_metadata(root).map_err(|_| ClaimFailure::Ledger)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() || metadata.uid() != 0 {
        return Err(ClaimFailure::Ledger);
    }
    let marker = root.join(request_id);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&marker)
        .map_err(|error| ClaimFailure::from_ledger_io(&error))?;
    file.write_all(b"pending\n")
        .map_err(|_| ClaimFailure::Ledger)?;
    file.sync_all().map_err(|_| ClaimFailure::Ledger)?;
    OpenOptions::new()
        .read(true)
        .open(root)
        .and_then(|directory| directory.sync_all())
        .map_err(|_| ClaimFailure::Ledger)
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

fn read_root_text(path: &Path, maximum_bytes: u64) -> Result<String, String> {
    let metadata = fs::symlink_metadata(path).map_err(display)?;
    verify_root_text(path, &metadata, maximum_bytes)?;
    // Re-open with O_NOFOLLOW and re-verify on the opened descriptor so the
    // checks cannot be bypassed by swapping the path after the metadata check.
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)
        .map_err(display)?;
    verify_root_text(path, &file.metadata().map_err(display)?, maximum_bytes)?;
    let mut value = String::new();
    (&file).read_to_string(&mut value).map_err(display)?;
    Ok(value.trim_end().to_owned())
}

fn verify_root_text(
    path: &Path,
    metadata: &fs::Metadata,
    maximum_bytes: u64,
) -> Result<(), String> {
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
    Ok(())
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
    use super::{HelperRejection, HelperResponse, MAX_CONCURRENT_REQUESTS, acquire_worker};
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
    fn exited_runtime_diagnostics_survive_the_framed_helper_response() {
        let (mut client, mut server) = std::os::unix::net::UnixStream::pair().unwrap();
        let operation = HostOperation::ExecuteContainerRuntimeRequestOperation(
            vonk_agent_protocol::generated::ExecuteContainerRuntimeRequestOperation {
                type_: "execute-container-runtime-request".into(),
                action: ContainerRuntimeAction::RunInspect,
                fence: uuid::Uuid::nil(),
                request_sha256: "a".repeat(64),
                installation_id: None,
                reconciliation_identity: None,
                run_generation: None,
                runtime_installation_id: None,
                runtime_run_id: None,
                runtime_target_id: None,
                start_plan_sha256: None,
                stop_plan_sha256: None,
            },
        );
        let rejection = HelperRejection::for_operation(
            "10000000-0000-4000-8000-000000000001",
            &operation,
            OperationError::RuntimeProcessExited {
                logs: Some(Box::new(vonk_agent_helper::runtime_logs::retain_container(
                    b"worker rank 1 is starting\n",
                    &b"startup failed\n".repeat(2000),
                ))),
                capture_error: None,
            },
        );
        super::reject(&mut server, &rejection);
        let bytes = vonk_agent_helper::protocol::read_frame(&mut client).unwrap();
        let response: HelperResponse = vonk_agent_protocol::parse_strict(&bytes).unwrap();
        let logs = response.process_logs.unwrap();
        assert_eq!(logs.stdout.text, "worker rank 1 is starting\n");
        assert!(logs.stderr.text.contains("startup failed"));
        assert!(logs.stderr.truncated);
        assert!(logs.stderr.text.chars().count() <= 2048);
        assert!(response.diagnostic.is_none());
        assert_eq!(rejection.detail, "runtime process exited");
    }

    #[test]
    fn rejection_response_contains_only_stable_diagnostics() {
        let response = HelperResponse {
            diagnostic: None,
            process_logs: None,
            schema_version: 1,
            request_id: Some("10000000-0000-4000-8000-000000000001".parse().unwrap()),
            status: "rejected".parse().expect("declared helper response status"),
            exit_code: None,
            error_code: Some("operation_failed".to_owned()),
            process_running: None,
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
            process_logs: None,
            schema_version: 1,
            request_id: Some("10000000-0000-4000-8000-000000000001".parse().unwrap()),
            status: "package-installed".parse().unwrap(),
            exit_code: None,
            error_code: None,
            process_running: None,
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
    fn only_an_existing_ledger_marker_names_a_replay() {
        use std::io::{Error, ErrorKind};
        // The agent reports these as distinct codes, so a ledger that is full,
        // read-only or missing must not arrive as a replayed grant.
        assert_eq!(
            super::ClaimFailure::from_ledger_io(&Error::from(ErrorKind::AlreadyExists))
                .error_code(),
            "request_replayed"
        );
        for kind in [
            ErrorKind::PermissionDenied,
            ErrorKind::NotFound,
            ErrorKind::Other,
        ] {
            assert_eq!(
                super::ClaimFailure::from_ledger_io(&Error::from(kind)).error_code(),
                "request_ledger_failed",
                "{kind:?} was reported as a replayed grant"
            );
        }
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
    fn a_firewall_rejection_carries_its_reason_as_the_diagnostic() {
        // Wrong implementation: the reason stopped at the helper, so the
        // rejection the agent received held only the stable code.
        let rejection = HelperRejection::for_error(
            "request-1",
            false,
            OperationError::RuntimeFabricFirewallRejected {
                reason: "endpoint=8000: host endpoint port 8000 is not authorized".into(),
            },
        );
        assert_eq!(rejection.error_code, "runtime_fabric_firewall_rejected");
        assert_eq!(
            rejection.diagnostic.as_deref(),
            Some("endpoint=8000: host endpoint port 8000 is not authorized")
        );
        assert_eq!(
            rejection.detail,
            "native fabric firewall rejected the placement"
        );
    }

    #[test]
    fn runtime_image_failures_identify_the_failed_stage_without_details() {
        let operation = HostOperation::ExecuteContainerRuntimeRequestOperation(
            vonk_agent_protocol::generated::ExecuteContainerRuntimeRequestOperation {
                type_: "execute-container-runtime-request".into(),
                action: ContainerRuntimeAction::ImagePull,
                fence: uuid::Uuid::nil(),
                request_sha256: "a".repeat(64),
                installation_id: None,
                reconciliation_identity: None,
                run_generation: None,
                runtime_installation_id: None,
                runtime_run_id: None,
                runtime_target_id: None,
                start_plan_sha256: None,
                stop_plan_sha256: None,
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
