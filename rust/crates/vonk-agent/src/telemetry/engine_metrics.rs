//! Engine readings. The agent leaves one small target file per ready run in its
//! runtime directory; the monitor, which cannot read the agent's database,
//! scrapes each target's Prometheus `/metrics` on the Spark's own address and turns the
//! families it recognises into an `EngineReading`. A run without a fresh
//! target, a slow or oversized reply, or an unknown metric format simply has
//! no reading in that sample.

use std::{
    net::{IpAddr, SocketAddr},
    path::Path,
    time::{Duration, SystemTime},
};

use futures_util::{StreamExt, stream};
use uuid::Uuid;
use vonk_agent_protocol::generated::{EngineFormat, EngineReading, EngineTarget};

use super::read_bounded_text;

/// The agent's runtime directory, shared read-only with the monitor.
pub const RUNTIME_DIRECTORY: &str = "/run/vonk-forge-agent";
/// The directory below the agent's runtime directory that holds the targets.
pub const TARGETS_DIRECTORY: &str = "engine-targets";
/// One total deadline for all engines of a sample.
pub const SCRAPE_DEADLINE: Duration = Duration::from_millis(750);
const MAX_BODY_BYTES: usize = 2 * 1024 * 1024;
const SCRAPE_CONCURRENCY: usize = 8;
/// The agent rewrites a ready run's target on every observation (seconds, not
/// minutes); an older file belongs to a run nobody observes any more.
const TARGET_MAX_AGE: Duration = Duration::from_secs(120);

/// Publish where a ready run listens (the address its readiness probe uses). Atomic, so the monitor never reads half a file.
pub fn write_target(
    directory: &Path,
    run_id: Uuid,
    address: IpAddr,
    port: u16,
) -> std::io::Result<()> {
    std::fs::create_dir_all(directory)?;
    let body = serde_json::to_vec(&EngineTarget {
        run_id,
        address: address.to_string(),
        port,
    })
    .map_err(|error| std::io::Error::other(error.to_string()))?;
    let temporary = tempfile::NamedTempFile::new_in(directory)?;
    std::fs::write(temporary.path(), body)?;
    temporary
        .persist(directory.join(format!("{run_id}.json")))
        .map_err(|error| error.error)?;
    Ok(())
}

/// A run that is not ready (or not running) has no target.
pub fn remove_target(directory: &Path, run_id: Uuid) {
    let _ = std::fs::remove_file(directory.join(format!("{run_id}.json")));
}

fn fresh_targets(directory: &Path) -> Vec<EngineTarget> {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter(|entry| {
            entry
                .metadata()
                .and_then(|meta| meta.modified())
                .ok()
                .and_then(|modified| SystemTime::now().duration_since(modified).ok())
                .is_some_and(|age| age <= TARGET_MAX_AGE)
        })
        .filter_map(|entry| {
            let text = read_bounded_text(&entry.path())?;
            vonk_agent_protocol::parse_strict::<EngineTarget>(text.as_bytes()).ok()
        })
        .collect()
}

/// Scrape every fresh target at once. Whatever has answered when the deadline
/// passes is returned; the rest is omitted from this sample.
pub async fn scrape_engines(directory: &Path, deadline: Duration) -> Vec<EngineReading> {
    let targets = fresh_targets(directory);
    if targets.is_empty() {
        return Vec::new();
    }
    let Ok(client) = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
    else {
        return Vec::new();
    };
    let client = &client;
    let mut pending = stream::iter(targets)
        .map(|target| async move {
            let address = target.address.parse::<IpAddr>().ok()?;
            let body = fetch(client, SocketAddr::new(address, target.port)).await?;
            parse_engine(&body, target.run_id)
        })
        .buffer_unordered(SCRAPE_CONCURRENCY);
    let mut readings = Vec::new();
    let deadline = tokio::time::sleep(deadline);
    tokio::pin!(deadline);
    loop {
        tokio::select! {
            next = pending.next() => match next {
                Some(reading) => readings.extend(reading),
                None => break,
            },
            () = &mut deadline => break,
        }
    }
    readings
}

/// The body of `GET /metrics`, if the engine answers 200 within the size cap.
async fn fetch(client: &reqwest::Client, endpoint: SocketAddr) -> Option<String> {
    let response = client
        .get(format!("http://{endpoint}/metrics"))
        .send()
        .await
        .ok()?;
    if response.status() != reqwest::StatusCode::OK {
        return None;
    }
    let mut body = Vec::new();
    let mut chunks = response.bytes_stream();
    while let Some(chunk) = chunks.next().await {
        body.extend_from_slice(&chunk.ok()?);
        if body.len() > MAX_BODY_BYTES {
            return None;
        }
    }
    String::from_utf8(body).ok()
}

