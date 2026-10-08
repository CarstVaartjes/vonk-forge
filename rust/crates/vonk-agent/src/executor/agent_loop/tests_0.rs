#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn successful_claim_publishes_readiness_before_job_execution() {
    let directory = tempdir().unwrap();
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: false,
        heartbeats: Arc::new(Mutex::new(Vec::new())),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let events = Arc::new(Mutex::new(Vec::new()));
    let executor = OrderingExecutor {
        events: events.clone(),
    };
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let hook_events = events.clone();

    run_once_with_claim_hook(&client, &mut state, &executor, None, 0, None, move || {
        hook_events.lock().unwrap().push("readiness");
        Ok(())
    })
    .await
    .unwrap();

    assert_eq!(*events.lock().unwrap(), ["readiness", "execute"]);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn long_execution_renews_and_persists_its_lease_before_result() {
    let directory = tempdir().unwrap();
    let original = claim();
    let heartbeats = Arc::new(Mutex::new(Vec::new()));
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(original.clone()))),
        fail_heartbeat: false,
        heartbeats: heartbeats.clone(),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let executor = HeartbeatGatedExecutor {
        heartbeats,
        minimum: 2,
        observed_deadline: Arc::new(Mutex::new(None)),
    };
    let observed_deadline = executor.observed_deadline.clone();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &executor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(10),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();

    let heartbeats = client.heartbeats.lock().unwrap();
    assert!(heartbeats.len() >= 2);
    assert!(
        heartbeats
            .iter()
            .all(|heartbeat| heartbeat.progress.is_none())
    );
    drop(heartbeats);
    let results = client.results.lock().unwrap();
    assert_eq!(results.len(), 1);
    assert!(observed_deadline.lock().unwrap().unwrap() > original.deadline);
    assert!(state.pending_results().unwrap().is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_long_execution_keeps_feeding_the_service_watchdog_through_its_renewals() {
    // The control loop only reaches its own watchdog feed between steps, so
    // an operation longer than the unit's watchdog period (a large model
    // copy) was killed and restarted by systemd while it was healthy.
    let directory = tempdir().unwrap();
    let heartbeats = Arc::new(Mutex::new(Vec::new()));
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: false,
        heartbeats: heartbeats.clone(),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let executor = HeartbeatGatedExecutor {
        heartbeats,
        minimum: 3,
        observed_deadline: Arc::new(Mutex::new(None)),
    };
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let before = WATCHDOG_FEEDS.load(std::sync::atomic::Ordering::SeqCst);

    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &executor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(10),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: count_watchdog_feed,
        },
        || Ok(()),
    )
    .await
    .unwrap();

    assert!(WATCHDOG_FEEDS.load(std::sync::atomic::Ordering::SeqCst) - before >= 3);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_conflicting_result_response_is_not_an_acknowledgement() {
    // The real client has to separate "the Controller already holds this
    // outcome" (204) from "the Controller refused it because the attempt is
    // no longer current" (409).  Treating both as accepted is what let a
    // refused result be deleted locally as though it had landed.
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = Vec::new();
        let mut buffer = [0_u8; 4096];
        while !request.windows(4).any(|value| value == b"\r\n\r\n") {
            let read = stream.read(&mut buffer).unwrap();
            assert_ne!(read, 0);
            request.extend_from_slice(&buffer[..read]);
        }
        stream
            .write_all(b"HTTP/1.1 409 Conflict\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            .unwrap();
        request
    });
    let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID);

    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let claim = claim();
    assert!(matches!(
        state.begin(&claim, Utc::now()).unwrap(),
        BeginDecision::Execute
    ));
    let result = state.finish(&claim, recipe_install_success(0)).unwrap();

    assert!(matches!(
        client.submit_result(&result).await,
        Err(ClientError::ResultSuperseded)
    ));
    let request = String::from_utf8_lossy(&server.join().unwrap()).to_ascii_lowercase();
    assert!(request.starts_with("post /agent/result"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_validation_rejected_result_response_is_typed_for_local_custody() {
    // The real client has to separate a 422 ingress refusal from the
    // generic "protocol response is invalid" and from a transport failure,
    // so the loop can record the bounded reason and continue.  Treating it
    // as a bare non-retryable controller error is what exited the agent.
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = Vec::new();
        let mut buffer = [0_u8; 4096];
        while !request.windows(4).any(|value| value == b"\r\n\r\n") {
            let read = stream.read(&mut buffer).unwrap();
            assert_ne!(read, 0);
            request.extend_from_slice(&buffer[..read]);
        }
        stream
            .write_all(
                b"HTTP/1.1 422 Unprocessable Entity\r\n\
                  content-type: application/json\r\n\
                  x-vonk-error-code: controller.invalid_request\r\n\
                  x-request-id: req-422\r\n\
                  content-length: 0\r\n\
                  connection: close\r\n\r\n",
            )
            .unwrap();
        request
    });
    let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID);

    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let claim = claim();
    assert!(matches!(
        state.begin(&claim, Utc::now()).unwrap(),
        BeginDecision::Execute
    ));
    let result = state.finish(&claim, recipe_install_success(0)).unwrap();

    let Err(ClientError::ResultRejected(error)) = client.submit_result(&result).await else {
        panic!("a 422 must be a typed result rejection");
    };
    assert_eq!(error.status, 422);
    assert_eq!(
        error.code,
        vonk_agent_protocol::generated::ControllerErrorCode::ControllerInvalidRequest.as_str()
    );
    assert_eq!(error.request_id.as_deref(), Some("req-422"));
    assert_eq!(error.endpoint, "/agent/result");
    let request = String::from_utf8_lossy(&server.join().unwrap()).to_ascii_lowercase();
    assert!(request.starts_with("post /agent/result"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_refused_result_is_kept_instead_of_discarded() {
    // A result the Controller refuses because the attempt is no longer
    // current never reached durable storage.  Treating that refusal as an
    // acknowledgement discarded the only evidence of the work this agent
    // performed, so the outcome stays in local custody instead.
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let mut state = StateStore::open(&path, NODE_ID).unwrap();
    let claim = claim();
    assert!(matches!(
        state.begin(&claim, Utc::now()).unwrap(),
        BeginDecision::Execute
    ));
    let result = state.finish(&claim, recipe_install_success(0)).unwrap();
    assert_eq!(state.pending_results().unwrap().len(), 1);

    let client = RefusingResultClient {
        submitted: Arc::new(Mutex::new(Vec::new())),
    };
    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &RejectingExecutor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(10),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();

    assert_eq!(client.submitted.lock().unwrap().len(), 1);
    // The execution is not retried. Its recorded outcome remains readable
    // and is offered once more as diagnostic evidence after restart.
    assert!(state.pending_results().unwrap().is_empty());
    assert_eq!(state.unreconciled_results().unwrap().len(), 1);
    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &RejectingExecutor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(10),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();
    assert_eq!(client.submitted.lock().unwrap().len(), 2);
    assert!(state.unreconciled_results().unwrap().is_empty());
    let connection = rusqlite::Connection::open(&path).unwrap();
    let stored: Option<Vec<u8>> = connection
        .query_row(
            "SELECT result_json FROM operations WHERE fence=?1",
            rusqlite::params![result.fence.to_string()],
            |row| row.get(0),
        )
        .unwrap();
    assert!(stored.is_some());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_ingress_rejected_result_is_recorded_and_the_loop_stays_alive() {
    // A 422 refuses these exact bytes at the Controller's validation
    // boundary.  The general client policy treats a non-retryable 4xx as
    // "exit", so before this the refusal propagated out of the loop and the
    // restarted agent replayed the same durable result.  The receipt now
    // stays in custody, the bounded refusal is durable, and the loop keeps
    // serving claim/heartbeat work.
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let mut state = StateStore::open(&path, NODE_ID).unwrap();
    let result = completed_install_result(&mut state);
    let client = IngressRejectingClient {
        accept: Arc::new(Mutex::new(false)),
        submitted: Arc::new(Mutex::new(Vec::new())),
    };

    for _ in 0..2 {
        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &RejectingExecutor,
            RunOncePolicy {
                preflight_fingerprint: None,
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(10),
                heartbeat_retry_interval: Duration::from_millis(1),
                lease_renewed: crate::systemd_notify::watchdog,
            },
            || Ok(()),
        )
        .await
        .unwrap();
    }

    // The same bytes were offered once and then not hot-looped, the receipt
    // is still unacknowledged, and the refusal names its boundary.
    assert_eq!(client.submitted.lock().unwrap().len(), 1);
    assert_eq!(state.pending_results().unwrap().len(), 1);
    let rejection = state
        .result_rejection(&result, Utc::now())
        .unwrap()
        .expect("a recorded ingress refusal");
    assert_eq!(rejection.http_status, 422);
    assert_eq!(
        rejection.code,
        vonk_agent_protocol::generated::ControllerErrorCode::ControllerInvalidRequest.as_str()
    );
    assert_eq!(rejection.request_id.as_deref(), Some("req-422"));
    // The refusal names the failing field and rule from the Controller's
    // own validation digest, rather than only the endpoint it was refused
    // at, so the durable record is actionable without Controller access.
    assert!(rejection.reason.contains("failure_kind"));
    assert!(rejection.reason.contains("is_instance_of"));
    assert!(rejection.reason.len() <= 256);
    assert!(rejection.retry_due_at > Utc::now());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_corrected_ingress_reconciles_the_retained_result() {
    // Reconciliation after the cause is corrected: once the recorded
    // cool-down has elapsed the retained receipt is offered again, and the
    // Controller's acceptance acknowledges it.
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let mut state = StateStore::open(&path, NODE_ID).unwrap();
    let result = completed_install_result(&mut state);
    state
        .reject_result(
            &result,
            &ingress_refusal(),
            Utc::now() - ChronoDuration::seconds(1200),
        )
        .unwrap();
    assert!(
        state
            .result_rejection(&result, Utc::now())
            .unwrap()
            .is_none()
    );
    let client = IngressRejectingClient {
        accept: Arc::new(Mutex::new(true)),
        submitted: Arc::new(Mutex::new(Vec::new())),
    };

    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &RejectingExecutor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(10),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();

    assert_eq!(client.submitted.lock().unwrap().len(), 1);
    assert!(state.pending_results().unwrap().is_empty());
    assert!(
        state
            .result_rejection(&result, Utc::now())
            .unwrap()
            .is_none()
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn transient_heartbeat_failure_does_not_terminate_healthy_execution() {
    let directory = tempdir().unwrap();
    let heartbeats = Arc::new(Mutex::new(Vec::new()));
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: true,
        heartbeats: heartbeats.clone(),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let executor = HeartbeatGatedExecutor {
        heartbeats,
        minimum: 2,
        observed_deadline: Arc::new(Mutex::new(None)),
    };
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &executor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(10),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();

    assert!(client.heartbeats.lock().unwrap().len() >= 2);
    assert_eq!(client.results.lock().unwrap().len(), 1);
    assert!(state.pending_results().unwrap().is_empty());
}

#[tokio::test]
async fn cancelled_heartbeat_preserves_the_executors_confirmed_stop_result() {
    let directory = tempdir().unwrap();
    let heartbeats = Arc::new(Mutex::new(Vec::new()));
    let client = RecordingClient {
        cancel_requested: true,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: false,
        heartbeats: heartbeats.clone(),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let executor = CancelledHeartbeatExecutor(HeartbeatGatedExecutor {
        heartbeats,
        minimum: 1,
        observed_deadline: Arc::new(Mutex::new(None)),
    });
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &executor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(1),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();

    let results = client.results.lock().unwrap();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].state.as_str(), "cancelled");
    let vonk_agent_protocol::generated::AgentResultResult::OutcomeFailed(body) = &results[0].result
    else {
        panic!("cancelled start outcome lost its typed failure result");
    };
    assert_eq!(body.reason, "exact workload stop confirmed");
    assert_eq!(body.code, FailureCode::OperationCancelled);
}

#[tokio::test]
async fn exact_superseded_cancellation_heartbeat_is_an_expected_loop_outcome() {
    let directory = tempdir().unwrap();
    let heartbeats = Arc::new(Mutex::new(Vec::new()));
    let client = SupersededCancellationClient(RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: false,
        heartbeats: heartbeats.clone(),
        results: Arc::new(Mutex::new(Vec::new())),
    });
    let executor = CancelledHeartbeatExecutor(HeartbeatGatedExecutor {
        heartbeats,
        minimum: 1,
        observed_deadline: Arc::new(Mutex::new(None)),
    });
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &executor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(1),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();

    let results = client.0.results.lock().unwrap();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].state.as_str(), "cancelled");
}

#[tokio::test(start_paused = true)]
async fn transient_heartbeat_failure_retries_inside_the_accepted_lease() {
    let directory = tempdir().unwrap();
    let heartbeats = Arc::new(Mutex::new(Vec::new()));
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: true,
        heartbeats: heartbeats.clone(),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let executor = HeartbeatGatedExecutor {
        heartbeats: heartbeats.clone(),
        minimum: 2,
        observed_deadline: Arc::new(Mutex::new(None)),
    };
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let run = run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &executor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(800),
            heartbeat_retry_interval: HEARTBEAT_RETRY_FLOOR,
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    );
    let drive_clock = async {
        // Claim persistence and heartbeat task startup take an arbitrary
        // number of polls. Advance virtual time in small steps until the
        // first request, then measure the retry against that request.
        for _ in 0..100 {
            if !heartbeats.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::advance(Duration::from_millis(10)).await;
            tokio::task::yield_now().await;
        }
        assert_eq!(heartbeats.lock().unwrap().len(), 1);
        tokio::task::yield_now().await;
        tokio::time::advance(HEARTBEAT_RETRY_FLOOR).await;
        tokio::task::yield_now().await;
        assert_eq!(heartbeats.lock().unwrap().len(), 2);
    };
    let (result, ()) = tokio::join!(run, drive_clock);
    result.unwrap();
    assert_eq!(client.results.lock().unwrap().len(), 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn terminal_heartbeat_failure_cancels_a_blocking_executor() {
    for panic in [false, true] {
        let directory = tempdir().unwrap();
        let client = TerminalHeartbeatClient {
            inner: RecordingClient {
                cancel_requested: false,
                claim: Arc::new(Mutex::new(Some(claim()))),
                fail_heartbeat: false,
                heartbeats: Arc::new(Mutex::new(Vec::new())),
                results: Arc::new(Mutex::new(Vec::new())),
            },
            panic,
        };
        let executor = BlockingCancellationExecutor {
            cancelled: Arc::new(AtomicBool::new(false)),
        };
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
        let result = run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &executor,
            RunOncePolicy {
                preflight_fingerprint: None,
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(10),
                heartbeat_retry_interval: Duration::from_millis(1),
                lease_renewed: crate::systemd_notify::watchdog,
            },
            || Ok(()),
        )
        .await;
        assert!(result.is_err());
        assert!(
            executor.cancelled.load(Ordering::SeqCst),
            "executor continued after heartbeat failure (panic={panic})"
        );
        assert!(client.inner.results.lock().unwrap().is_empty());
        // After identity recovery a fresh fence remains admissible; the old
        // heartbeat task and its bookkeeping retain no execution gate.
        let mut fresh = claim();
        fresh.fence = Uuid::new_v4();
        client.inner.claim.lock().unwrap().replace(fresh);
        run_once(&client.inner, &mut state, &RejectingExecutor, None, 0, None)
            .await
            .unwrap();
        assert!(state.pending_results().unwrap().is_empty());
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_retryable_renewal_failure_after_the_lease_lapses_still_renews() {
    // Wrong implementation: the retry arm was guarded by
    // ``Utc::now() < deadline``, so the first renewal that could not be
    // re-sent inside the accepted lease fell through to the catch-all, the
    // heartbeat task returned, and the work was cancelled.  One lost round
    // trip near the expiry therefore ended renewal for a start that was
    // still healthy -- and ended the agent's ability to observe the
    // Controller's cancellation with it.
    let directory = tempdir().unwrap();
    // Open the store before the lease is timed.  `state.begin` refuses an
    // already-expired claim, so anything slow on the path to the loop is
    // inside the lease's margin; a SQLite open plus schema creation is
    // exactly that, and on a loaded two-core runner it was enough to make
    // the claim expire before the loop started.
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let heartbeats = Arc::new(Mutex::new(Vec::new()));
    let accepted_at = Arc::new(Mutex::new(Vec::new()));
    let mut lease = claim();
    // The lease lapses in real time, because that is the condition under
    // test.  The margin now covers only loop startup, and the refusal
    // window comfortably outlives the lease.
    let lease_deadline = Utc::now() + ChronoDuration::milliseconds(500);
    lease.deadline = lease_deadline.with_timezone(&FixedOffset::east_opt(0).unwrap());
    let client = LeaseLapseClient {
        inner: RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(lease))),
            fail_heartbeat: false,
            heartbeats: heartbeats.clone(),
            results: Arc::new(Mutex::new(Vec::new())),
        },
        // Comfortably past the accepted lease, so every renewal before this
        // instant is refused and the lease has certainly lapsed.
        lapsed_after: Utc::now() + ChronoDuration::milliseconds(2000),
        accepted_at: accepted_at.clone(),
    };
    let cancelled = Arc::new(AtomicBool::new(false));
    let executor = RenewalGatedExecutor {
        accepted: accepted_at.clone(),
        minimum: 1,
        cap: Duration::from_secs(5),
        cancelled: cancelled.clone(),
    };

    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &executor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(5),
            heartbeat_retry_interval: Duration::from_millis(5),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .expect("a lapsed lease that the Controller still accepts must be re-acquired");

    assert!(
        !cancelled.load(Ordering::SeqCst),
        "the work was cancelled although the Controller still accepted renewals"
    );
    let accepted_at = accepted_at.lock().unwrap();
    assert!(
        accepted_at.iter().any(|instant| *instant > lease_deadline),
        "no renewal was accepted after the lease lapsed: {accepted_at:?}"
    );
    assert!(heartbeats.lock().unwrap().len() > accepted_at.len());
    assert_eq!(client.inner.results.lock().unwrap().len(), 1);
    assert!(state.pending_results().unwrap().is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_retryable_renewal_failure_stops_once_the_start_budget_is_spent() {
    // The lease is what a renewal recovers, so it cannot also be the
    // recovery budget.  Wrong implementation: the loop retried while the
    // accepted lease was live, ignoring the start's own immutable budget, so
    // it kept renewing an attempt whose start deadline had already elapsed.
    let directory = tempdir().unwrap();
    let mut start = claim();
    start.operation = AgentOperation::RecipeStart;
    let mut distributed_plan: Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
    ))
    .unwrap();
    distributed_plan["runtime"]["placement"]["world_size"] = json!(2);
    distributed_plan["runtime"]["placement"]["local_address"] = json!("192.168.100.3");
    distributed_plan["runtime"]["placement"]["master_address"] = json!("192.168.100.2");
    distributed_plan["runtime"]["placement"]["master_port"] = json!(29500);
    let payload = json!({
        "run_id": "00000000-0000-4000-8000-0000000000aa",
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "recipe_revision_id": "00000000-0000-4000-8000-0000000000bb",
        "mapping_id": "00000000-0000-4000-8000-0000000000cc",
        "run_generation": 1,
        "plan_digest": "a".repeat(64),
        "compiled_execution_plan": distributed_plan,
        "phase": "rank-launch",
        "start_deadline": (Utc::now() - ChronoDuration::seconds(1)).to_rfc3339(),
    });
    let typed: vonk_agent_protocol::generated::AgentClaimPayload =
        serde_json::from_value(payload).unwrap();
    start.payload = typed;
    let client = LeaseLapseClient {
        inner: RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(start))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        },
        // Never accepts, so only the start budget can end the loop.
        lapsed_after: Utc::now() + ChronoDuration::hours(1),
        accepted_at: Arc::new(Mutex::new(Vec::new())),
    };
    let executor = BlockingCancellationExecutor {
        cancelled: Arc::new(AtomicBool::new(false)),
    };
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

    let result = tokio::time::timeout(
        Duration::from_millis(500),
        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &executor,
            RunOncePolicy {
                preflight_fingerprint: None,
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(5),
                heartbeat_retry_interval: Duration::from_millis(5),
                lease_renewed: crate::systemd_notify::watchdog,
            },
            || Ok(()),
        ),
    )
    .await;
    assert!(
        result.is_ok(),
        "renewal retried past the start's own immutable budget"
    );
    assert!(result.unwrap().is_err());
    assert!(executor.cancelled.load(Ordering::SeqCst));
}

