//! The Controller's runtime image store, served to the local Docker daemon.
//!
//! Runtime images live in one content-addressed OCI layout on the Controller
//! and are read through the agent site's digest-only `/v2/` routes, which
//! require this agent's mTLS identity. Docker cannot present that identity,
//! so for the duration of one pull the agent serves those same routes on a
//! loopback port (Docker treats loopback registries as plain HTTP) and
//! forwards each request with its own authenticated, rotating client. Docker
//! then fetches only the layers it does not already hold and verifies every
//! blob against the pinned manifest while pulling.

use std::future::Future;

use futures_util::{StreamExt, stream::BoxStream, stream::FuturesUnordered};
use reqwest::header::HeaderMap;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

use crate::client::{AgentHttpClient, ClientError, TRANSFER_INACTIVITY};
use crate::stall_window::{Inactivity, Stalled};

/// Receives the bytes of each pulled blob as they reach Docker.
pub type PullProgress<'a> = &'a (dyn Fn(u64) + Sync);

/// Why the store could not serve one of Docker's reads. Docker only ever sees
/// a 502 or a cut body, so the pull keeps the latest cause for its evidence.
#[derive(Debug, thiserror::Error)]
pub enum StoreFailure {
    #[error("image store upstream refused {path}: {error}")]
    Upstream { path: String, error: ClientError },
    #[error("image store read ended: {0}")]
    Transfer(#[from] std::io::Error),
}

/// The latest store failure of one pull, shared by its connections.
type FailureSlot = std::sync::Mutex<Option<StoreFailure>>;

/// One upstream answer: status, headers and a streamed body.
pub struct StoreResponse {
    pub status: reqwest::StatusCode,
    pub headers: HeaderMap,
    pub body: BoxStream<'static, std::io::Result<Vec<u8>>>,
}

/// Where the loopback store reads from: the Controller, through the agent's
/// authenticated client.
#[async_trait::async_trait]
pub trait StoreUpstream: Sync {
    async fn get(
        &self,
        head: bool,
        path: &str,
        accept: Option<&str>,
        range: Option<&str>,
    ) -> Result<StoreResponse, ClientError>;
}

#[async_trait::async_trait]
impl StoreUpstream for AgentHttpClient {
    async fn get(
        &self,
        head: bool,
        path: &str,
        accept: Option<&str>,
        range: Option<&str>,
    ) -> Result<StoreResponse, ClientError> {
        let response = self.image_store_request(head, path, accept, range).await?;
        Ok(StoreResponse {
            status: response.status(),
            headers: response.headers().clone(),
            body: response
                .bytes_stream()
                .map(|chunk| {
                    chunk
                        .map(|bytes| bytes.to_vec())
                        .map_err(std::io::Error::other)
                })
                .boxed(),
        })
    }
}

/// Docker's request heads are a few hundred bytes.
const MAX_REQUEST_HEAD: usize = 16 * 1024;
/// Response headers a registry client needs; everything else is dropped.
const FORWARDED_HEADERS: [&str; 7] = [
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "docker-content-digest",
    "docker-distribution-api-version",
    "etag",
];

/// Whether `path` is the registry ping or a digest read of the runtime store.
pub fn valid_store_path(path: &str) -> bool {
    if path == "/v2/" {
        return true;
    }
    let Some(rest) = path.strip_prefix("/v2/vonk/runtime/") else {
        return false;
    };
    rest.strip_prefix("manifests/sha256:")
        .or_else(|| rest.strip_prefix("blobs/sha256:"))
        .is_some_and(|digest| {
            digest.len() == 64
                && digest
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        })
}

#[derive(Debug, PartialEq, Eq)]
pub struct StoreRequest {
    pub head: bool,
    pub path: String,
    pub accept: Option<String>,
    pub range: Option<String>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum RequestRefusal {
    Malformed,
    Method,
    Path,
}

/// Parse one HTTP/1.1 request head from Docker, accepting only store reads.
pub fn parse_request_head(head: &[u8]) -> Result<StoreRequest, RequestRefusal> {
    let text = std::str::from_utf8(head).map_err(|_| RequestRefusal::Malformed)?;
    let mut lines = text.split("\r\n");
    let mut request_line = lines.next().ok_or(RequestRefusal::Malformed)?.split(' ');
    let (Some(method), Some(target), Some(version), None) = (
        request_line.next(),
        request_line.next(),
        request_line.next(),
        request_line.next(),
    ) else {
        return Err(RequestRefusal::Malformed);
    };
    if !version.starts_with("HTTP/1.") {
        return Err(RequestRefusal::Malformed);
    }
    let head_only = match method {
        "GET" => false,
        "HEAD" => true,
        _ => return Err(RequestRefusal::Method),
    };
    if !valid_store_path(target) {
        return Err(RequestRefusal::Path);
    }
    let (mut accept, mut range) = (None, None);
    for line in lines.take_while(|line| !line.is_empty()) {
        let (name, value) = line.split_once(':').ok_or(RequestRefusal::Malformed)?;
        let value = value.trim();
        if value.bytes().any(|byte| byte.is_ascii_control()) {
            return Err(RequestRefusal::Malformed);
        }
        if name.eq_ignore_ascii_case("accept") {
            accept = Some(match accept {
                Some(previous) => format!("{previous}, {value}"),
                None => value.to_owned(),
            });
        } else if name.eq_ignore_ascii_case("range") {
            range = Some(value.to_owned());
        }
    }
    Ok(StoreRequest {
        head: head_only,
        path: target.to_owned(),
        accept,
        range,
    })
}

/// The response head sent to Docker: the upstream status and the registry
/// headers it needs, one response per connection.
pub fn response_head(status: reqwest::StatusCode, headers: &HeaderMap) -> String {
    let mut head = format!(
        "HTTP/1.1 {} {}\r\n",
        status.as_u16(),
        status.canonical_reason().unwrap_or("")
    );
    for name in FORWARDED_HEADERS {
        if let Some(value) = headers.get(name).and_then(|value| value.to_str().ok()) {
            head.push_str(&format!("{name}: {value}\r\n"));
        }
    }
    head.push_str("connection: close\r\n\r\n");
    head
}

fn refusal_response(refusal: &RequestRefusal) -> &'static [u8] {
    match refusal {
        RequestRefusal::Malformed => {
            b"HTTP/1.1 400 Bad Request\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
        }
        RequestRefusal::Method => {
            b"HTTP/1.1 405 Method Not Allowed\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
        }
        RequestRefusal::Path => {
            b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
        }
    }
}

async fn read_request_head(stream: &mut TcpStream) -> std::io::Result<Vec<u8>> {
    let mut head = Vec::new();
    let mut buffer = [0_u8; 1024];
    while !head.windows(4).any(|window| window == b"\r\n\r\n") {
        if head.len() > MAX_REQUEST_HEAD {
            return Err(std::io::Error::other("request head too large"));
        }
        let read = stream.read(&mut buffer).await?;
        if read == 0 {
            return Err(std::io::ErrorKind::UnexpectedEof.into());
        }
        head.extend_from_slice(&buffer[..read]);
    }
    let end = head
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .unwrap_or(head.len());
    head.truncate(end);
    Ok(head)
}

async fn serve_connection(
    mut stream: TcpStream,
    client: &dyn StoreUpstream,
    progress: PullProgress<'_>,
    failure: &FailureSlot,
) {
    // One failed blob read is Docker's to retry; the pull keeps its other
    // connections, and the cause stays with the pull for its evidence.
    if let Err(error) = forward(&mut stream, client, progress).await {
        eprintln!("vonk-agent: {error}");
        *failure
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(error);
    }
    let _ = stream.shutdown().await;
}

fn stalled(stalled: Stalled) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::TimedOut, stalled)
}

