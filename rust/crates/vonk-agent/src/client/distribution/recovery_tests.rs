#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn reference_busy_manifest_retries_in_the_original_download() {
    // Catches the three-request cutoff interrupting recipe.start while its
    // exact accepted manifest is merely waiting on reference bookkeeping.
    let model = b"small model object";
    let assignment = distribution_assignment_fixture(model);
    let mut objects = HashMap::new();
    objects.insert(hex_sha256(model), model.to_vec());
    let (client, server) = distribution_fixture_server(
        assignment,
        objects,
        6,
        DistributionFixtureMode::ReferenceBusyFirstFourManifests,
    );
    let root = tempfile::tempdir().unwrap();
    let evidence = client
        .download_distribution(TEST_PLAN_DIGEST, root.path())
        .await;
    let requests = server.finish().unwrap();
    let targets: Vec<_> = requests
        .iter()
        .map(|request| {
            String::from_utf8_lossy(request)
                .lines()
                .next()
                .unwrap_or_default()
                .to_owned()
        })
        .collect();
    assert!(evidence.is_ok(), "{evidence:?}; requests={targets:?}");
    let evidence = evidence.unwrap();
    assert_eq!(std::fs::read(&evidence.model_paths[0]).unwrap(), model);
    assert_eq!(requests.len(), 6);
    for request in &requests[..5] {
        assert!(String::from_utf8_lossy(request).contains(TEST_PLAN_DIGEST));
    }
}

#[tokio::test]
async fn reference_busy_manifest_preserves_cause_at_its_deadline() {
    // Catches resetting the budget on each wait or losing its typed cause.
    let assignment = distribution_assignment_fixture(b"model");
    let (client, server) = distribution_fixture_server(
        assignment,
        HashMap::new(),
        1,
        DistributionFixtureMode::ReferenceBusyFirstFourManifests,
    );
    let error = client
        .distribution_manifest_until(
            TEST_PLAN_DIGEST,
            tokio::time::Instant::now() + Duration::from_millis(500),
        )
        .await
        .unwrap_err();
    assert_eq!(error.disposition(), Disposition::Wait);
    assert!(error.code().is_some_and(|code| vocabulary::is(
        code,
        vonk_agent_protocol::generated::ArtifactLifecycleCode::ArtifactReferenceBusy,
    )));
    assert_eq!(server.finish().unwrap().len(), 1);
}

#[tokio::test]
async fn distribution_acceptance_rejects_an_unauthorized_destination() {
    let model = b"small model object";
    let assignment = distribution_assignment_fixture(model);
    let mut objects = HashMap::new();
    objects.insert(hex_sha256(model), model.to_vec());
    let (authorized_client, authorized_server) = distribution_fixture_server(
        assignment.clone(),
        objects.clone(),
        1,
        DistributionFixtureMode::Good,
    );
    let unauthorized_client =
        AgentHttpClient::for_http_test(authorized_client.controller.as_str(), TEST_NODE_ID);
    assert!(matches!(
        unauthorized_client
            .distribution_manifest(TEST_PLAN_DIGEST)
            .await,
        Err(ClientError::Controller(error)) if error.status == 401
    ));
    assert_eq!(authorized_server.finish().unwrap().len(), 1);
}

#[tokio::test]
async fn damaged_managed_objects_and_oversized_checkpoints_refetch_exact_content() {
    for (final_damage, directory_damage) in [(true, false), (false, false), (true, true)] {
        let model = b"small model object";
        let assignment = distribution_assignment_fixture(model);
        let root = tempfile::tempdir().unwrap();
        let destination = root
            .path()
            .join("models")
            .join(&assignment.objects[0].sha256);
        std::fs::create_dir_all(destination.parent().unwrap()).unwrap();
        let damaged = if final_damage {
            destination.clone()
        } else {
            partial_path(&destination)
        };
        if directory_damage {
            std::fs::create_dir(&damaged).unwrap();
            std::fs::write(damaged.join("preserved"), b"unmatched local bytes").unwrap();
        } else {
            std::fs::write(&damaged, vec![0; model.len() + 1]).unwrap();
            std::fs::set_permissions(&damaged, std::fs::Permissions::from_mode(0o600)).unwrap();
        }
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        let (client, server) =
            distribution_fixture_server(assignment, objects, 3, DistributionFixtureMode::Good);
        client
            .download_distribution(TEST_PLAN_DIGEST, root.path())
            .await
            .unwrap();
        assert_eq!(std::fs::read(&destination).unwrap(), model);
        // A fresh request reuses the repaired object with no second transfer.
        client
            .download_distribution(TEST_PLAN_DIGEST, root.path())
            .await
            .unwrap();
        assert_eq!(server.finish().unwrap().len(), 3);
    }
}

