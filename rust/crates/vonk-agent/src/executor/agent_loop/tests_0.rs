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
            Utc::now() - ChronoDuration::seconds(899),
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
    vonk_agent_protocol::revalidate(body).unwrap();
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
            recovered: Arc::new(AtomicBool::new(false)),
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
        client.recovered.store(true, Ordering::SeqCst);
        run_once(
            &client,
            &mut state,
            &HeartbeatGatedExecutor {
                heartbeats: client.inner.heartbeats.clone(),
                minimum: 0,
                observed_deadline: Arc::new(Mutex::new(None)),
            },
            None,
            0,
            None,
        )
        .await
        .unwrap();
        assert!(state.pending_results().unwrap().is_empty());
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_retryable_renewal_failure_after_the_lease_lapses_still_renews() {
    for (unreadable, reply_status) in [
        (true, None),
        (true, Some(404)),
        (true, Some(409)),
        (false, None),
    ] {
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
            unreadable,
            reply_status,
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
        {
            let accepted_at = accepted_at.lock().unwrap();
            assert!(
                accepted_at.iter().any(|instant| *instant > lease_deadline),
                "no renewal was accepted after the lease lapsed: {accepted_at:?}"
            );
            assert!(heartbeats.lock().unwrap().len() > accepted_at.len());
        }
        assert_eq!(client.inner.results.lock().unwrap().len(), 1);
        assert!(state.pending_results().unwrap().is_empty());
        let mut fresh = claim();
        fresh.fence = Uuid::new_v4();
        let fresh_fence = fresh.fence;
        *client.inner.claim.lock().unwrap() = Some(fresh);
        run_once(
            &client.inner,
            &mut state,
            &OrderingExecutor {
                events: Arc::new(Mutex::new(Vec::new())),
            },
            None,
            0,
            None,
        )
        .await
        .unwrap();
        assert_eq!(
            client
                .inner
                .results
                .lock()
                .unwrap()
                .last()
                .map(|result| result.fence),
            Some(fresh_fence)
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_retryable_renewal_failure_stops_once_the_start_budget_is_spent() {
    assert_renewal_unknown_ends_then_admits_fresh(true).await;
    assert_renewal_unknown_ends_then_admits_fresh(false).await;
}

async fn assert_renewal_unknown_ends_then_admits_fresh(unreadable: bool) {
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
        "phase": vonk_agent_protocol::generated::RecipeStartPayloadPhase::RankLaunch.as_str(),
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
        unreadable,
        reply_status: None,
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
    result.unwrap().unwrap();
    assert!(executor.cancelled.load(Ordering::SeqCst));
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    let fresh_fence = fresh.fence;
    let fresh_client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(fresh))),
        fail_heartbeat: false,
        heartbeats: Arc::new(Mutex::new(Vec::new())),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    run_once(
        &fresh_client,
        &mut state,
        &OrderingExecutor {
            events: Arc::new(Mutex::new(Vec::new())),
        },
        None,
        0,
        None,
    )
    .await
    .unwrap();
    assert!(
        fresh_client
            .results
            .lock()
            .unwrap()
            .iter()
            .any(|result| result.fence == fresh_fence)
    );
}

#[derive(Clone)]
struct StaleRenewalClient {
    inner: RecordingClient,
    stale: Arc<AtomicBool>,
}

#[async_trait]
impl LoopClient for StaleRenewalClient {
    async fn claim(
        &self,
        fingerprint: Option<&str>,
        wait: u64,
        identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        self.inner.claim(fingerprint, wait, identity).await
    }
    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        let mut directive = self.inner.heartbeat(progress).await?;
        if self.stale.swap(false, Ordering::SeqCst) {
            directive.deadline = (Utc::now() - ChronoDuration::seconds(1)).fixed_offset();
        }
        Ok(directive)
    }
    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.inner.submit_result(result).await
    }
}