async fn forward(
    stream: &mut TcpStream,
    client: &dyn StoreUpstream,
    progress: PullProgress<'_>,
) -> Result<(), StoreFailure> {
    let request = match parse_request_head(&read_request_head(stream).await?) {
        Ok(request) => request,
        Err(refusal) => return Ok(stream.write_all(refusal_response(&refusal)).await?),
    };
    let response = match client
        .get(
            request.head,
            &request.path,
            request.accept.as_deref(),
            request.range.as_deref(),
        )
        .await
    {
        Ok(response) => response,
        Err(error) => {
            // Docker retries a 502; the upstream cause is what the pull keeps.
            if let Err(write) = stream
                .write_all(
                    b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\nconnection: close\r\n\r\n",
                )
                .await
            {
                eprintln!("vonk-agent: image store 502 not delivered: {write}");
            }
            return Err(StoreFailure::Upstream {
                path: request.path,
                error,
            });
        }
    };
    stream
        .write_all(response_head(response.status, &response.headers).as_bytes())
        .await?;
    if request.head {
        return Ok(());
    }
    // The blob ends only when it stops moving, upstream or toward Docker.
    let inactivity = Inactivity::new(TRANSFER_INACTIVITY);
    let mut body = response.body;
    while let Some(chunk) = inactivity.bound(body.next()).await.map_err(stalled)? {
        let chunk = chunk?;
        inactivity
            .bound(stream.write_all(&chunk))
            .await
            .map_err(stalled)??;
        inactivity.observe_bytes(chunk.len() as u64);
        progress(chunk.len() as u64);
    }
    Ok(stream.flush().await?)
}

