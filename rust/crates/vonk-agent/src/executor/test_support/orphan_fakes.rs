#![cfg(test)]
//! A faithful Controller and privileged helper for the orphan-stop boundary.
//!
//! The Controller answers the dispositions, observation reports and orphan
//! stop grants an agent asks for, by the same rule the real one reads: a stop
//! is granted exactly when the run's disposition is unowned. The helper keeps a
//! table of containers and answers inspections and signed stop grants as the
//! real one does: a stop needs the grant to match the request file the agent
//! wrote, removes a container only when it is the grant's own (same run, target
//! and installation, same or older generation), and treats an absent container
//! as already stopped. Both record every call.

use super::*;

use std::collections::BTreeMap;

/// How long a fake keeps serving if its owner never drops it.
const SERVE_FOR: Duration = Duration::from_secs(120);
use std::path::PathBuf;
use vonk_agent_protocol::generated::{
    HelperErrorCode, HostHelperContainerLabels, HostHelperResponse, HostHelperResponseStatus,
};

/// What the Controller says about one run.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(in crate::executor) enum FakeDisposition {
    Unowned,
    /// Wanted here, running at this generation.
    Wanted(u64),
}

#[derive(Default)]
struct ControllerState {
    dispositions: BTreeMap<String, FakeDisposition>,
    down: bool,
    /// Every grant ask is refused as if a live operation owned the run.
    refuse_grants: bool,
    /// How long a disposition answer is held back.
    disposition_delay: Duration,
    reports: Vec<Value>,
    grants: Vec<String>,
    refused: Vec<String>,
}

pub(in crate::executor) struct FakeController {
    pub(in crate::executor) client: AgentHttpClient,
    state: Arc<Mutex<ControllerState>>,
    stop: Arc<AtomicBool>,
}

impl FakeController {
    pub(in crate::executor) fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        let state = Arc::new(Mutex::new(ControllerState::default()));
        let stop = Arc::new(AtomicBool::new(false));
        let (shared, stopped) = (state.clone(), stop.clone());
        thread::spawn(move || {
            let deadline = Instant::now() + SERVE_FOR;
            while !stopped.load(Ordering::SeqCst) && Instant::now() < deadline {
                match listener.accept() {
                    Ok((stream, _)) => {
                        // A slow answer must not hold up the others.
                        let shared = shared.clone();
                        thread::spawn(move || serve_controller(stream, &shared));
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(1));
                    }
                    Err(error) => panic!("controller listener: {error}"),
                }
            }
        });
        Self {
            client: AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID),
            state,
            stop,
        }
    }

    pub(in crate::executor) fn say(&self, run: Uuid, disposition: FakeDisposition) {
        self.state
            .lock()
            .unwrap()
            .dispositions
            .insert(run.to_string(), disposition);
    }

    /// While down every request is dropped without an answer.
    pub(in crate::executor) fn set_down(&self, down: bool) {
        self.state.lock().unwrap().down = down;
    }

    pub(in crate::executor) fn delay_dispositions(&self, delay: Duration) {
        self.state.lock().unwrap().disposition_delay = delay;
    }

    /// How many observation reports have arrived.
    pub(in crate::executor) fn reports_seen(&self) -> usize {
        self.state.lock().unwrap().reports.len()
    }

    /// The run ids named by the reports from number `from` on.
    pub(in crate::executor) fn reported_since(&self, from: usize) -> Vec<String> {
        self.state.lock().unwrap().reports[from..]
            .iter()
            .flat_map(|report| report["runs"].as_array().cloned().unwrap_or_default())
            .map(|run| run["run_id"].as_str().unwrap().to_owned())
            .collect()
    }

    pub(in crate::executor) fn refuse_grants(&self, refuse: bool) {
        self.state.lock().unwrap().refuse_grants = refuse;
    }

    /// The runs a grant was issued for, in order.
    pub(in crate::executor) fn grants(&self) -> Vec<String> {
        self.state.lock().unwrap().grants.clone()
    }

    /// The runs a grant was refused for (409), in order.
    pub(in crate::executor) fn refusals(&self) -> Vec<String> {
        self.state.lock().unwrap().refused.clone()
    }

    /// Every run id any observation report named.
    pub(in crate::executor) fn reported_runs(&self) -> Vec<String> {
        self.state
            .lock()
            .unwrap()
            .reports
            .iter()
            .flat_map(|report| report["runs"].as_array().cloned().unwrap_or_default())
            .map(|run| run["run_id"].as_str().unwrap().to_owned())
            .collect()
    }
}

impl Drop for FakeController {
    fn drop(&mut self) {
        // The serving thread sees the flag within a millisecond and ends.
        self.stop.store(true, Ordering::SeqCst);
    }
}

