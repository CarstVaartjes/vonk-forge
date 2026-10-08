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
