#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn distribution_consumer_fetches_manifest_and_all_objects_with_ranges() {
    let model = b"model payload".to_vec();
    let config = b"config!".to_vec();
    let digest = |bytes: &[u8]| hex_sha256(bytes);
    let model_digest = digest(&model);
    let config_digest = digest(&config);
    let assignment = vonk_agent_protocol::DistributionAssignment {
        objects: vec![
            vonk_agent_protocol::DistributionObject {
                name: "weights/model.bin".to_owned(),
                sha256: model_digest.clone(),
                bytes: model.len() as u64,
                kind: "model".to_owned(),
            },
            vonk_agent_protocol::DistributionObject {
                name: "config/tokenizer.json".to_owned(),
                sha256: config_digest.clone(),
                bytes: config.len() as u64,
                kind: "model".to_owned(),
            },
        ],
        oci_image_digest: format!("sha256:{}", "d".repeat(64)),
        oci_image_config_digest: format!("sha256:{}", "e".repeat(64)),
    };
    assignment.validate().unwrap();
    let manifest = canonical_json(&assignment).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = spawn_peer(move || {
        let deadline = std::time::Instant::now() + PEER_BUDGET;
        let objects = [(model_digest, model), (config_digest, config)];
        for _ in 0..3 {
            let mut stream = accept_peer(&listener, deadline);
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            while !request.windows(4).any(|value| value == b"\r\n\r\n") {
                assert!(
                    std::time::Instant::now() < deadline,
                    "fixture read deadline"
                );
                let size = stream.read(&mut buffer).unwrap();
                assert!(size > 0);
                request.extend_from_slice(&buffer[..size]);
            }
            let headers = String::from_utf8_lossy(&request);
            let target = headers
                .lines()
                .next()
                .unwrap()
                .split_whitespace()
                .nth(1)
                .unwrap();
            if target.starts_with("/agent/distribution/manifests/") {
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    manifest.len()
                )
                .unwrap();
                stream.write_all(&manifest).unwrap();
                continue;
            }
            let object_digest = target
                .strip_prefix("/agent/distribution/objects/")
                .unwrap()
                .split('?')
                .next()
                .unwrap();
            let body = objects
                .iter()
                .find(|(digest, _)| digest == object_digest)
                .unwrap()
                .1
                .as_slice();
            let range = headers.lines().find_map(|line| {
                line.strip_prefix("range: bytes=")
                    .or_else(|| line.strip_prefix("Range: bytes="))
            });
            let (start, end) = range
                .unwrap()
                .split_once('-')
                .map(|(start, end)| {
                    (
                        start.parse::<usize>().unwrap(),
                        end.parse::<usize>().unwrap(),
                    )
                })
                .unwrap();
            let chunk = &body[start..=end];
            write!(stream, "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes {}-{}/{}\r\nETag: \"sha256:{}\"\r\nConnection: close\r\n\r\n", chunk.len(), start, end, body.len(), object_digest).unwrap();
            stream.write_all(chunk).unwrap();
        }
    });
    let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), TEST_NODE_ID);
    let root = tempfile::tempdir().unwrap();
    let evidence = client
        .download_distribution(TEST_PLAN_DIGEST, root.path())
        .await
        .unwrap();
    assert_eq!(
        std::fs::read(&evidence.model_paths[0]).unwrap(),
        b"model payload"
    );
    assert_eq!(std::fs::read(&evidence.model_paths[1]).unwrap(), b"config!");
    // The runtime image is pulled by these digests, never downloaded here.
    assert_eq!(
        evidence.oci_image_digest,
        format!("sha256:{}", "d".repeat(64))
    );
    assert_eq!(
        evidence.oci_image_config_digest,
        format!("sha256:{}", "e".repeat(64))
    );
    server.finish().unwrap();
}

