#![cfg(test)]
use super::*;

const LOOPBACK: IpAddr = crate::client::test_support::FIXTURE_IP;

#[test]
fn the_captured_tensorfold_metrics_give_the_vector_reading() {
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/telemetry-sample-v2.json"
    ))
    .unwrap();
    let body = vector["captures"]["tensorfold_metrics"].as_str().unwrap();
    let expected = &vector["sample"]["engines"][0];
    let run_id = expected["run_id"].as_str().unwrap().parse().unwrap();
    let reading = parse_engine(body, run_id).unwrap();
    assert_eq!(serde_json::to_value(&reading).unwrap(), *expected);
    // A body in a format without a name table gives no reading.
    assert!(parse_engine("sglang:num_running_reqs 1\n", run_id).is_none());
}

#[test]
fn the_vllm_and_gr8_samples_give_the_vector_readings() {
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/engine-metrics-v1.json"
    ))
    .unwrap();
    for sample in vector["samples"].as_array().unwrap() {
        let expected = &sample["expected"];
        let run_id = sample["run_id"].as_str().unwrap().parse().unwrap();
        let reading = parse_engine(sample["metrics"].as_str().unwrap(), run_id).unwrap();
        let mut actual = serde_json::to_value(&reading).unwrap();
        actual
            .as_object_mut()
            .unwrap()
            .retain(|_, value| !value.is_null());
        assert_eq!(actual, *expected, "{}", sample["name"]);
    }
}

#[test]
fn a_sum_that_overflows_is_absent_not_infinite() {
    let body = "vllm:e2e_request_latency_seconds_sum 1e308\n\
                    vllm:e2e_request_latency_seconds_sum 1e308\n\
                    vllm:e2e_request_latency_seconds_sum -1e308\n\
                    vllm:e2e_request_latency_seconds_count 3\n";
    let reading = parse_engine(body, Uuid::nil()).unwrap();
    assert_eq!(reading.e2e_seconds_sum, None);
    assert_eq!(reading.e2e_seconds_count, Some(3));
}

async fn serve(body: Option<String>) -> u16 {
    let listener = tokio::net::TcpListener::bind(crate::client::test_support::FIXTURE_BIND)
        .await
        .unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        for _ in 0..16 {
            let (mut socket, _) = listener.accept().await.unwrap();
            let body = body.clone();
            tokio::spawn(async move {
                let mut request = [0_u8; 1024];
                #[expect(
                    clippy::let_underscore_must_use,
                    reason = "waits for a task to end; its outcome is observed elsewhere"
                )]
                let _ = socket.read(&mut request).await;
                // No body means an engine that accepts and never answers.
                let Some(body) = body else {
                    return std::future::pending::<()>().await;
                };
                let head = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n", body.len());
                #[expect(
                    clippy::let_underscore_must_use,
                    reason = "waits for a task to end; its outcome is observed elsewhere"
                )]
                let _ = socket.write_all(head.as_bytes()).await;
                #[expect(
                    clippy::let_underscore_must_use,
                    reason = "waits for a task to end; its outcome is observed elsewhere"
                )]
                let _ = socket.write_all(body.as_bytes()).await;
            });
        }
    });
    port
}

#[tokio::test]
async fn a_hung_or_oversized_engine_is_omitted_and_the_others_answer() {
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/telemetry-sample-v2.json"
    ))
    .unwrap();
    let good = serve(Some(
        vector["captures"]["tensorfold_metrics"]
            .as_str()
            .unwrap()
            .to_owned(),
    ))
    .await;
    let hung = serve(None).await;
    let oversized = serve(Some(format!(
        "tensorfold:requests_running 1\n{}",
        "#".repeat(MAX_BODY_BYTES)
    )))
    .await;
    let directory = tempfile::tempdir().unwrap();
    for (id, port) in [(1, good), (2, hung), (3, oversized)] {
        write_target(directory.path(), Uuid::from_u128(id), LOOPBACK, port).unwrap();
    }
    // A damaged file and a missing directory are no targets, not errors.
    std::fs::write(directory.path().join("damaged.json"), "{").unwrap();
    let readings = scrape_engines(directory.path(), SCRAPE_DEADLINE).await;
    assert_eq!(readings.len(), 1);
    assert_eq!(readings[0].run_id, Uuid::from_u128(1));
    assert_eq!(readings[0].prompt_tokens, Some(8_491_903));
    assert!(
        scrape_engines(&directory.path().join("missing"), SCRAPE_DEADLINE)
            .await
            .is_empty(),
        "unexpected content"
    );
}
