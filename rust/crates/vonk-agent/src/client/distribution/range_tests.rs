#![cfg(test)]
//! One object moves in several ranges at once, and its checkpoint names the
//! ranges that landed.
//!
//! Wrong implementations caught: ranges of one object fetched one after
//! another (a large file limited to one stream's rate), a resume that
//! refetches recorded ranges or trusts a gap, and a checkpoint of an earlier
//! agent that is not adopted as its prefix.

use super::super::test_support::*;
use super::*;
use crate::stream_governor::INITIAL_STREAMS;
use std::io::{Read, Write};
use std::sync::Condvar;

const BODY: &[u8] = b"forty bytes of model weights, five range";
const RANGE: u64 = 8;

#[derive(Default)]
struct Served {
    ranges: Vec<(u64, u64)>,
    in_flight: usize,
    peak: usize,
}

/// Serve ranges of [`BODY`], one connection per request. The first `hold`
/// requests are answered only once all of them are open (or after a few
/// seconds, which a test then sees in `peak`); request number `refuse` (from
/// 1) is answered with another object's ETag.
fn range_server(hold: usize, refuse: Option<usize>) -> (String, Arc<(Mutex<Served>, Condvar)>) {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let served = Arc::new((Mutex::new(Served::default()), Condvar::new()));
    let shared = served.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            let served = shared.clone();
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
                let head = String::from_utf8_lossy(&head).to_ascii_lowercase();
                let (start, end) = head
                    .lines()
                    .find_map(|line| line.strip_prefix("range: bytes="))
                    .and_then(|range| range.split_once('-'))
                    .map(|(start, end)| {
                        (start.parse::<u64>().unwrap(), end.parse::<u64>().unwrap())
                    })
                    .unwrap();
                let (lock, opened) = &*served;
                let number = {
                    let mut state = lock.lock().unwrap();
                    state.ranges.push((start, end));
                    state.in_flight += 1;
                    state.peak = state.peak.max(state.in_flight);
                    opened.notify_all();
                    let number = state.ranges.len();
                    let (state, _) = opened
                        .wait_timeout_while(state, Duration::from_secs(3), |state| {
                            number <= hold && state.ranges.len() < hold
                        })
                        .unwrap();
                    drop(state);
                    number
                };
                let etag = if refuse == Some(number) {
                    "0".repeat(64)
                } else {
                    hex_sha256(BODY)
                };
                let body = &BODY[start as usize..=end as usize];
                let _ = write!(
                    stream,
                    "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\n\
                     Content-Range: bytes {start}-{end}/{}\r\nETag: \"sha256:{etag}\"\r\n\
                     Connection: close\r\n\r\n",
                    body.len(),
                    BODY.len(),
                );
                let _ = stream.write_all(body).and_then(|()| stream.flush());
                lock.lock().unwrap().in_flight -= 1;
            });
        }
    });
    (format!("http://{address}/"), served)
}

async fn download(url: &str, root: &Path, governor: &StreamGovernor) -> Result<(), ClientError> {
    AgentHttpClient::for_http_test(url, TEST_NODE_ID)
        .with_distribution_range_bytes(RANGE)
        .download_trusted_distribution_object_with_progress(
            TEST_PLAN_DIGEST,
            &hex_sha256(BODY),
            BODY.len() as u64,
            ObjectPlacement {
                destination: &root.join("object"),
                managed_root: root,
                governor,
            },
            |_, _| {},
        )
        .await
}

fn ranges(served: &Arc<(Mutex<Served>, Condvar)>) -> Vec<(u64, u64)> {
    let mut ranges = served.0.lock().unwrap().ranges.clone();
    ranges.sort_unstable();
    ranges
}

fn write_private(path: &Path, bytes: &[u8]) {
    std::fs::write(path, bytes).unwrap();
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
}

#[tokio::test]
async fn a_single_large_object_keeps_the_governors_streams_in_flight() {
    // The server answers none of the first four ranges until all four are
    // open, so ranges fetched one after another cannot reach the peak.
    let (url, served) = range_server(INITIAL_STREAMS, None);
    let root = tempfile::tempdir().unwrap();
    download(&url, root.path(), &StreamGovernor::default())
        .await
        .unwrap();
    assert_eq!(std::fs::read(root.path().join("object")).unwrap(), BODY);
    assert!(!partial_path(&root.path().join("object")).exists());
    assert!(served.0.lock().unwrap().peak >= INITIAL_STREAMS);
    assert_eq!(
        ranges(&served),
        [(0, 7), (8, 15), (16, 23), (24, 31), (32, 39)]
    );
}

#[tokio::test]
async fn an_interrupted_object_resumes_only_its_unrecorded_ranges() {
    let root = tempfile::tempdir().unwrap();
    let partial = partial_path(&root.path().join("object"));
    // One stream: two ranges land and are recorded, the third is refused.
    let (url, first) = range_server(0, Some(3));
    let one_stream = StreamGovernor::new(1, 1);
    assert!(matches!(
        download(&url, root.path(), &one_stream).await,
        Err(ClientError::Protocol)
    ));
    let landed = first.0.lock().unwrap().ranges[..2].to_vec();
    // The checkpoint is longer than the object: an earlier agent discards
    // it rather than reading its length as a prefix.
    assert!(std::fs::metadata(&partial).unwrap().len() > BODY.len() as u64);

    let (url, second) = range_server(0, None);
    download(&url, root.path(), &StreamGovernor::default())
        .await
        .unwrap();
    assert_eq!(std::fs::read(root.path().join("object")).unwrap(), BODY);
    let fetched = ranges(&second);
    assert_eq!(fetched.len(), 3);
    assert!(landed.iter().all(|range| !fetched.contains(range)));
}

#[tokio::test]
async fn an_earlier_agents_prefix_is_adopted_and_only_the_rest_is_fetched() {
    let root = tempfile::tempdir().unwrap();
    write_private(&partial_path(&root.path().join("object")), &BODY[..13]);
    let (url, served) = range_server(0, None);
    download(&url, root.path(), &StreamGovernor::default())
        .await
        .unwrap();
    assert_eq!(std::fs::read(root.path().join("object")).unwrap(), BODY);
    assert_eq!(ranges(&served), [(13, 15), (16, 23), (24, 31), (32, 39)]);
}

#[tokio::test]
async fn a_checkpoint_of_record_length_without_a_record_starts_over() {
    let root = tempfile::tempdir().unwrap();
    // The length a record of five ranges has, but no record in it.
    write_private(
        &partial_path(&root.path().join("object")),
        &vec![0; BODY.len() + 17],
    );
    let (url, served) = range_server(0, None);
    download(&url, root.path(), &StreamGovernor::default())
        .await
        .unwrap();
    assert_eq!(std::fs::read(root.path().join("object")).unwrap(), BODY);
    assert_eq!(
        ranges(&served),
        [(0, 7), (8, 15), (16, 23), (24, 31), (32, 39)]
    );
}

#[tokio::test]
async fn a_last_range_continues_the_prefix_without_a_record() {
    let root = tempfile::tempdir().unwrap();
    let partial = partial_path(&root.path().join("object"));
    write_private(&partial, &BODY[..33]);
    // Only the last range is left: it is written in order, so the length
    // stays the checkpoint an interrupted request resumes from exactly.
    let (url, served) = range_server(0, Some(1));
    assert!(matches!(
        download(&url, root.path(), &StreamGovernor::default()).await,
        Err(ClientError::Protocol)
    ));
    assert_eq!(ranges(&served), [(33, 39)]);
    assert_eq!(std::fs::read(&partial).unwrap(), &BODY[..33]);
}