#[test]
fn heartbeat_failure_classification_is_a_closed_set() {
    // Wrong implementation: ``Err(error) => return Err(...)`` treated every
    // error class it did not recognise as terminal, so a new class added to
    // the client would silently end renewal.  The replacement names each
    // class and is exhaustive, so adding one is a compile error here.
    let refusal = |status: u16, code: &str| {
        ClientError::Controller(Box::new(ControllerError {
            operation: "controller.request /agent/heartbeat".to_owned(),
            endpoint: "/agent/heartbeat".to_owned(),
            status,
            code: code.to_owned(),
            request_id: None,
            decision: "exit",
            retry_after_seconds: None,
            summary: None,
        }))
    };
    // A Controller that is asking for the same request again.
    for status in [408, 429, 500, 503] {
        assert_eq!(
            classify_heartbeat_failure(&refusal(status, "controller_unavailable")),
            HeartbeatFailure::Retryable,
            "status {status}"
        );
    }
    assert_eq!(
        classify_heartbeat_failure(&ClientError::Retryable),
        HeartbeatFailure::Retryable
    );
    assert_eq!(
        classify_heartbeat_failure(&ClientError::Protocol),
        HeartbeatFailure::Retryable
    );
    // A refused renewal: authority, fence, a lease past its allowance, or an
    // invalid claim.  None of these is repaired by sending it again.
    for status in [400, 401, 403, 404, 409, 410, 422] {
        assert_eq!(
            classify_heartbeat_failure(&refusal(status, "stale_agent_attempt")),
            HeartbeatFailure::Terminal,
            "status {status}"
        );
    }
    assert_eq!(
        classify_heartbeat_failure(&refusal(409, "superseded_operation_cancelled")),
        HeartbeatFailure::SupersededCancellation
    );
    for terminal in [ClientError::Identity, ClientError::Pin] {
        assert_eq!(
            classify_heartbeat_failure(&terminal),
            HeartbeatFailure::Terminal
        );
    }
}

