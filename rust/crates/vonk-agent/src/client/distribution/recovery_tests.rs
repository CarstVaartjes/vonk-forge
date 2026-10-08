#![cfg(test)]

use super::super::test_support::*;
use super::*;

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
async fn malformed_delivery_is_refetched_before_effects_and_fresh_requests_are_admitted() {
    for (mode, first_succeeds, requests) in [
        (DistributionFixtureMode::MalformedFirstManifest, true, 4),
        (
            DistributionFixtureMode::MalformedFirstThreeManifests,
            false,
            5,
        ),
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
        let result = client
            .download_distribution(TEST_PLAN_DIGEST, root.path())
            .await;
        if first_succeeds {
            result.unwrap();
            assert_eq!(std::fs::read(&destination).unwrap(), model);
        } else {
            assert!(result.is_err());
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
async fn repeated_truncation_ends_with_resumable_bytes_and_a_fresh_request_finishes() {
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
    assert!(
        client
            .download_distribution_object(
                TEST_PLAN_DIGEST,
                &hex_sha256(model),
                model.len() as u64,
                &destination
            )
            .await
            .is_err()
    );
    assert!(!destination.exists());
    let partial = std::fs::read(partial_path(&destination)).unwrap();
    assert!(!partial.is_empty());
    assert!(model.starts_with(&partial));
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