/// A loopback view of the Controller's runtime image store for one pull.
pub struct LoopbackImageStore {
    listener: TcpListener,
    port: u16,
}

impl LoopbackImageStore {
    pub async fn bind() -> std::io::Result<Self> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
        let port = listener.local_addr()?.port();
        Ok(Self { listener, port })
    }

    /// The helper's `image-pull` arguments for one pinned image.
    pub fn pull_arguments(&self, manifest_digest: &str, config_digest: &str) -> Vec<String> {
        let hex = manifest_digest
            .strip_prefix("sha256:")
            .unwrap_or(manifest_digest);
        vec![
            format!("127.0.0.1:{}", self.port),
            manifest_digest.to_owned(),
            config_digest.to_owned(),
            format!("localhost/vonk/compiled-runtime-{hex}@{manifest_digest}"),
        ]
    }

    /// Serve the store until `work` (the helper's pull) completes, and return
    /// its outcome with the latest read the store could not serve.
    pub async fn serve_while<T>(
        self,
        client: &dyn StoreUpstream,
        progress: PullProgress<'_>,
        work: impl Future<Output = T>,
    ) -> (T, Option<StoreFailure>) {
        let failure = FailureSlot::default();
        let outcome = self.serve_until(client, progress, work, &failure).await;
        (
            outcome,
            failure
                .into_inner()
                .unwrap_or_else(std::sync::PoisonError::into_inner),
        )
    }

    /// Docker opens several connections at once; each is served
    /// concurrently and streams its body without buffering. Every forwarded
    /// byte is reported to `progress`: it is the pull's progress.
    async fn serve_until<T>(
        self,
        client: &dyn StoreUpstream,
        progress: PullProgress<'_>,
        work: impl Future<Output = T>,
        failure: &FailureSlot,
    ) -> T {
        tokio::pin!(work);
        let mut connections = FuturesUnordered::new();
        loop {
            tokio::select! {
                result = &mut work => return result,
                accepted = self.listener.accept() => {
                    match accepted {
                        Ok((stream, peer)) if peer.ip().is_loopback() => {
                            connections.push(serve_connection(stream, client, progress, failure));
                        }
                        Ok(_) => {}
                        Err(error) => eprintln!("vonk-agent: image store accept deferred: {error}"),
                    }
                }
                Some(()) = connections.next(), if !connections.is_empty() => {}
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DIGEST: &str = "0e16f117c4f95ba678a0d89ef1fe2cbf2c08bec568f9c55d4873272ac1d69b7e";

    #[test]
    fn only_the_ping_and_digest_reads_are_served() {
        assert!(valid_store_path("/v2/"));
        assert!(valid_store_path(&format!(
            "/v2/vonk/runtime/manifests/sha256:{DIGEST}"
        )));
        assert!(valid_store_path(&format!(
            "/v2/vonk/runtime/blobs/sha256:{DIGEST}"
        )));
        for path in [
            "/v2",
            "/v2/_catalog",
            "/v2/vonk/runtime/manifests/latest",
            "/v2/vonk/releases/manifests/sha256:0e16f117c4f95ba678a0d89ef1fe2cbf2c08bec568f9c55d4873272ac1d69b7e",
            "/v2/vonk/runtime/blobs/sha256:0E16F117C4F95BA678A0D89EF1FE2CBF2C08BEC568F9C55D4873272AC1D69B7E",
            "/v2/vonk/runtime/blobs/sha256:../../agent/claim",
            "/agent/claim",
            "/v2/vonk/runtime/blobs/uploads/",
        ] {
            assert!(!valid_store_path(path), "{path}");
        }
    }

    #[test]
    fn docker_request_heads_are_parsed_and_everything_else_refused() {
        let request = parse_request_head(
            format!(
                "GET /v2/vonk/runtime/blobs/sha256:{DIGEST} HTTP/1.1\r\nHost: 127.0.0.1:41000\r\n\
                 Accept: application/vnd.oci.image.manifest.v1+json\r\n\
                 Accept: application/vnd.docker.distribution.manifest.v2+json\r\n\
                 Range: bytes=100-\r\nUser-Agent: docker/27\r\n"
            )
            .as_bytes(),
        )
        .unwrap();
        assert_eq!(
            request,
            StoreRequest {
                head: false,
                path: format!("/v2/vonk/runtime/blobs/sha256:{DIGEST}"),
                accept: Some(
                    "application/vnd.oci.image.manifest.v1+json, \
                     application/vnd.docker.distribution.manifest.v2+json"
                        .to_owned()
                ),
                range: Some("bytes=100-".to_owned()),
            }
        );
        assert!(parse_request_head(b"HEAD /v2/ HTTP/1.1\r\n").unwrap().head);
        assert_eq!(
            parse_request_head(b"PUT /v2/ HTTP/1.1\r\n"),
            Err(RequestRefusal::Method)
        );
        assert_eq!(
            parse_request_head(b"GET /v2/_catalog HTTP/1.1\r\n"),
            Err(RequestRefusal::Path)
        );
        assert_eq!(
            parse_request_head(b"GET /v2/ HTTP/1.1 extra\r\n"),
            Err(RequestRefusal::Malformed)
        );
    }

    #[test]
    fn response_heads_carry_only_registry_headers_and_close() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "content-type",
            "application/vnd.oci.image.manifest.v1+json"
                .parse()
                .unwrap(),
        );
        headers.insert("content-length", "1234".parse().unwrap());
        headers.insert(
            "docker-content-digest",
            format!("sha256:{DIGEST}").parse().unwrap(),
        );
        headers.insert("set-cookie", "leak".parse().unwrap());
        let head = response_head(reqwest::StatusCode::OK, &headers);
        assert!(head.starts_with("HTTP/1.1 200 OK\r\n"));
        assert!(head.contains("content-length: 1234\r\n"));
        assert!(head.contains(&format!("docker-content-digest: sha256:{DIGEST}\r\n")));
        assert!(!head.contains("set-cookie"));
        assert!(head.ends_with("connection: close\r\n\r\n"));
    }

    struct FakeController;

    #[async_trait::async_trait]
    impl StoreUpstream for FakeController {
        async fn get(
            &self,
            head: bool,
            path: &str,
            _accept: Option<&str>,
            range: Option<&str>,
        ) -> Result<StoreResponse, ClientError> {
            let body = format!("{path} range={}", range.unwrap_or("-"));
            let mut headers = HeaderMap::new();
            headers.insert("content-type", "application/octet-stream".parse().unwrap());
            headers.insert("content-length", body.len().to_string().parse().unwrap());
            headers.insert(
                "docker-content-digest",
                format!("sha256:{DIGEST}").parse().unwrap(),
            );
            headers.insert("set-cookie", "leak".parse().unwrap());
            let chunks = if head {
                Vec::new()
            } else {
                // Two chunks: the body is streamed, not buffered.
                let (first, second) = body.split_at(body.len() / 2);
                vec![
                    Ok(first.as_bytes().to_vec()),
                    Ok(second.as_bytes().to_vec()),
                ]
            };
            Ok(StoreResponse {
                status: reqwest::StatusCode::from_u16(if range.is_some() { 206 } else { 200 })
                    .unwrap(),
                headers,
                body: futures_util::stream::iter(chunks).boxed(),
            })
        }
    }

    async fn exchange(port: u16, request: String) -> String {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        stream.write_all(request.as_bytes()).await.unwrap();
        let mut response = String::new();
        stream.read_to_string(&mut response).await.unwrap();
        response
    }

    #[tokio::test]
    async fn the_loopback_store_forwards_docker_reads_while_the_pull_runs() {
        let store = LoopbackImageStore::bind().await.unwrap();
        let port = store.port;
        let path = format!("/v2/vonk/runtime/blobs/sha256:{DIGEST}");
        let forwarded = std::sync::atomic::AtomicU64::new(0);
        let progress = |bytes| {
            forwarded.fetch_add(bytes, std::sync::atomic::Ordering::SeqCst);
        };
        let ((blob, ranged, refused), failure) = store
            .serve_while(&FakeController, &progress, async {
                (
                    exchange(port, format!("GET {path} HTTP/1.1\r\nHost: x\r\n\r\n")).await,
                    exchange(
                        port,
                        format!("GET {path} HTTP/1.1\r\nRange: bytes=5-\r\n\r\n"),
                    )
                    .await,
                    exchange(port, "DELETE /v2/ HTTP/1.1\r\n\r\n".to_owned()).await,
                )
            })
            .await;
        assert!(blob.starts_with("HTTP/1.1 200 OK\r\n"), "{blob}");
        assert!(blob.contains(&format!("docker-content-digest: sha256:{DIGEST}\r\n")));
        assert!(!blob.contains("set-cookie"));
        assert!(blob.ends_with(&format!("{path} range=-")));
        assert!(ranged.starts_with("HTTP/1.1 206 Partial Content\r\n"));
        assert!(ranged.ends_with("range=bytes=5-"));
        assert!(refused.starts_with("HTTP/1.1 405"));
        assert!(failure.is_none(), "{failure:?}");
        // Every forwarded body byte is the pull's progress.
        let bodies = format!("{path} range=-").len() + format!("{path} range=bytes=5-").len();
        assert_eq!(
            forwarded.load(std::sync::atomic::Ordering::SeqCst),
            bodies as u64
        );
        // The store stops serving once the pull is over.
        assert!(TcpStream::connect(("127.0.0.1", port)).await.is_err());
    }

    #[tokio::test]
    async fn pull_arguments_name_the_loopback_store_and_the_pinned_local_image() {
        let store = LoopbackImageStore::bind().await.unwrap();
        let port = store.port;
        assert_ne!(port, 0);
        let manifest = format!("sha256:{DIGEST}");
        let config = format!("sha256:{}", "c".repeat(64));
        assert_eq!(
            store.pull_arguments(&manifest, &config),
            vec![
                format!("127.0.0.1:{port}"),
                manifest.clone(),
                config,
                format!("localhost/vonk/compiled-runtime-{DIGEST}@{manifest}"),
            ]
        );
    }
}