#[tokio::test]
async fn distribution_downloads_overlap_without_reordering_evidence_or_progress() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    async fn request(stream: &mut tokio::net::TcpStream) -> String {
        let mut bytes = Vec::new();
        while !bytes.ends_with(b"\r\n\r\n") {
            assert!(bytes.len() < 4096);
            bytes.push(
                tokio::time::timeout(PEER_BUDGET, stream.read_u8())
                    .await
                    .expect("fixture I/O deadline")
                    .unwrap(),
            );
        }
        String::from_utf8(bytes).unwrap()
    }

    let first = b"first model file";
    let second = b"second model file";
    let mut assignment = distribution_assignment_fixture(first);
    assignment.objects.insert(
        1,
        vonk_agent_protocol::DistributionObject {
            name: "weights/second.bin".to_owned(),
            sha256: hex_sha256(second),
            bytes: second.len() as u64,
            kind: "model".to_owned(),
        },
    );
    assignment.validate().unwrap();
    let manifest = canonical_json(&assignment).unwrap();
    let objects = HashMap::from([
        (hex_sha256(first), first.to_vec()),
        (hex_sha256(second), second.to_vec()),
    ]);
    let completion_order = assignment
        .objects
        .iter()
        .rev()
        .map(|object| object.sha256.clone())
        .collect::<Vec<_>>();
    let completed = Arc::new(tokio::sync::Semaphore::new(0));
    let observed = completed.clone();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = spawn_async_peer(async move {
        let (mut stream, _) = tokio::time::timeout(PEER_BUDGET, listener.accept())
            .await
            .expect("fixture I/O deadline")
            .unwrap();
        assert!(
            request(&mut stream)
                .await
                .contains("/agent/distribution/manifests/")
        );
        stream
            .write_all(
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    manifest.len(),
                )
                .as_bytes(),
            )
            .await
            .unwrap();
        stream.write_all(&manifest).await.unwrap();
        drop(stream);

        // Withhold bodies until both requests are in flight. A serial
        // downloader cannot pass this barrier; no throughput threshold is used.
        let mut pending = Vec::new();
        for _ in 0..2 {
            let (mut stream, _) = tokio::time::timeout(PEER_BUDGET, listener.accept())
                .await
                .expect("fixture I/O deadline")
                .unwrap();
            let headers = request(&mut stream).await;
            pending.push((stream, headers));
        }
        for digest in completion_order {
            let position = pending
                .iter()
                .position(|(_, headers)| {
                    headers.contains(&format!("/agent/distribution/objects/{digest}?"))
                })
                .unwrap();
            let (mut stream, headers) = pending.swap_remove(position);
            let body = &objects[&digest];
            assert!(
                headers
                    .to_lowercase()
                    .contains(&format!("range: bytes=0-{}", body.len() - 1))
            );
            stream.write_all(format!(
                "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\nETag: \"sha256:{}\"\r\nConnection: close\r\n\r\n",
                body.len(), body.len() - 1, body.len(), digest,
            ).as_bytes()).await.unwrap();
            stream.write_all(body).await.unwrap();
            drop(stream);
            // Force reverse verification order before releasing the next
            // body, so completion order cannot accidentally match the manifest.
            tokio::time::timeout(PEER_BUDGET, observed.acquire())
                .await
                .expect("fixture I/O deadline")
                .unwrap()
                .forget();
        }
    });
    let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), TEST_NODE_ID);
    let root = tempfile::tempdir().unwrap();
    let mut snapshots = Vec::new();
    let mut completed_items = 0;
    let result = tokio::time::timeout(
        Duration::from_secs(10),
        client.download_distribution_with_progress(TEST_PLAN_DIGEST, root.path(), |item| {
            if item.completed_items > completed_items {
                assert_eq!(item.completed_items, completed_items + 1);
                completed_items = item.completed_items;
                completed.add_permits(1);
            }
            snapshots.push(item);
        }),
    )
    .await;
    if result.is_err() {
        server.abort();
    }
    let evidence = result
        .expect("independent distribution requests must overlap")
        .unwrap();
    server.await.unwrap();
    assert_eq!(
        evidence.model_digests,
        vec![hex_sha256(first), hex_sha256(second)]
    );
    assert_eq!(std::fs::read(&evidence.model_paths[0]).unwrap(), first);
    assert_eq!(std::fs::read(&evidence.model_paths[1]).unwrap(), second);
    assert!(
        snapshots
            .windows(2)
            .all(|pair| pair[0].bytes <= pair[1].bytes
                && pair[0].completed_items <= pair[1].completed_items)
    );
    let last = snapshots.last().unwrap();
    assert_eq!(last.completed_items, assignment.objects.len() as u64);
    assert_eq!(last.bytes, evidence.downloaded_bytes);
}