#[derive(Clone)]
struct DeferredDeliveryClient {
    inner: RecordingClient,
    unavailable: Arc<AtomicBool>,
}

#[async_trait]
impl LoopClient for DeferredDeliveryClient {
    async fn claim(
        &self,
        fingerprint: Option<&str>,
        wait: u64,
        identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        self.inner.claim(fingerprint, wait, identity).await
    }
    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        self.inner.heartbeat(progress).await
    }
    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        if self.unavailable.load(Ordering::SeqCst) {
            Err(ClientError::Retryable)
        } else {
            self.inner.submit_result(result).await
        }
    }
}

#[tokio::test]
async fn failed_old_delivery_admits_fresh_work_then_reconciles_after_fault_clear() {
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    completed_install_result(&mut state);
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    let client = DeferredDeliveryClient {
        inner: RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(fresh))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        },
        unavailable: Arc::new(AtomicBool::new(true)),
    };
    // Result delivery is unavailable, but the new request still executes and
    // its exact outcome remains in custody alongside the older one.
    assert!(
        run_once(&client, &mut state, &RejectingExecutor, None, 0, None)
            .await
            .is_err()
    );
    assert!(client.inner.claim.lock().unwrap().is_none());
    assert_eq!(state.pending_results().unwrap().len(), 2);
    client.unavailable.store(false, Ordering::SeqCst);
    let mut newer = claim();
    newer.fence = Uuid::new_v4();
    client.inner.claim.lock().unwrap().replace(newer);
    run_once(&client, &mut state, &RejectingExecutor, None, 0, None)
        .await
        .unwrap();
    assert!(state.pending_results().unwrap().is_empty());
    assert_eq!(client.inner.results.lock().unwrap().len(), 3);
}