#[tokio::test]
async fn stale_renewal_reobserves_then_completes_and_admits_fresh_execution() {
    // A shortened authenticated reply is not cancellation. Wrong behavior:
    // the old second gate cancelled work on the first stale projection.
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let client = StaleRenewalClient {
        inner: RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(claim()))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        },
        stale: Arc::new(AtomicBool::new(true)),
    };
    let executor = HeartbeatGatedExecutor {
        heartbeats: client.inner.heartbeats.clone(),
        minimum: 2,
        observed_deadline: Arc::new(Mutex::new(None)),
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
    .unwrap();
    assert!(executor.observed_deadline.lock().unwrap().unwrap() > Utc::now());
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    client.inner.claim.lock().unwrap().replace(fresh);
    run_once(&client, &mut state, &executor, None, 0, None)
        .await
        .unwrap();
    let fences: std::collections::HashSet<_> = client
        .inner
        .results
        .lock()
        .unwrap()
        .iter()
        .map(|result| result.fence)
        .collect();
    assert_eq!(fences.len(), 2);
    assert!(state.pending_results().unwrap().is_empty());
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
    run_once(&client, &mut state, &RejectingExecutor, None, 0, None)
        .await
        .unwrap();
    assert!(state.pending_results().unwrap().is_empty());
    let fences: std::collections::HashSet<_> = client
        .inner
        .results
        .lock()
        .unwrap()
        .iter()
        .map(|result| result.fence)
        .collect();
    assert_eq!(fences.len(), 3);
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
        path: path.clone(),
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
    drop(state);
    let mut state = StateStore::open_recovered(&path, NODE_ID).unwrap();
    let persisted: String = rusqlite::Connection::open(&path)
        .unwrap()
        .query_row("SELECT deadline FROM operations LIMIT 1", [], |row| {
            row.get(0)
        })
        .unwrap();
    let persisted = DateTime::parse_from_rfc3339(&persisted).unwrap();
    assert!(persisted >= executor.observed.observed_deadline.lock().unwrap().unwrap());
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    client.claim.lock().unwrap().replace(fresh);
    run_once(&client, &mut state, &RejectingExecutor, None, 0, None)
        .await
        .unwrap();
    assert!(state.pending_results().unwrap().is_empty());
}

#[tokio::test]
async fn damaged_receipt_and_failed_projection_writes_do_not_gate_fresh_success() {
    for damaged in [
        rusqlite::types::Value::Blob(vec![0]),
        rusqlite::types::Value::Text("damaged receipt".to_owned()),
    ] {
        let directory = tempdir().unwrap();
        let path = directory.path().join("state.sqlite");
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        let old = completed_install_result(&mut state);
        state
            .reject_result(&old, &ControllerError::from_status(422), Utc::now())
            .unwrap();
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute("UPDATE result_rejections SET retry_due_at='broken'", [])
            .unwrap();
        // Inject on-disk schema damage before writing the wrong SQL storage
        // class: STRICT normally rejects TEXT before our reader can see it.
        db.execute_batch("PRAGMA writable_schema=ON; UPDATE sqlite_schema SET sql=replace(sql, ' STRICT', '') WHERE name='operations'; PRAGMA writable_schema=RESET;").unwrap();
        db.execute(
            "UPDATE operations SET result_json=?1",
            rusqlite::params![damaged],
        )
        .unwrap();
        db.execute_batch("CREATE TRIGGER prevent_suppression_cleanup BEFORE DELETE ON result_rejections BEGIN SELECT RAISE(FAIL,'projection unavailable'); END;
            CREATE TRIGGER prevent_old_ack BEFORE UPDATE ON operations WHEN OLD.fence=(SELECT fence FROM result_rejections LIMIT 1) BEGIN SELECT RAISE(FAIL,'projection unavailable'); END;").unwrap();
        let mut fresh = claim();
        fresh.fence = Uuid::new_v4();
        let client = RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(fresh))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let executor = HeartbeatGatedExecutor {
            heartbeats: client.heartbeats.clone(),
            minimum: 0,
            observed_deadline: Arc::new(Mutex::new(None)),
        };
        run_once(&client, &mut state, &executor, None, 0, None)
            .await
            .unwrap();
        let preserved: rusqlite::types::Value = db
            .query_row(
                "SELECT result_json FROM operations WHERE fence=?1",
                [old.fence.to_string()],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(preserved, damaged);
        assert!(client.results.lock().unwrap().iter().any(|result| matches!(
            result.result,
            vonk_agent_protocol::generated::AgentResultResult::OutcomeDone(_)
        )));
        db.execute_batch("DROP TRIGGER prevent_suppression_cleanup; DROP TRIGGER prevent_old_ack;")
            .unwrap();
        let mut newer = claim();
        newer.fence = Uuid::new_v4();
        client.claim.lock().unwrap().replace(newer);
        run_once(&client, &mut state, &executor, None, 0, None)
            .await
            .unwrap();
        // Wrapping the cursor takes one completed pass, then reoffers the damaged
        // row as uncertainty. It never replaces or executes the old effect.
        run_once(&client, &mut state, &executor, None, 0, None)
            .await
            .unwrap();
        let acknowledged: bool = db
            .query_row(
                "SELECT result_acknowledged FROM operations WHERE fence=?1",
                [old.fence.to_string()],
                |row| row.get(0),
            )
            .unwrap();
        assert!(acknowledged);
    }
}

#[tokio::test]
async fn unavailable_custody_observes_without_effects_and_restores_fresh_execution() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    std::fs::create_dir(&path).unwrap();
    assert!(StateStore::open_recovered(&path, NODE_ID).is_err());
    let mut state = StateStore::observation_only(&path, NODE_ID).unwrap();
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: false,
        heartbeats: Arc::new(Mutex::new(Vec::new())),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    // No heartbeat means no executor was dispatched through unavailable
    // custody; the Controller gets uncertainty instead of silence.
    let executor = HeartbeatGatedExecutor {
        heartbeats: client.heartbeats.clone(),
        minimum: 0,
        observed_deadline: Arc::new(Mutex::new(None)),
    };
    run_once(&client, &mut state, &executor, None, 0, None)
        .await
        .unwrap();
    assert!(executor.observed_deadline.lock().unwrap().is_none());
    assert!(path.is_dir());
    std::fs::remove_dir(&path).unwrap();
    state.restore_custody();
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    client.claim.lock().unwrap().replace(fresh);
    run_once(&client, &mut state, &executor, None, 0, None)
        .await
        .unwrap();
    assert!(executor.observed_deadline.lock().unwrap().is_some());
    assert!(state.pending_results().unwrap().is_empty());
}