#[test]
fn transfers_plan_files_in_sixty_four_mebibyte_ranges_with_adaptive_streams() {
    const MIB: u64 = 1024 * 1024;
    // Objects open at once are the most streams the governor may allow.
    assert_eq!(DISTRIBUTION_CONCURRENCY, 8);
    // A large object is walked in whole 64 MiB windows...
    assert_eq!(range_end(0, 1024 * MIB), 64 * MIB - 1);
    assert_eq!(range_end(64 * MIB, 1024 * MIB), 128 * MIB - 1);
    // ...a resumed offset continues from where the partial ended...
    assert_eq!(range_end(100 * MIB, 1024 * MIB), 164 * MIB - 1);
    // ...and the last window stops at the final byte.
    assert_eq!(range_end(64 * MIB, 70 * MIB), 70 * MIB - 1);
    assert_eq!(range_end(0, 5), 4);
}

#[tokio::test]
async fn write_path_reserves_space_without_changing_the_length_and_writes_exact_bytes() {
    use tokio::io::{AsyncWriteExt, BufWriter};
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("object.partial");
    let payload: Vec<u8> = (0..5 * 1024 * 1024_u32).map(|value| value as u8).collect();
    let file = open_trusted_partial(&path).await.unwrap();
    preallocate(&file, 0, payload.len() as u64);
    // Resume reads the partial's length as the bytes already received, so
    // reserving space must not make it look complete.
    assert_eq!(file.metadata().await.unwrap().len(), 0);
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::MetadataExt;
        // tmpfs and the CI filesystems reserve blocks; skip nothing on Linux.
        assert!(file.metadata().await.unwrap().blocks() * 512 >= payload.len() as u64);
    }

    // A one-MiB window forces several background flushes during the write.
    let mut output = BufWriter::with_capacity(64 * 1024, file);
    let mut write_behind = WriteBehind::with_window(0, 1024 * 1024);
    let mut written = 0_u64;
    for chunk in payload.chunks(256 * 1024) {
        output.write_all(chunk).await.unwrap();
        written += chunk.len() as u64;
        write_behind.written(&mut output, written).await.unwrap();
    }
    write_behind.finish().await.unwrap();
    output.flush().await.unwrap();
    output.get_ref().sync_all().await.unwrap();
    assert_eq!(std::fs::read(&path).unwrap(), payload);
}