#[tokio::test]
async fn malformed_delivery_is_refused_before_effects_and_a_fresh_request_is_admitted() {
    for (mode, malformed_attempts, requests) in [
        (DistributionFixtureMode::MalformedFirstManifest, 1, 3),
        (DistributionFixtureMode::MalformedFirstThreeManifests, 3, 5),
    ] {
        let model = b"small model object";
        let assignment = distribution_assignment_fixture(model);
        let root = tempfile::tempdir().unwrap();
        let destination = root
            .path()
            .join("models")
            .join(&assignment.objects[0].sha256);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        let (client, server) = distribution_fixture_server(assignment, objects, requests, mode);
        for _ in 0..malformed_attempts {
            let error = client
                .download_distribution(TEST_PLAN_DIGEST, root.path())
                .await
                .unwrap_err();
            assert!(!error.retryable());
            assert!(!root.path().join("models").exists());
        }
        client
            .download_distribution(TEST_PLAN_DIGEST, root.path())
            .await
            .unwrap();
        assert_eq!(std::fs::read(&destination).unwrap(), model);
        assert_eq!(server.finish().unwrap().len(), requests);
    }
}

#[tokio::test]
async fn repeated_truncation_that_keeps_moving_bytes_finishes_without_spending_retries() {
    let model = b"small model object";
    let assignment = distribution_assignment_fixture(model);
    let root = tempfile::tempdir().unwrap();
    let destination = root.path().join("model");
    let mut objects = HashMap::new();
    objects.insert(hex_sha256(model), model.to_vec());
    let (client, server) = distribution_fixture_server(
        assignment,
        objects,
        6,
        DistributionFixtureMode::InterruptFirstFiveObjects,
    );
    // Five truncated ranges each move bytes: progress is not a spent retry,
    // so one request resumes through all of them.
    client
        .download_distribution_object(
            TEST_PLAN_DIGEST,
            &hex_sha256(model),
            model.len() as u64,
            &destination,
        )
        .await
        .unwrap();
    assert_eq!(std::fs::read(&destination).unwrap(), model);
    assert!(!partial_path(&destination).exists());
    assert_eq!(server.finish().unwrap().len(), 6);
}

#[tokio::test]
async fn malformed_range_observation_ends_boundedly_and_same_digest_is_admitted_after_repair() {
    let model = b"small model object";
    let digest = hex_sha256(model);
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let client = authenticated_test_client(&format!("http://{address}"), TEST_NODE_ID);
    let response_digest = digest.clone();
    // Keep the fault active until the first owner returns. The HTTP transport
    // may retry a connection itself; counting responses could accidentally
    // repair the fault inside that first request and make this test flaky.
    let repaired = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let peer_repaired = repaired.clone();
    let peer = spawn_peer(move || {
        let deadline = std::time::Instant::now() + PEER_BUDGET;
        let mut requests = Vec::new();
        loop {
            let mut socket = accept_peer(&listener, deadline);
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            while !request.windows(4).any(|part| part == b"\r\n\r\n") {
                assert!(std::time::Instant::now() < deadline);
                let read = socket.read(&mut buffer).unwrap();
                assert!(read > 0);
                request.extend_from_slice(&buffer[..read]);
            }
            requests.push(request);
            let repair_visible = peer_repaired.load(std::sync::atomic::Ordering::Acquire);
            let range = if !repair_visible {
                "unreadable".to_owned()
            } else {
                format!("bytes 0-{}/{}", model.len() - 1, model.len())
            };
            write!(socket, "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nETag: \"sha256:{}\"\r\nContent-Range: {}\r\nConnection: close\r\n\r\n",
                model.len(), response_digest, range).unwrap();
            // Framing loss never accepts these bytes into the final namespace.
            let _ = socket.write_all(model);
            if repair_visible {
                break;
            }
        }
        requests
    });
    let root = tempfile::tempdir().unwrap();
    let destination = root.path().join(&digest);
    let started = std::time::Instant::now();
    assert!(
        client
            .download_distribution_object(
                TEST_PLAN_DIGEST,
                &digest,
                model.len() as u64,
                &destination
            )
            .await
            .is_err()
    );
    assert!(started.elapsed() < Duration::from_secs(20));
    assert!(!destination.exists());
    repaired.store(true, std::sync::atomic::Ordering::Release);
    client
        .download_distribution_object(TEST_PLAN_DIGEST, &digest, model.len() as u64, &destination)
        .await
        .unwrap();
    assert_eq!(std::fs::read(&destination).unwrap(), model);
    client
        .download_distribution_object(TEST_PLAN_DIGEST, &digest, model.len() as u64, &destination)
        .await
        .unwrap();
    peer.finish().unwrap();
}
