#![cfg(test)]
//! A transfer ends on inactivity, never on total elapsed time; a wait for a
//! stream slot is waiting and starts no clock.
//!
//! Wrong implementations caught: a per-range or per-object total (a slow
//! range fails while still moving), a slot wait counted against the range,
//! and a silent body ending without naming its inactivity window.

use super::super::test_support::*;
use super::*;
use crate::stall_window::Stalled;
use std::io::{Read, Write};

const WINDOW: Duration = Duration::from_millis(400);

/// Serve each ranged request whole, split into `chunks` parts: write the first
/// `sent` of them one `pause` apart, then hold the connection open in silence.
fn dripping_server(body: &'static [u8], chunks: usize, sent: usize, pause: Duration) -> String {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            std::thread::spawn(move || {
                let deadline = std::time::Instant::now() + Duration::from_secs(5);
                let mut head = Vec::new();
                let mut byte = [0_u8; 1];
                while !head.ends_with(b"\r\n\r\n") && std::time::Instant::now() < deadline {
                    if stream.read(&mut byte).unwrap_or(0) != 1 {
                        return;
                    }
                    head.push(byte[0]);
                }
                let length = body.len();
                let _ = write!(
                    stream,
                    "HTTP/1.1 206 Partial Content\r\nContent-Length: {length}\r\n\
                     Content-Range: bytes 0-{}/{length}\r\nETag: \"sha256:{}\"\r\n\r\n",
                    length - 1,
                    hex_sha256(body),
                );
                for part in body.chunks(length.div_ceil(chunks)).take(sent) {
                    std::thread::sleep(pause);
                    if stream
                        .write_all(part)
                        .and_then(|()| stream.flush())
                        .is_err()
                    {
                        return;
                    }
                }
                // Silence: the connection stays open until the client leaves.
                let _ = stream.read(&mut [0_u8; 1]);
            });
        }
    });
    format!("http://{address}/")
}

fn client(url: &str) -> AgentHttpClient {
    AgentHttpClient::for_http_test(url, TEST_NODE_ID).with_transfer_inactivity(WINDOW)
}

async fn download(
    client: &AgentHttpClient,
    body: &[u8],
    root: &Path,
    governor: &StreamGovernor,
) -> Result<(), ClientError> {
    client
        .download_trusted_distribution_object_with_progress(
            TEST_PLAN_DIGEST,
            &hex_sha256(body),
            body.len() as u64,
            ObjectPlacement {
                destination: &root.join("object"),
                managed_root: root,
                governor,
            },
            |_, _| {},
        )
        .await
}

#[tokio::test]
async fn a_range_that_keeps_dripping_completes_however_long_it_takes() {
    const BODY: &[u8] = b"weights that arrive a few bytes at a time over many windows";
    // Twelve chunks, each well inside the window: the range takes three
    // whole windows and never stalls.
    let url = dripping_server(BODY, 12, 12, WINDOW / 4);
    let root = tempfile::tempdir().unwrap();
    let started = tokio::time::Instant::now();
    download(&client(&url), BODY, root.path(), &StreamGovernor::default())
        .await
        .unwrap();
    assert!(started.elapsed() > WINDOW * 2);
    assert_eq!(std::fs::read(root.path().join("object")).unwrap(), BODY);
}

#[tokio::test]
async fn a_body_silent_for_a_window_ends_with_its_inactivity_evidence() {
    const BODY: &[u8] = b"three chunks arrive, then the body goes silent";
    let url = dripping_server(BODY, 6, 3, WINDOW / 4);
    let client = client(&url);
    let inactivity = Inactivity::new(WINDOW);
    let mut response = send_streaming(
        client.current_client().await.unwrap().get(&url),
        &inactivity,
    )
    .await
    .unwrap();
    let mut received = 0_u64;
    let error = loop {
        match next_chunk(response.chunk(), &inactivity).await {
            Ok(Some(chunk)) => received += chunk.len() as u64,
            Ok(None) => panic!("the body never completes"),
            Err(error) => break error,
        }
    };
    // The evidence names the inactivity window and the bytes that did move.
    assert_eq!(
        error.stalled(),
        Some(&Stalled {
            window: WINDOW,
            bytes: received
        })
    );
    assert!(received > 0 && received < BODY.len() as u64);
    assert!(error.retryable());
}

#[tokio::test]
async fn a_slot_wait_longer_than_the_window_does_not_fail_the_range() {
    const BODY: &[u8] = b"a range that waits for its stream slot first";
    let url = dripping_server(BODY, 1, 1, Duration::ZERO);
    let root = tempfile::tempdir().unwrap();
    let governor = StreamGovernor::new(1, 1);
    let held = governor.acquire().await;
    let client = client(&url);
    let transfer = download(&client, BODY, root.path(), &governor);
    tokio::pin!(transfer);
    // The only slot stays taken for several windows: the range waits.
    assert!(
        tokio::time::timeout(WINDOW * 4, &mut transfer)
            .await
            .is_err()
    );
    drop(held);
    transfer.await.unwrap();
    assert_eq!(std::fs::read(root.path().join("object")).unwrap(), BODY);
}