#[tokio::test]
async fn distribution_acceptance_uses_authenticated_ranges_and_replays_without_refetching() {
    let model = b"small model object";
    let assignment = distribution_assignment_fixture(model);
    assignment.validate().unwrap();
    let mut objects = HashMap::new();
    objects.insert(hex_sha256(model), model.to_vec());
    // Manifest and model, then a replay that fetches only the manifest.
    let (client, server) = distribution_fixture_server(
        assignment.clone(),
        objects,
        3,
        DistributionFixtureMode::Good,
    );
    let root = tempfile::tempdir().unwrap();
    let assignment_root = root.path().join("distribution").join("plan");
    std::fs::create_dir_all(&assignment_root).unwrap();
    let mut snapshots = Vec::new();
    let evidence = client
        .download_distribution_with_progress(TEST_PLAN_DIGEST, &assignment_root, |item| {
            snapshots.push(item)
        })
        .await
        .unwrap();
    // One operation-wide phase for the whole transfer: a per-object step
    // must not make it flip while other objects are still moving.
    assert!(
        snapshots
            .iter()
            .all(|item| item.phase == ProgressPhase::Copying)
    );
    assert!(
        snapshots
            .windows(2)
            .all(|pair| pair[0].bytes <= pair[1].bytes)
    );
    let final_progress = snapshots.last().unwrap();
    assert_eq!(final_progress.bytes, evidence.downloaded_bytes);
    assert_eq!(final_progress.completed_items, final_progress.total_items);
    assert_eq!(evidence.oci_image_digest, assignment.oci_image_digest);
    assert_eq!(
        evidence.oci_image_config_digest,
        assignment.oci_image_config_digest
    );
    assert_eq!(std::fs::read(&evidence.model_paths[0]).unwrap(), model);
    assert_eq!(
        std::fs::metadata(&evidence.model_paths[0])
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    // Replace the object with same-size different bytes: a replay trusts
    // name, size and custody, so it neither re-hashes nor re-fetches.
    std::fs::write(&evidence.model_paths[0], vec![0_u8; model.len()]).unwrap();
    let replayed = client
        .download_distribution(TEST_PLAN_DIGEST, &assignment_root)
        .await
        .unwrap();
    assert_eq!(
        std::fs::read(&replayed.model_paths[0]).unwrap(),
        vec![0_u8; model.len()]
    );
    assert_eq!(replayed.downloaded_bytes, evidence.downloaded_bytes);
    let requests = server.finish().unwrap();
    assert_eq!(requests.len(), 3);
    assert!(requests.iter().all(|request| {
        String::from_utf8_lossy(request)
            .to_ascii_lowercase()
            .contains("x-vonk-fixture-auth: enrolled-agent")
    }));
}

#[tokio::test]
async fn content_addressed_distribution_handoff_reuses_objects_across_plan_digests() {
    let model = b"model payload".to_vec();
    let assignment = distribution_assignment_fixture(&model);
    let plan_digest = "a".repeat(64);
    let model_artifact_set_sha256 = "d".repeat(64);
    assignment.validate().unwrap();
    let mut objects = HashMap::new();
    objects.insert(hex_sha256(&model), model.clone());
    let (client, server) = distribution_fixture_server(
        assignment.clone(),
        objects.clone(),
        2,
        DistributionFixtureMode::Good,
    );
    let root = tempfile::tempdir().unwrap();
    let distribution_root = root.path().join("distribution");
    std::fs::create_dir_all(&distribution_root).unwrap();
    let evidence = client
        .download_distribution(&plan_digest, &distribution_root)
        .await
        .unwrap();
    assert_eq!(server.finish().unwrap().len(), 2);
    assert_eq!(
        evidence.model_paths,
        vec![distribution_root.join("models").join(hex_sha256(&model))]
    );

    let mut plan_value: Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
    ))
    .unwrap();
    let model_sha256 = hex_sha256(&model);
    plan_value["identity"]["model_artifact_set_sha256"] = json!(model_artifact_set_sha256.clone());
    plan_value["artifacts"][0]["sha256"] = json!(model_sha256.clone());
    plan_value["artifacts"][0]["size_bytes"] = json!(model.len());
    let plan: CompiledExecutionPlan = serde_json::from_value(plan_value).unwrap();
    plan.validate().unwrap();

    let runner = NoProcess;
    let runtime = OciRuntime {
        runner: &runner,
        data_root: root.path(),
    };
    let first_installation = "cb555393-764b-4eb6-8f15-b416d289428f";
    runtime
        .install(
            &plan,
            first_installation,
            &plan.identity.recipe_revision_sha256,
        )
        .unwrap();
    assert_eq!(
        std::fs::read(root.path().join(format!(
            "installations/{first_installation}/models/primary/weights.bin"
        )))
        .unwrap(),
        model
    );

    runtime
        .install(
            &plan,
            first_installation,
            &plan.identity.recipe_revision_sha256,
        )
        .unwrap();

    let second_assignment = assignment.clone();
    let second_plan_digest = "f".repeat(64);
    let second_model_artifact_set_sha256 = "c".repeat(64);
    let (second_client, second_server) = distribution_fixture_server(
        second_assignment.clone(),
        objects,
        1,
        DistributionFixtureMode::Good,
    );
    let reused = second_client
        .download_distribution(&second_plan_digest, &distribution_root)
        .await
        .unwrap();
    assert_eq!(reused.downloaded_bytes, evidence.downloaded_bytes);
    assert_eq!(
        reused.model_paths,
        vec![distribution_root.join("models").join(&model_sha256)]
    );
    assert_eq!(
        std::fs::read_dir(distribution_root.join("models"))
            .unwrap()
            .count(),
        1,
        "the same object is retained once across artifact sets"
    );
    assert_eq!(second_server.finish().unwrap().len(), 1);

    let mut second_plan = plan.clone();
    second_plan.identity.model_artifact_set_sha256 = second_model_artifact_set_sha256.clone();
    let second_installation = "cb555393-764b-4eb6-8f15-b416d2894290";
    runtime
        .install(
            &second_plan,
            second_installation,
            &second_plan.identity.recipe_revision_sha256,
        )
        .unwrap();
    assert_eq!(
        std::fs::read(root.path().join(format!(
            "installations/{second_installation}/models/primary/weights.bin"
        )))
        .unwrap(),
        model
    );
}

