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
            head = format!("{head}{name}: {value}\r\n");
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
    #[expect(
        clippy::let_underscore_must_use,
        reason = "the receiving side may already be gone; nobody is waiting for the value"
    )]
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
mod tests;
