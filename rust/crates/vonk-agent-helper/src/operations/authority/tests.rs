#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn a_request_document_between_the_retired_private_cap_and_the_exchange_ceiling_is_read() {
    // Wrong implementation: the helper read the request file under a
    // private `MAX_RUNTIME_REQUEST_BYTES = 64 * 1024` round number, so a
    // legitimate many-mount command line the plan admits -- and the agent
    // admitted under the same byte budget -- was refused as
    // `helper.unsafe_path` after a successful install, blaming the path
    // rather than the bound.
    let temp = tempfile::tempdir().unwrap();
    let requests = temp.path().join("runtime-requests");
    fs::create_dir_all(&requests).unwrap();
    let request = HostRuntimeRequest {
        action: HostRuntimeAction::ImagePull,
        fence: uuid::Uuid::new_v4(),
        arguments: (0..3000)
            .map(|index| format!("--mount=type=bind,src=/run/vonk/models/{index:05}"))
            .collect(),
        installation_id: None,
        reconciliation_identity: None,
        job_plan: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    };
    let body = vonk_agent_protocol::canonical_json(&request).unwrap();
    assert!(
        body.len() > 64 * 1024,
        "this document must exceed the retired private cap, got {} bytes",
        body.len()
    );
    assert!(body.len() <= vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES);
    let digest = hex_sha256(&body);
    let path = requests.join(format!("{digest}.json"));
    fs::write(&path, &body).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();

    let executor = OperationExecutor::new(
        ManagedRoots::under(temp.path()).with_runtime_requests(&requests),
        &[0; 32],
        MissingContainerRunner,
        None,
    )
    .unwrap();
    let read = executor
        .read_runtime_request(&digest)
        .expect("a request inside the exchange ceiling must be read");
    assert_eq!(read.arguments.len(), 3000);
}