#[tokio::test]
async fn distribution_acceptance_resumes_partial_object_and_rejects_corruption() {
    let model = b"small model object";
    let assignment = distribution_assignment_fixture(model);
    let mut objects = HashMap::new();
    objects.insert(hex_sha256(model), model.to_vec());
    let root = tempfile::tempdir().unwrap();
    let model_path = root
        .path()
        .join("models")
        .join(&assignment.objects[0].sha256);
    let partial_path = PathBuf::from(format!("{}.partial", model_path.display()));
    std::fs::create_dir_all(model_path.parent().unwrap()).unwrap();
    std::fs::write(&partial_path, &model[..5]).unwrap();
    std::fs::set_permissions(&partial_path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let (client, server) = distribution_fixture_server(
        assignment.clone(),
        objects,
        2,
        DistributionFixtureMode::Good,
    );
    client
        .download_distribution(TEST_PLAN_DIGEST, root.path())
        .await
        .unwrap();
    assert_eq!(std::fs::read(&model_path).unwrap(), model);
    let requests = server.finish().unwrap();
    let model_request = requests
        .iter()
        .map(|request| String::from_utf8_lossy(request).to_ascii_lowercase())
        .find(|request| {
            request.contains(&format!(
                "/agent/distribution/objects/{}?",
                assignment.objects[0].sha256
            ))
        })
        .unwrap();
    assert!(model_request.contains("range: bytes=5-"));

    let corrupt_root = tempfile::tempdir().unwrap();
    let (corrupt_client, corrupt_server) = distribution_fixture_server(
        assignment.clone(),
        {
            let mut values = HashMap::new();
            values.insert(hex_sha256(model), model.to_vec());
            values
        },
        2,
        DistributionFixtureMode::WrongEtagFirstObject,
    );
    assert!(matches!(
        corrupt_client
            .download_distribution(TEST_PLAN_DIGEST, corrupt_root.path())
            .await,
        Err(ClientError::Protocol)
    ));
    assert_eq!(corrupt_server.finish().unwrap().len(), 2);
}

#[tokio::test]
async fn a_finished_object_installations_link_to_is_still_a_trusted_final_object() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let object = dir.path().join("object");
    std::fs::write(&object, b"model").unwrap();
    let set_mode =
        |mode| std::fs::set_permissions(&object, std::fs::Permissions::from_mode(mode)).unwrap();
    set_mode(0o600);

    // Installations hold hard links to it; that does not make it untrusted.
    std::fs::hard_link(&object, dir.path().join("installation-link")).unwrap();
    assert!(
        super::inspect_trusted_final(&object, 5)
            .await
            .unwrap()
            .is_some()
    );
    // Its size is still the identity.
    assert!(
        super::inspect_trusted_final(&object, 6)
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        std::fs::read(dir.path().join("installation-link")).unwrap(),
        b"model"
    );
    // Group or world access without the runtime user's exact grant is not.
    for mode in [0o640, 0o644, 0o660, 0o400] {
        std::fs::write(&object, b"model").unwrap();
        set_mode(mode);
        assert!(
            super::inspect_trusted_final(&object, 5)
                .await
                .unwrap()
                .is_none()
        );
        assert!(!object.exists());
        assert_eq!(
            std::fs::read(dir.path().join("installation-link")).unwrap(),
            b"model"
        );
    }
    std::fs::write(&object, b"model").unwrap();
    // The exact runtime read grant an installation's start leaves behind is.
    let mut acl = 0x0002_u32.to_le_bytes().to_vec();
    for (tag, permissions, identifier) in [
        (0x0001_u16, 0o6_u16, u32::MAX),
        (0x0002, 0o4, 10_001),
        (0x0004, 0, u32::MAX),
        (0x0010, 0o4, u32::MAX),
        (0x0020, 0, u32::MAX),
    ] {
        acl.extend_from_slice(&tag.to_le_bytes());
        acl.extend_from_slice(&permissions.to_le_bytes());
        acl.extend_from_slice(&identifier.to_le_bytes());
    }
    set_mode(0o600);
    let file = std::fs::OpenOptions::new()
        .write(true)
        .open(&object)
        .unwrap();
    rustix::fs::fsetxattr(
        &file,
        "system.posix_acl_access",
        &acl,
        rustix::fs::XattrFlags::empty(),
    )
    .unwrap();
    assert_eq!(
        std::fs::metadata(&object).unwrap().permissions().mode() & 0o777,
        0o640
    );
    assert!(
        super::inspect_trusted_final(&object, 5)
            .await
            .unwrap()
            .is_some()
    );
}