fn serve_controller(mut stream: std::net::TcpStream, state: &Mutex<ControllerState>) {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut request = Vec::new();
    let mut buffer = [0_u8; 4096];
    let deadline = Instant::now() + Duration::from_secs(5);
    let header_end = loop {
        if Instant::now() >= deadline {
            return;
        }
        let Ok(size) = stream.read(&mut buffer) else {
            return;
        };
        if size == 0 {
            return;
        }
        request.extend_from_slice(&buffer[..size]);
        if let Some(index) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
            break index + 4;
        }
    };
    let head = String::from_utf8_lossy(&request[..header_end]).into_owned();
    let line = head.lines().next().unwrap().to_owned();
    let length = head
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().unwrap())
        })
        .unwrap_or(0);
    while request.len() - header_end < length && Instant::now() < deadline {
        let Ok(size) = stream.read(&mut buffer) else {
            return;
        };
        if size == 0 {
            return;
        }
        request.extend_from_slice(&buffer[..size]);
    }
    let body = &request[header_end..];
    if line.contains("/disposition") {
        let delay = state.lock().unwrap().disposition_delay;
        thread::sleep(delay);
    }
    let mut state = state.lock().unwrap();
    if state.down {
        return;
    }
    let mut parts = line.split(' ');
    let (method, path) = (parts.next().unwrap(), parts.next().unwrap().to_owned());
    let run_of = |suffix: &str| {
        path.strip_prefix("/agent/recipe-runs/")
            .and_then(|rest| rest.strip_suffix(suffix))
            .map(str::to_owned)
    };
    let respond = |stream: &mut std::net::TcpStream, status: &str, headers: &str, body: &[u8]| {
        let _ = write!(
            stream,
            "HTTP/1.1 {status}\r\n{headers}Content-Length: {}\r\nConnection: close\r\n\r\n",
            body.len()
        );
        let _ = stream.write_all(body);
    };
    match (method, path.as_str()) {
        ("GET", _) if run_of("/disposition").is_some() => {
            let run = run_of("/disposition").unwrap();
            let header = match state.dispositions.get(&run) {
                Some(FakeDisposition::Wanted(generation)) => {
                    format!("x-vonk-recipe-run-generation: {generation}\r\n")
                }
                Some(FakeDisposition::Unowned) => {
                    "x-vonk-recipe-run-disposition: unowned\r\n".to_owned()
                }
                None => String::new(),
            };
            respond(&mut stream, "204 No Content", &header, b"");
        }
        ("POST", "/agent/recipe-runs/observations") => {
            state.reports.push(serde_json::from_slice(body).unwrap());
            respond(&mut stream, "204 No Content", "", b"");
        }
        ("POST", _) if run_of("/orphan-stop-grant").is_some() => {
            let run = run_of("/orphan-stop-grant").unwrap();
            let request: Value = serde_json::from_slice(body).unwrap();
            // The Controller's one rule: a stop for what it does not want here.
            assert_eq!(request["action"], "stop");
            assert_eq!(request["runtime_run_id"], run.as_str());
            if state.refuse_grants
                || !matches!(state.dispositions.get(&run), Some(FakeDisposition::Unowned))
            {
                state.refused.push(run);
                respond(
                    &mut stream,
                    "409 Conflict",
                    "content-type: application/json\r\n",
                    br#"{"detail":"recipe run is owned"}"#,
                );
                return;
            }
            state.grants.push(run.clone());
            let operation = json!({
                "type": "execute-container-runtime-request",
                "action": "stop",
                "fence": request["fence"],
                "request_sha256": request["request_sha256"],
                "stop_plan_sha256": request["stop_plan_sha256"],
                "run_generation": request["run_generation"],
                "runtime_run_id": run,
                "runtime_target_id": request["runtime_target_id"],
                "runtime_installation_id": request["runtime_installation_id"],
            });
            let now = Utc::now().timestamp();
            let grant = json!({ "grant": {
                "schema_version": 1,
                "claims": {
                    "schema_version": 1,
                    "authority": "vonk.host-maintenance-helper",
                    "request_id": Uuid::new_v4().to_string(),
                    "node_id": NODE_ID,
                    "issued_at": now - 30,
                    "expires_at": now + 30,
                    "operation": operation,
                },
                "signature": {
                    "algorithm": "ed25519",
                    "key_id": "0".repeat(64),
                    "value": "0".repeat(128),
                },
            }});
            respond(
                &mut stream,
                "200 OK",
                "content-type: application/json\r\n",
                grant.to_string().as_bytes(),
            );
        }
        // Anything else (a stray client of a recycled port) is not this
        // Controller's to answer; it must not stop the fake serving.
        _ => respond(&mut stream, "404 Not Found", "", b""),
    }
}