/// Recognise the format by the metric-name prefix and read the families whose
/// names a verbatim capture proved. Other prefixes have no reading yet.
pub fn parse_engine(body: &str, run_id: Uuid) -> Option<EngineReading> {
    let format = if body.lines().any(|line| line.starts_with("tensorfold:")) {
        EngineFormat::Tensorfold
    } else {
        return None;
    };
    let mut reading = EngineReading {
        run_id,
        format,
        prompt_tokens: None,
        generation_tokens: None,
        cached_prompt_tokens: None,
        preemptions: None,
        spec_decode_draft_tokens: None,
        spec_decode_accepted_tokens: None,
        requests_running: None,
        requests_waiting: None,
        kv_cache_usage_ratio: None,
        ttft_seconds_sum: None,
        ttft_seconds_count: None,
        time_per_output_token_seconds_sum: None,
        time_per_output_token_seconds_count: None,
        prefill_seconds_sum: None,
        prefill_seconds_count: None,
        decode_seconds_sum: None,
        decode_seconds_count: None,
        e2e_seconds_sum: None,
        e2e_seconds_count: None,
    };
    for line in body.lines().filter(|line| !line.starts_with('#')) {
        let Some((name, value)) = split_sample(line) else {
            continue;
        };
        add_tensorfold(&mut reading, name, value);
    }
    Some(reading)
}

/// `name{labels} value` (labels dropped) into the family name and its number.
fn split_sample(line: &str) -> Option<(&str, f64)> {
    let (series, value) = line.trim().rsplit_once(' ')?;
    let name = series.split('{').next()?;
    let value = value
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite())?;
    Some((name, value))
}

/// Sum across label sets, as a counter or gauge of the whole engine; the KV
/// ratio is per pool, so the fullest pool stands for the engine.
fn add_tensorfold(reading: &mut EngineReading, name: &str, value: f64) {
    let Some(name) = name.strip_prefix("tensorfold:") else {
        return;
    };
    let count = || {
        (0.0..=i64::MAX as f64)
            .contains(&value)
            .then_some(value as u64)
    };
    fn add(slot: &mut Option<u64>, value: Option<u64>) {
        if let Some(value) = value {
            *slot = Some(slot.unwrap_or(0).saturating_add(value));
        }
    }
    fn add_seconds(slot: &mut Option<f64>, value: f64) {
        *slot = Some(slot.unwrap_or(0.0) + value);
    }
    match name {
        "prompt_tokens_total" => add(&mut reading.prompt_tokens, count()),
        "generation_tokens_total" => add(&mut reading.generation_tokens, count()),
        "prompt_tokens_cached_total" => add(&mut reading.cached_prompt_tokens, count()),
        "preemptions_total" => add(&mut reading.preemptions, count()),
        "spec_decode_num_draft_tokens_total" => add(&mut reading.spec_decode_draft_tokens, count()),
        "spec_decode_num_accepted_tokens_total" => {
            add(&mut reading.spec_decode_accepted_tokens, count())
        }
        "requests_running" => add(&mut reading.requests_running, count()),
        "requests_waiting" => add(&mut reading.requests_waiting, count()),
        "kv_cache_usage_ratio" if (0.0..=1.0).contains(&value) => {
            reading.kv_cache_usage_ratio =
                Some(reading.kv_cache_usage_ratio.unwrap_or(0.0).max(value))
        }
        "time_to_first_token_seconds_sum" => add_seconds(&mut reading.ttft_seconds_sum, value),
        "time_to_first_token_seconds_count" => add(&mut reading.ttft_seconds_count, count()),
        "request_time_per_output_token_seconds_sum" => {
            add_seconds(&mut reading.time_per_output_token_seconds_sum, value)
        }
        "request_time_per_output_token_seconds_count" => {
            add(&mut reading.time_per_output_token_seconds_count, count())
        }
        "request_prefill_seconds_sum" => add_seconds(&mut reading.prefill_seconds_sum, value),
        "request_prefill_seconds_count" => add(&mut reading.prefill_seconds_count, count()),
        "request_decode_seconds_sum" => add_seconds(&mut reading.decode_seconds_sum, value),
        "request_decode_seconds_count" => add(&mut reading.decode_seconds_count, count()),
        "e2e_request_latency_seconds_sum" => add_seconds(&mut reading.e2e_seconds_sum, value),
        "e2e_request_latency_seconds_count" => add(&mut reading.e2e_seconds_count, count()),
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const LOOPBACK: IpAddr = IpAddr::V4(std::net::Ipv4Addr::LOCALHOST);

    #[test]
    fn the_captured_tensorfold_metrics_give_the_vector_reading() {
        let vector: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/telemetry-sample-v2.json"
        ))
        .unwrap();
        let body = vector["captures"]["tensorfold_metrics"].as_str().unwrap();
        let expected = &vector["sample"]["engines"][0];
        let run_id = expected["run_id"].as_str().unwrap().parse().unwrap();
        let reading = parse_engine(body, run_id).unwrap();
        assert_eq!(serde_json::to_value(&reading).unwrap(), *expected);
        // A body in another (not yet captured) format gives no reading.
        assert!(parse_engine("vllm:num_requests_running 1\n", run_id).is_none());
    }

    async fn serve(body: Option<String>) -> u16 {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            loop {
                let (mut socket, _) = listener.accept().await.unwrap();
                let body = body.clone();
                tokio::spawn(async move {
                    let mut request = [0_u8; 1024];
                    let _ = socket.read(&mut request).await;
                    // No body means an engine that accepts and never answers.
                    let Some(body) = body else {
                        return std::future::pending::<()>().await;
                    };
                    let head = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n", body.len());
                    let _ = socket.write_all(head.as_bytes()).await;
                    let _ = socket.write_all(body.as_bytes()).await;
                });
            }
        });
        port
    }

    #[tokio::test]
    async fn a_hung_or_oversized_engine_is_omitted_and_the_others_answer() {
        let vector: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/telemetry-sample-v2.json"
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
                .is_empty()
        );
    }
}