#[tokio::test]
async fn direct_distribution_object_resumes_private_partial_atomically() {
    let model = b"small model object";
    let assignment = distribution_assignment_fixture(model);
    let mut objects = HashMap::new();
    objects.insert(hex_sha256(model), model.to_vec());
    let (client, server) = distribution_fixture_server(
        assignment.clone(),
        objects,
        1,
        DistributionFixtureMode::Good,
    );
    let root = tempfile::tempdir().unwrap();
    let destination = root.path().join("config.json");
    let partial = partial_path(&destination);
    std::fs::write(&partial, &model[..5]).unwrap();
    std::fs::set_permissions(&partial, std::fs::Permissions::from_mode(0o600)).unwrap();
    client
        .download_distribution_object(
            TEST_PLAN_DIGEST,
            &assignment.objects[0].sha256,
            model.len() as u64,
            &destination,
        )
        .await
        .unwrap();
    assert_eq!(std::fs::read(&destination).unwrap(), model);
    assert!(!partial.exists());
    assert_eq!(server.finish().unwrap().len(), 1);
}

#[tokio::test]
async fn distribution_isolates_partial_replacement_and_a_fresh_request_succeeds() {
    let model = b"small model object";
    let assignment = distribution_assignment_fixture(model);
    let mut objects = HashMap::new();
    objects.insert(hex_sha256(model), model.to_vec());
    let (client, server) = distribution_fixture_server(
        assignment.clone(),
        objects,
        2,
        DistributionFixtureMode::Good,
    );
    let root = tempfile::tempdir().unwrap();
    let destination = root.path().join("config.json");
    let partial = partial_path(&destination);
    let replacement = root.path().join("replacement");
    let mut corrupt = model.to_vec();
    corrupt[0] ^= 0xff;
    let mut swapped = false;
    let result = client
        .download_trusted_distribution_object_with_progress(
            TEST_PLAN_DIGEST,
            &assignment.objects[0].sha256,
            model.len() as u64,
            ObjectPlacement {
                destination: &destination,
                managed_root: root.path(),
                governor: &StreamGovernor::default(),
            },
            |_, phase| {
                if phase == ProgressPhase::Finalizing && !swapped {
                    std::fs::write(&replacement, &corrupt).unwrap();
                    std::fs::set_permissions(&replacement, std::fs::Permissions::from_mode(0o600))
                        .unwrap();
                    std::fs::rename(&replacement, &partial).unwrap();
                    swapped = true;
                }
            },
        )
        .await;
    assert!(result.is_err());
    assert!(!destination.exists());
    assert!(!partial.exists());
    client
        .download_distribution_object(
            TEST_PLAN_DIGEST,
            &assignment.objects[0].sha256,
            model.len() as u64,
            &destination,
        )
        .await
        .unwrap();
    assert_eq!(std::fs::read(&destination).unwrap(), model);
    assert_eq!(server.finish().unwrap().len(), 2);
}

#[tokio::test]
async fn distribution_reuses_an_existing_object_without_hashing_or_fetching() {
    // Name, size and private custody are the identity of an object already
    // in the managed cache. Same-size different bytes are neither hashed
    // nor re-transferred; the unreachable Controller proves no request.
    let model = b"small model object";
    let root = tempfile::tempdir().unwrap();
    let destination = root.path().join("config.json");
    let mut other = model.to_vec();
    other[0] ^= 0xff;
    std::fs::write(&destination, &other).unwrap();
    std::fs::set_permissions(&destination, std::fs::Permissions::from_mode(0o600)).unwrap();
    let client = AgentHttpClient {
        client: Arc::new(RwLock::new(reqwest::Client::new())),
        controller: Url::parse("http://127.0.0.1:1/").unwrap(),
        node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
        identity_content: Default::default(),
        progress_phase: Default::default(),
    };

    client
        .download_distribution_object(
            TEST_PLAN_DIGEST,
            &hex_sha256(model),
            model.len() as u64,
            &destination,
        )
        .await
        .unwrap();

    assert_eq!(std::fs::read(&destination).unwrap(), other);
}