#[derive(Clone)]
struct SilentHeartbeatClient {
    inner: RecordingClient,
    unavailable: Arc<AtomicBool>,
}

#[async_trait]
impl LoopClient for SilentHeartbeatClient {
    async fn claim(
        &self,
        fingerprint: Option<&str>,
        wait: u64,
        identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        self.inner.claim(fingerprint, wait, identity).await
    }
    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        if self.unavailable.load(Ordering::SeqCst) {
            std::future::pending().await
        } else {
            self.inner.heartbeat(progress).await
        }
    }
    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.inner.submit_result(result).await
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn silent_non_start_renewal_ends_at_immutable_budget_and_fresh_work_succeeds() {
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let mut old = claim();
    old.observation_budget_seconds = 1;
    let client = SilentHeartbeatClient {
        inner: RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(old))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        },
        unavailable: Arc::new(AtomicBool::new(true)),
    };
    let executor = BlockingCancellationExecutor {
        cancelled: Arc::new(AtomicBool::new(false)),
    };
    tokio::time::timeout(
        Duration::from_secs(3),
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
    .await
    .unwrap()
    .unwrap();
    assert!(executor.cancelled.load(Ordering::SeqCst));
    client.unavailable.store(false, Ordering::SeqCst);
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    client.inner.claim.lock().unwrap().replace(fresh);
    let fresh_executor = HeartbeatGatedExecutor {
        heartbeats: client.inner.heartbeats.clone(),
        minimum: 0,
        observed_deadline: Arc::new(Mutex::new(None)),
    };
    run_once(&client, &mut state, &fresh_executor, None, 0, None)
        .await
        .unwrap();
    assert!(fresh_executor.observed_deadline.lock().unwrap().is_some());
    assert!(state.pending_results().unwrap().is_empty());
}

#[tokio::test]
async fn projections_removed_after_startup_are_rebuilt_before_fresh_success() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let mut state = StateStore::open(&path, NODE_ID).unwrap();
    let old = completed_install_result(&mut state);
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch("DROP TABLE result_rejections; DROP TABLE result_reconciliation;")
        .unwrap();
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    let fresh_fence = fresh.fence;
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(fresh))),
        fail_heartbeat: false,
        heartbeats: Arc::new(Mutex::new(Vec::new())),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let executor = HeartbeatGatedExecutor {
        heartbeats: client.heartbeats.clone(),
        minimum: 0,
        observed_deadline: Arc::new(Mutex::new(None)),
    };
    run_once(&client, &mut state, &executor, None, 0, None)
        .await
        .unwrap();
    {
        let submitted = client.results.lock().unwrap();
        assert!(submitted.iter().any(|result| result == &old));
        assert!(submitted.iter().any(|result| result.fence == fresh_fence
            && matches!(
                result.result,
                vonk_agent_protocol::generated::AgentResultResult::OutcomeDone(_)
            )));
    }
    // The old effect is delivered from its exact receipt, never dispatched.
    assert!(state.pending_results().unwrap().is_empty());
    drop(state);
    let mut state = StateStore::open_recovered(&path, NODE_ID).unwrap();
    let mut newer = claim();
    newer.fence = Uuid::new_v4();
    client.claim.lock().unwrap().replace(newer);
    run_once(&client, &mut state, &executor, None, 0, None)
        .await
        .unwrap();
    assert!(state.pending_results().unwrap().is_empty());
}
