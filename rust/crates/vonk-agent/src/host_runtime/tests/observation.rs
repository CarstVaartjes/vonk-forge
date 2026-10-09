#![cfg(test)]

use super::*;

/// Real Unix framing and the production spawn_blocking boundary prove
/// ownership survives abandoned logical pages. Only native process-running
/// evidence is a fixture response; permits/socket/request cleanup are real.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cancelled_observation_pages_keep_native_slots_and_leave_foreground_work_ready() {
    use super::super::{
        BACKGROUND_RUN_INSPECTION_CONCURRENCY, HostRuntimeBoundary, RUN_INSPECTION_REQUEST_TIMEOUT,
    };
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::{Arc, Condvar, Mutex};
    struct Gate {
        release: Arc<(Mutex<bool>, Condvar)>,
        stop: Arc<AtomicBool>,
        server: Option<std::thread::JoinHandle<()>>,
        tasks: Arc<Mutex<Vec<tokio::task::AbortHandle>>>,
        deadline: std::time::Instant,
        fixture: Option<tempfile::TempDir>,
        socket: std::path::PathBuf,
    }
    impl Drop for Gate {
        fn drop(&mut self) {
            let mut cleanup_complete = true;
            let tasks = self.tasks.lock().unwrap_or_else(|error| error.into_inner());
            for task in tasks.iter() {
                task.abort();
            }
            *self
                .release
                .0
                .lock()
                .unwrap_or_else(|error| error.into_inner()) = true;
            self.release.1.notify_all();
            self.stop.store(true, Ordering::SeqCst);
            if let Some(server) = self.server.take() {
                while !server.is_finished() && std::time::Instant::now() < self.deadline {
                    std::thread::sleep(Duration::from_millis(1));
                }
                // Never turn a bounded cleanup into an unbounded join.
                // A still-running server keeps the fixture below intact.
                if server.is_finished() {
                    cleanup_complete &= server.join().is_ok();
                } else {
                    cleanup_complete = false;
                }
            }
            while tasks.iter().any(|task| !task.is_finished())
                && std::time::Instant::now() < self.deadline
            {
                std::thread::sleep(Duration::from_millis(1));
            }
            cleanup_complete &= tasks.iter().all(|task| task.is_finished());
            // An aborted async task can leave real spawn_blocking work
            // alive. Returning all permits fences its request cleanup.
            let slots = super::super::background_inspection_slots(&self.socket);
            loop {
                if let Ok(permits) = slots
                    .clone()
                    .try_acquire_many_owned(BACKGROUND_RUN_INSPECTION_CONCURRENCY as u32)
                {
                    drop(permits);
                    break;
                }
                if std::time::Instant::now() >= self.deadline {
                    cleanup_complete = false;
                    break;
                }
                std::thread::sleep(Duration::from_millis(1));
            }
            if !cleanup_complete {
                // Files remain owned by unresolved native calls. Do not
                // turn a deadline or a failed reaper into false absence.
                if let Some(fixture) = self.fixture.take() {
                    let _retained = fixture.keep();
                }
                eprintln!("inspection fixture cleanup unresolved; owned files retained");
                if !std::thread::panicking() {
                    panic!("inspection fixture cleanup exceeded its elapsed budget or failed");
                }
            }
        }
    }
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    let temp = tempfile::tempdir().unwrap();
    let socket = temp.path().join("inspection.sock");
    let requests = temp.path().join("requests");
    let listener = UnixListener::bind(&socket).unwrap();
    listener.set_nonblocking(true).unwrap();
    let active = Arc::new(AtomicUsize::new(0));
    let maximum = Arc::new(AtomicUsize::new(0));
    let started = Arc::new(AtomicUsize::new(0));
    let release = Arc::new((Mutex::new(false), Condvar::new()));
    let stop = Arc::new(AtomicBool::new(false));
    let tasks = Arc::new(Mutex::new(Vec::new()));
    let mut gate = Gate {
        socket: socket.clone(),
        release: release.clone(),
        stop: stop.clone(),
        server: None,
        tasks: tasks.clone(),
        deadline,
        fixture: Some(temp),
    };
    let native_active = active.clone();
    let native_maximum = maximum.clone();
    let native_started = started.clone();
    let native_requests = requests.clone();
    let server = std::thread::spawn(move || {
        let mut workers = Vec::new();
        while !stop.load(Ordering::SeqCst) {
            assert!(
                std::time::Instant::now() < deadline,
                "inspection fixture server exceeded its elapsed budget"
            );
            let (mut stream, _) = match listener.accept() {
                Ok(connection) => connection,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(1));
                    continue;
                }
                Err(error) => panic!("inspection listener: {error}"),
            };
            let active = native_active.clone();
            let maximum = native_maximum.clone();
            let started = native_started.clone();
            let release = release.clone();
            let requests = native_requests.clone();
            workers.push(std::thread::spawn(move || {
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                stream
                    .set_write_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut prefix = [0; 4];
                stream.read_exact(&mut prefix).unwrap();
                let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
                stream.read_exact(&mut body).unwrap();
                let request: vonk_agent_protocol::RecipeRunInspectionRequest =
                    vonk_agent_protocol::parse_strict(&body).unwrap();
                let background = request.include_logs != Some(true);
                if background {
                    let current = active.fetch_add(1, Ordering::SeqCst) + 1;
                    maximum.fetch_max(current, Ordering::SeqCst);
                    started.fetch_add(1, Ordering::SeqCst);
                    let mut released = release.0.lock().unwrap();
                    while !*released {
                        let remaining = deadline
                            .checked_duration_since(std::time::Instant::now())
                            .expect("inspection fixture release exceeded its elapsed budget");
                        let (next, timeout) = release.1.wait_timeout(released, remaining).unwrap();
                        released = next;
                        assert!(
                            !timeout.timed_out() || *released,
                            "inspection fixture release timed out"
                        );
                    }
                    // Dropping an awaiting logical page must neither free
                    // the native permit nor delete its still-owned request.
                    assert!(
                        requests
                            .join(format!("{}.json", request.request_sha256))
                            .exists()
                    );
                }
                let response = super::super::HelperResponse {
                    installation_intent_nonce: None,
                    schema_version: 1,
                    request_id: Some(request.request_id),
                    status: super::super::HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
                    process_running: Some(true),
                    exit_code: None,
                    error_code: None,
                    diagnostic: None,
                    process_logs: None,
                };
                let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
                if background {
                    // Native inspection is finished before its reply makes
                    // the client's permit available to the next call.
                    active.fetch_sub(1, Ordering::SeqCst);
                }
                stream
                    .write_all(&(body.len() as u32).to_be_bytes())
                    .unwrap();
                stream.write_all(&body).unwrap();
            }));
        }
        let mut worker_failed = false;
        for worker in workers {
            while !worker.is_finished() {
                assert!(
                    std::time::Instant::now() < deadline,
                    "inspection fixture worker exceeded its elapsed budget"
                );
                std::thread::sleep(Duration::from_millis(1));
            }
            worker_failed |= std::thread::JoinHandle::join(worker).is_err();
        }
        assert!(!worker_failed, "inspection fixture worker failed");
    });
    gate.server = Some(server);
    let spawn = |background: bool| {
        let socket = socket.clone();
        let requests = requests.clone();
        let task = tokio::spawn(async move {
            let client = crate::client::AgentHttpClient::for_http_test(
                "http://127.0.0.1:9/",
                "spk_0123456789abcdef0123456789abcdef",
            );
            let boundary = HostRuntimeBoundary {
                client: &client,
                request_root: &requests,
                helper_socket: &socket,
            };
            let arguments = vec![Uuid::new_v4().to_string()];
            if background {
                boundary
                    .inspect_recipe_run_evidence_for_observation(arguments)
                    .await
                    .map(|report| report.running)
            } else {
                boundary
                    .inspect_recipe_run_report(arguments, true)
                    .await
                    .map(|report| report.running)
            }
        });
        tasks.lock().unwrap().push(task.abort_handle());
        task
    };
    let mut first: Vec<_> = (0..BACKGROUND_RUN_INSPECTION_CONCURRENCY)
        .map(|_| spawn(true))
        .collect();
    tokio::time::timeout(RUN_INSPECTION_REQUEST_TIMEOUT, async {
        while active.load(Ordering::SeqCst) != BACKGROUND_RUN_INSPECTION_CONCURRENCY {
            for task in &mut first {
                if task.is_finished() {
                    match task.await {
                        Ok(Err(error)) => panic!(
                            "inspection fixture startup refused request: {}",
                            error.preflight_code()
                        ),
                        Ok(Ok(_)) => {
                            panic!("inspection fixture unexpectedly completed before release")
                        }
                        Err(_) => panic!("inspection fixture startup task failed to join"),
                    }
                }
            }
            assert!(
                std::time::Instant::now() < deadline,
                "inspection fixture startup exceeded its elapsed budget"
            );
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    for task in first {
        task.abort();
        let _ = task.await;
    }
    for _ in 0..3 {
        let page: Vec<_> = (0..BACKGROUND_RUN_INSPECTION_CONCURRENCY)
            .map(|_| spawn(true))
            .collect();
        tokio::time::sleep(Duration::from_millis(20)).await;
        for task in page {
            task.abort();
            let _ = task.await;
        }
        assert_eq!(
            active.load(Ordering::SeqCst),
            BACKGROUND_RUN_INSPECTION_CONCURRENCY
        );
        assert_eq!(
            started.load(Ordering::SeqCst),
            BACKGROUND_RUN_INSPECTION_CONCURRENCY
        );
    }
    // A real foreground boundary call has its own lifecycle lane. It is
    // not stuck behind permits still owned by abandoned observer futures.
    assert!(
        tokio::time::timeout(Duration::from_secs(2), spawn(false))
            .await
            .unwrap()
            .unwrap()
            .unwrap()
    );
    assert_eq!(
        maximum.load(Ordering::SeqCst),
        BACKGROUND_RUN_INSPECTION_CONCURRENCY
    );
    *gate.release.0.lock().unwrap() = true;
    gate.release.1.notify_all();
    // Releasing the peer is not proof that the blocking owner has returned
    // its permit. Re-observe through the same producer until a fresh native
    // inspection succeeds; leaked ownership would exhaust this budget.
    let recovery_deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    tokio::time::timeout_at(recovery_deadline, async {
        loop {
            assert!(tokio::time::Instant::now() < recovery_deadline);
            if matches!(spawn(true).await.unwrap(), Ok(true)) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while active.load(Ordering::SeqCst) != 0 {
            assert!(
                std::time::Instant::now() < deadline,
                "inspection fixture draining exceeded its elapsed budget"
            );
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    // Returning every native permit also proves request cleanup happened
    // in the blocking closures, rather than just in the fixture server.
    let permits = tokio::time::timeout(
        Duration::from_secs(2),
        super::super::background_inspection_slots(&socket)
            .acquire_many_owned(BACKGROUND_RUN_INSPECTION_CONCURRENCY as u32),
    )
    .await
    .unwrap()
    .unwrap();
    *gate.release.0.lock().unwrap() = true;
    gate.release.1.notify_all();
    gate.stop.store(true, Ordering::SeqCst);
    while !gate.server.as_ref().unwrap().is_finished() {
        assert!(
            std::time::Instant::now() < deadline,
            "inspection fixture server shutdown exceeded its elapsed budget"
        );
        tokio::time::sleep(Duration::from_millis(1)).await;
    }
    std::thread::JoinHandle::join(gate.server.take().unwrap()).unwrap();
    assert!(fs::read_dir(requests).unwrap().next().is_none());
    drop(permits);
    drop(gate);
}