struct DamagedDeadlineExecutor {
    path: std::path::PathBuf,
    observed: HeartbeatGatedExecutor,
}

#[async_trait(?Send)]
impl Executor for DamagedDeadlineExecutor {
    async fn execute(
        &self,
        claim: &AgentClaim,
        deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        let connection = rusqlite::Connection::open(&self.path).unwrap();
        connection
            .execute("UPDATE operations SET deadline='damaged projection'", [])
            .unwrap();
        self.observed.execute(claim, deadline, cancellation).await
    }
}

#[tokio::test]
async fn damaged_local_deadline_does_not_cancel_verified_renewal_or_fresh_claims() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let mut state = StateStore::open(&path, NODE_ID).unwrap();
    let heartbeats = Arc::new(Mutex::new(Vec::new()));
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: false,
        heartbeats: heartbeats.clone(),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let executor = DamagedDeadlineExecutor {
        path,
        observed: HeartbeatGatedExecutor {
            heartbeats,
            minimum: 2,
            observed_deadline: Arc::new(Mutex::new(None)),
        },
    };
    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &executor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(5),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();
    assert!(state.pending_results().unwrap().is_empty());
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    client.claim.lock().unwrap().replace(fresh);
    run_once(&client, &mut state, &RejectingExecutor, None, 0, None)
        .await
        .unwrap();
    assert!(state.pending_results().unwrap().is_empty());
}
