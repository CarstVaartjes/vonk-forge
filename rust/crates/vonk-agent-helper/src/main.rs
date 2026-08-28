#![forbid(unsafe_code)]

use std::fs::{self, OpenOptions};
use std::io::Write;
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
use serde::Serialize;
use vonk_agent_helper::operations::{ManagedRoots, OperationExecutor, ProcessCommandRunner};
use vonk_agent_helper::protocol::{
    GrantVerifier, HelperError, PeerIdentity, parse_request, read_frame, write_frame,
};

const GRANT_KEY: &str = "/etc/vonk-forge-agent/host-helper-authority.pub";
const RELEASE_KEY: &str = "/usr/share/keyrings/vonk-forge-release.pub";
const AGENT_CONFIG: &str = "/etc/vonk-forge-agent/agent.toml";
const REQUEST_LEDGER: &str = "/var/lib/vonk-forge/helper/requests";
const DATA_ROOT: &str = "/var/lib/vonk-forge";
const AGENT_DATA_ROOT: &str = "/var/lib/vonk-forge-agent";
const RUNTIME_REQUEST_ROOT: &str = "/run/vonk-forge-agent/runtime-requests";
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

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct HelperResponse<'a> {
    schema_version: u8,
    request_id: Option<String>,
    status: &'a str,
    evidence_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    exit_code: Option<i32>,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("vonk-agent-helper: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let grant_key = load_root_public_key(Path::new(GRANT_KEY))?;
    let release_key = load_root_public_key(Path::new(RELEASE_KEY))?;
    let group_gid = group_gid(Path::new("/etc/group"), AGENT_GROUP)?;
    let agent_uid = user_uid(Path::new("/etc/passwd"), AGENT_GROUP)?;
    let node_id = node_id_from_config(&read_root_text(Path::new(AGENT_CONFIG), 64 * 1024)?)?;
    let verifier = Arc::new(GrantVerifier::new(&grant_key, group_gid).map_err(display)?);
    let executor = Arc::new(
        OperationExecutor::new(
            ManagedRoots::under(Path::new(DATA_ROOT))
                .with_agent_data(Path::new(AGENT_DATA_ROOT))
                .with_runtime_requests(Path::new(RUNTIME_REQUEST_ROOT)),
            &release_key,
            ProcessCommandRunner,
            Some(0),
        )
        .map_err(display)?
        .with_package_owner(agent_uid)
        .with_runtime_request_owner(agent_uid),
    );

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
                    reject(&mut stream, "concurrent request limit reached");
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

fn reject(stream: &mut UnixStream, error: &str) {
    let response = HelperResponse {
        schema_version: 1,
        request_id: None,
        status: "rejected",
        evidence_sha256: None,
        exit_code: None,
    };
    if let Ok(body) = vonk_agent_protocol::canonical_json(&response) {
        let _ = write_frame(stream, &body);
    }
    eprintln!("vonk-agent-helper: request rejected: {error}");
}

fn handle(
    stream: &mut UnixStream,
    verifier: &GrantVerifier,
    executor: &OperationExecutor<ProcessCommandRunner>,
    node_id: &str,
) -> Result<(), String> {
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .map_err(display)?;
    stream
        .set_write_timeout(Some(Duration::from_secs(10)))
        .map_err(display)?;
    let peer = peer_identity(stream)?;
    let raw = read_frame(stream).map_err(display)?;
    let request = parse_request(&raw).map_err(display)?;
    if request.claims.node_id != node_id {
        return Err("grant is for a different node".to_owned());
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(display)?
        .as_secs() as i64;
    verifier.authorize(&request, &peer, now).map_err(display)?;
    claim_once(request.claims.request_id.to_string().as_str())?;
    let outcome = executor
        .execute(&request.claims.operation)
        .map_err(display)?;
    let response = HelperResponse {
        schema_version: 1,
        request_id: Some(request.claims.request_id.to_string()),
        status: &outcome.status,
        evidence_sha256: Some(outcome.evidence_sha256),
        exit_code: outcome.exit_code,
    };
    let body = vonk_agent_protocol::canonical_json(&response).map_err(display)?;
    write_frame(stream, &body).map_err(display)
}

fn claim_once(request_id: &str) -> Result<(), String> {
    let root = Path::new(REQUEST_LEDGER);
    let metadata = fs::symlink_metadata(root).map_err(display)?;
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
    file.write_all(b"pending\n").map_err(display)?;
    file.sync_all().map_err(display)?;
    OpenOptions::new()
        .read(true)
        .open(root)
        .and_then(|directory| directory.sync_all())
        .map_err(display)
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

fn display(error: impl std::fmt::Display) -> String {
    error.to_string()
}

#[allow(dead_code)]
fn _classify_protocol_error(error: HelperError) -> String {
    error.to_string()
}

#[cfg(test)]
mod tests {
    use super::{MAX_CONCURRENT_REQUESTS, acquire_worker};
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
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
}
