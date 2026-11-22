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
            status: reqwest::StatusCode::from_u16(if range.is_some() { 206 } else { 200 }).unwrap(),
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