/// One thing the helper was asked, in order.
#[derive(Clone, PartialEq, Eq, Debug)]
pub(in crate::executor) enum HelperCall {
    Inspect {
        name: String,
    },
    /// A signed stop for the container at `name`, and whether it removed it.
    Stop {
        name: String,
        removed: bool,
    },
}

struct Container {
    running: bool,
    labels: Option<HostHelperContainerLabels>,
}

#[derive(Default)]
struct HostState {
    containers: BTreeMap<String, Container>,
    calls: Vec<HelperCall>,
    /// A newer generation takes the name just before the next stop lands.
    replace_before_stop: Option<(String, HostHelperContainerLabels)>,
    /// The helper stops the container, then the reply never arrives.
    drop_next_stop_reply: bool,
    /// The next stop takes this long to land.
    slow_next_stop: Duration,
}

pub(in crate::executor) struct FakeHost {
    pub(in crate::executor) socket: PathBuf,
    state: Arc<Mutex<HostState>>,
    stop: Arc<AtomicBool>,
}

pub(in crate::executor) fn labels_of(
    run: Uuid,
    installation: Uuid,
    generation: u64,
) -> HostHelperContainerLabels {
    HostHelperContainerLabels {
        run_id: run,
        target_id: run,
        installation_id: installation,
        run_generation: generation,
        plan_digest: "d".repeat(64),
    }
}

impl FakeHost {
    /// `request_root` is where the agent writes its signed request files.
    pub(in crate::executor) fn new(socket: PathBuf, request_root: PathBuf) -> Self {
        let listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
        listener.set_nonblocking(true).unwrap();
        let state = Arc::new(Mutex::new(HostState::default()));
        let stop = Arc::new(AtomicBool::new(false));
        let (shared, stopped) = (state.clone(), stop.clone());
        thread::spawn(move || {
            let deadline = Instant::now() + SERVE_FOR;
            while !stopped.load(Ordering::SeqCst) && Instant::now() < deadline {
                match listener.accept() {
                    Ok((stream, _)) => serve_helper(stream, &shared, &request_root),
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(1));
                    }
                    Err(error) => panic!("helper listener: {error}"),
                }
            }
        });
        Self {
            socket,
            state,
            stop,
        }
    }

    /// A container named `vonk-<run>`; `labels` is none for one that carries
    /// no readable identity.
    pub(in crate::executor) fn run(&self, run: Uuid, labels: Option<HostHelperContainerLabels>) {
        self.state.lock().unwrap().containers.insert(
            format!("vonk-{run}"),
            Container {
                running: true,
                labels,
            },
        );
    }

    pub(in crate::executor) fn is_running(&self, run: Uuid) -> bool {
        self.state
            .lock()
            .unwrap()
            .containers
            .get(&format!("vonk-{run}"))
            .is_some_and(|container| container.running)
    }

    pub(in crate::executor) fn calls(&self) -> Vec<HelperCall> {
        self.state.lock().unwrap().calls.clone()
    }

    pub(in crate::executor) fn replace_before_next_stop(
        &self,
        run: Uuid,
        labels: HostHelperContainerLabels,
    ) {
        self.state.lock().unwrap().replace_before_stop = Some((format!("vonk-{run}"), labels));
    }

    pub(in crate::executor) fn slow_next_stop(&self, delay: Duration) {
        self.state.lock().unwrap().slow_next_stop = delay;
    }

    pub(in crate::executor) fn lose_next_stop_reply(&self) {
        self.state.lock().unwrap().drop_next_stop_reply = true;
    }
}

impl Drop for FakeHost {
    fn drop(&mut self) {
        // The serving thread sees the flag within a millisecond and ends.
        self.stop.store(true, Ordering::SeqCst);
    }
}

fn reply(stream: &mut std::os::unix::net::UnixStream, response: &HostHelperResponse) {
    let body = vonk_agent_protocol::canonical_generated_json(response).unwrap();
    let _ = stream.write_all(&(body.len() as u32).to_be_bytes());
    let _ = stream.write_all(&body);
}

fn bare_response(request_id: Option<Uuid>, status: HostHelperResponseStatus) -> HostHelperResponse {
    HostHelperResponse {
        schema_version: 1,
        request_id,
        status,
        exit_code: None,
        error_code: None,
        installation_intent_nonce: None,
        process_running: None,
        job_progress_at: None,
        diagnostic: None,
        process_logs: None,
        container_labels: None,
    }
}

fn rejection(request_id: Option<Uuid>, code: HelperErrorCode) -> HostHelperResponse {
    HostHelperResponse {
        error_code: Some(code),
        ..bare_response(request_id, HostHelperResponseStatus::Rejected)
    }
}