#[tokio::test]
async fn direct_distribution_repairs_a_symlink_without_touching_its_target() {
    // The managed name is disposable; its external target must survive.
    let model = b"small model object";
    let assignment = distribution_assignment_fixture(model);
    let mut objects = HashMap::new();
    objects.insert(hex_sha256(model), model.to_vec());
    let (client, server) = distribution_fixture_server(
        assignment.clone(),
        objects,
        1,
        DistributionFixtureMode::Good,
    );
    let root = tempfile::tempdir().unwrap();
    let linked = root.path().join("linked");
    let mut corrupt = model.to_vec();
    corrupt[0] ^= 0xff;
    std::fs::write(&linked, &corrupt).unwrap();
    std::fs::set_permissions(&linked, std::fs::Permissions::from_mode(0o600)).unwrap();
    let destination = root.path().join("config.json");
    std::os::unix::fs::symlink(&linked, &destination).unwrap();

    client
        .download_distribution_object(
            TEST_PLAN_DIGEST,
            &assignment.objects[0].sha256,
            model.len() as u64,
            &destination,
        )
        .await
        .unwrap();
    assert_eq!(std::fs::read(&destination).unwrap(), model);
    // Fresh admission reuses the repaired entry without another transfer.
    client
        .download_distribution_object(
            TEST_PLAN_DIGEST,
            &assignment.objects[0].sha256,
            model.len() as u64,
            &destination,
        )
        .await
        .unwrap();
    assert_eq!(std::fs::read(&linked).unwrap(), corrupt);
    assert_eq!(server.finish().unwrap().len(), 1);
}

#[tokio::test]
async fn direct_distribution_retries_interrupted_body_from_appended_offset() {
    let model = b"small model object";
    let assignment = distribution_assignment_fixture(model);
    let mut objects = HashMap::new();
    objects.insert(hex_sha256(model), model.to_vec());
    let (client, server) = distribution_fixture_server(
        assignment.clone(),
        objects,
        2,
        DistributionFixtureMode::InterruptFirstObject,
    );
    let root = tempfile::tempdir().unwrap();
    let destination = root.path().join("image.tar");
    let mut updates = Vec::new();
    client
        .download_trusted_distribution_object_with_progress(
            TEST_PLAN_DIGEST,
            &hex_sha256(model),
            model.len() as u64,
            ObjectPlacement {
                destination: &destination,
                managed_root: root.path(),
                governor: &StreamGovernor::default(),
            },
            |bytes, phase| updates.push((bytes, phase)),
        )
        .await
        .unwrap();
    assert_eq!(std::fs::read(&destination).unwrap(), model);
    assert!(!partial_path(&destination).exists());
    let requests = server.finish().unwrap();
    assert!(
        String::from_utf8_lossy(&requests[1])
            .to_lowercase()
            .contains("range: bytes=5-")
    );
    assert!(updates.windows(2).all(|pair| pair[0].0 <= pair[1].0));
    assert_eq!(
        updates.last(),
        Some(&(model.len() as u64, ProgressPhase::Finalizing))
    );
}

#[tokio::test]
async fn direct_distribution_retries_service_unavailability_but_not_bad_identity() {
    let model = b"small model object";
    let assignment = distribution_assignment_fixture(model);
    for (mode, request_count, succeeds) in [
        (DistributionFixtureMode::UnavailableFirstObject, 2, true),
        (DistributionFixtureMode::WrongEtagFirstObject, 1, false),
    ] {
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        let (client, server) =
            distribution_fixture_server(assignment.clone(), objects, request_count, mode);
        let root = tempfile::tempdir().unwrap();
        let result = client
            .download_distribution_object(
                TEST_PLAN_DIGEST,
                &hex_sha256(model),
                model.len() as u64,
                &root.path().join("image.tar"),
            )
            .await;
        if succeeds {
            result.unwrap();
        } else {
            assert!(matches!(result, Err(ClientError::Protocol)));
        }
        assert_eq!(server.finish().unwrap().len(), request_count);
    }
}

#[test]
fn trusted_partial_paths_keep_same_stem_objects_distinct() {
    let json = Path::new("/tmp/config.json");
    let yaml = Path::new("/tmp/config.yaml");
    assert_ne!(partial_path(json), partial_path(yaml));
    assert_eq!(
        partial_path(json),
        PathBuf::from("/tmp/config.json.partial")
    );
}
