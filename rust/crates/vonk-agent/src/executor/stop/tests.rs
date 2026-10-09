#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn damaged_history_never_prevents_exact_stop_from_reaching_current_authority() {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::os::unix::fs::PermissionsExt;
    let mut claim: AgentClaim = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    )).unwrap();
    let AgentClaimPayload::RecipeJobRunRequest(job) = &claim.payload else {
        panic!("job fixture");
    };
    let request = exact_stop_plan_from_claim(&claim, &job.run_id.to_string(), true).unwrap();
    claim.operation = AgentOperation::RecipeStop;
    claim.payload = AgentClaimPayload::RecipeStopPayload(request.clone());
    claim.deadline = (Utc::now() + chrono::Duration::minutes(2)).fixed_offset();
    let data = tempdir().unwrap();
    let runtime_root = tempdir().unwrap();
    let history = data
        .path()
        .join("run-metadata")
        .join(request.run_id.to_string());
    fs::create_dir_all(&history).unwrap();
    fs::set_permissions(&history, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(history.join("lifecycle.json"), b"damaged history").unwrap();
    let installation = data
        .path()
        .join("installations")
        .join(request.installation_id.to_string());
    fs::create_dir_all(&installation).unwrap();
    fs::write(installation.join("spec.json"), b"damaged plan").unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    let peer = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut paths = Vec::new();
        for _ in 0..2 {
            let mut stream = loop {
                assert!(Instant::now() < deadline);
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(1))
                    }
                    Err(error) => panic!("stop peer: {error}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut bytes = Vec::new();
            let mut buffer = [0_u8; 4096];
            loop {
                let read = stream.read(&mut buffer).unwrap();
                assert!(read > 0);
                bytes.extend_from_slice(&buffer[..read]);
                if let Some(end) = bytes.windows(4).position(|part| part == b"\r\n\r\n") {
                    let headers = std::str::from_utf8(&bytes[..end]).unwrap();
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().unwrap())
                        })
                        .unwrap();
                    if bytes.len() >= end + 4 + length {
                        paths.push(headers.lines().next().unwrap().to_owned());
                        break;
                    }
                }
            }
            // Actual denied authority stays an effect boundary. The assertion
            // is that local history never prevents asking the current owner.
            stream
                .write_all(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .unwrap();
        }
        paths
    });
    let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: runtime_root.path(),
    };
    let (_cancel, cancellation) = tokio::sync::watch::channel(false);
    assert!(
        executor
            .runtime
            .prepare_stop(&request.run_id.to_string())
            .is_err()
    );
    for _ in 0..2 {
        claim.fence = Uuid::new_v4();
        let result = executor
            .execute_stop(&claim, cancellation.clone(), request.clone())
            .await;
        assert!(matches!(result, ExecutionResult::Unknown(_)));
    }
    assert_eq!(
        peer.join().unwrap(),
        vec!["POST /agent/host-runtime/grant HTTP/1.1"; 2]
    );
    assert_eq!(
        fs::read(installation.join("spec.json")).unwrap(),
        b"damaged plan"
    );
}