fn serve_helper(
    mut stream: std::os::unix::net::UnixStream,
    host: &Mutex<HostState>,
    request_root: &std::path::Path,
) {
    stream.set_nonblocking(false).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut prefix = [0; 4];
    if stream.read_exact(&mut prefix).is_err() {
        return;
    }
    let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
    stream.read_exact(&mut body).unwrap();
    let mut state = host.lock().unwrap();
    let read_request = |digest: &str| -> vonk_agent_protocol::HostRuntimeRequest {
        let bytes = fs::read(request_root.join(format!("{digest}.json"))).unwrap();
        assert_eq!(vonk_agent_protocol::hex_sha256(&bytes), digest);
        vonk_agent_protocol::parse_strict(&bytes).unwrap()
    };
    if let Ok(inspection) =
        vonk_agent_protocol::parse_strict::<vonk_agent_protocol::RecipeRunInspectionRequest>(&body)
    {
        let request = read_request(&inspection.request_sha256);
        // The container of a name-only look, or the one a full identity names.
        let name = match request.arguments.as_slice() {
            [run_id] => format!("vonk-{run_id}"),
            arguments => arguments
                .iter()
                .find(|argument| {
                    argument.starts_with("vonk-") && Uuid::parse_str(&argument[5..]).is_ok()
                })
                .cloned()
                .expect("a full-identity inspection names its container"),
        };
        state.calls.push(HelperCall::Inspect { name: name.clone() });
        let mut response = bare_response(
            Some(inspection.request_id),
            HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
        );
        match state.containers.get(&name) {
            Some(container) => {
                if let Some(generation) = request.run_generation
                    && container
                        .labels
                        .as_ref()
                        .is_none_or(|labels| labels.run_generation != generation)
                {
                    reply(
                        &mut stream,
                        &rejection(
                            Some(inspection.request_id),
                            HelperErrorCode::OperationInvalidArtifact,
                        ),
                    );
                    return;
                }
                response.process_running = Some(container.running);
                response.container_labels = container.labels.clone();
            }
            None => response.process_running = Some(false),
        }
        reply(&mut stream, &response);
        return;
    }
    let grant: Value = serde_json::from_slice(&body).unwrap();
    let claims = &grant["claims"];
    let request_id: Uuid = claims["request_id"].as_str().unwrap().parse().unwrap();
    assert_eq!(claims["node_id"], NODE_ID);
    let operation = &claims["operation"];
    assert_eq!(operation["action"], "stop");
    let request = read_request(operation["request_sha256"].as_str().unwrap());
    let plan = request.stop_plan.as_ref().expect("a stop carries its plan");
    let digest =
        vonk_agent_protocol::hex_sha256(&vonk_agent_protocol::canonical_json(plan).unwrap());
    // The grant must bind the exact request and plan the agent wrote.
    let bound = request.fence.to_string() == operation["fence"].as_str().unwrap()
        && digest == operation["stop_plan_sha256"].as_str().unwrap()
        && operation["run_generation"] == plan.run_generation
        && operation["runtime_run_id"] == plan.run_id.to_string()
        && operation["runtime_target_id"] == plan.target_runtime_id.to_string()
        && operation["runtime_installation_id"] == plan.installation_id.to_string();
    if !bound {
        reply(
            &mut stream,
            &rejection(Some(request_id), HelperErrorCode::OperationInvalid),
        );
        return;
    }
    let name = format!("vonk-{}", plan.target_runtime_id);
    let slow = std::mem::take(&mut state.slow_next_stop);
    drop(state);
    thread::sleep(slow);
    let mut state = host.lock().unwrap();
    if let Some((replaced, labels)) = state.replace_before_stop.take() {
        state.containers.insert(
            replaced,
            Container {
                running: true,
                labels: Some(labels),
            },
        );
    }
    // The helper's own rule: managed, same run, target and installation, and
    // the grant's generation or an older one. Anything else is untouched.
    let owned = state.containers.get(&name).map(|container| {
        container.labels.as_ref().is_some_and(|labels| {
            labels.run_id == plan.run_id
                && labels.target_id == plan.target_runtime_id
                && labels.installation_id == plan.installation_id
                && (1..=plan.run_generation).contains(&labels.run_generation)
        })
    });
    if owned == Some(false) {
        state.calls.push(HelperCall::Stop {
            name,
            removed: false,
        });
        reply(
            &mut stream,
            &rejection(Some(request_id), HelperErrorCode::OperationInvalidArtifact),
        );
        return;
    }
    let removed = state.containers.remove(&name).is_some();
    state.calls.push(HelperCall::Stop { name, removed });
    if std::mem::take(&mut state.drop_next_stop_reply) {
        return;
    }
    reply(
        &mut stream,
        &bare_response(
            Some(request_id),
            HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
        ),
    );
}
