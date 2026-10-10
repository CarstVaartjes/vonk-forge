#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn recipe_image_upload_resumes_interruption_and_overrides_ordinary_timeout() {
    let directory = tempfile::tempdir().unwrap();
    let archive = directory.path().join("image.docker.tar");
    std::fs::write(&archive, b"accepted archive").unwrap();
    let (client, server) = delayed_upload_client(Duration::from_millis(150));

    let result = client
        .upload_recipe_image(
            Uuid::parse_str("45ea6921-50c9-4971-be2a-4cd04ce05069").unwrap(),
            &format!("sha256:{}", "b".repeat(64)),
            &"a".repeat(64),
            16,
            &archive,
            |_| {},
        )
        .await;
    let request = server.finish().unwrap();

    assert!(
        result.is_ok(),
        "large upload inherited ordinary timeout: {result:?}"
    );
    assert!(request.starts_with(b"PUT /agent/recipe-builds/"));
    assert!(request.ends_with(b"archive"));
    let headers = String::from_utf8_lossy(&request);
    assert!(headers.contains("x-vonk-upload-offset: 9"));
    assert!(headers.contains("content-length: 7"));
}

#[tokio::test]
async fn recipe_job_input_stream_is_exact_and_cleans_interrupted_or_invalid_temps() {
    let cases = [
        // The digest names the input; only size is checked on receipt.
        (7, b"weights".to_vec(), "a".repeat(64), true),
        (7, b"short".to_vec(), hex_sha256(b"short!!"), false),
        (8, b"oversize".to_vec(), hex_sha256(b"oversize"), false),
        (7, b"weights".to_vec(), hex_sha256(b"weights"), true),
    ];
    for (declared, body, digest, succeeds) in cases {
        let directory = tempfile::tempdir().unwrap();
        let destination = directory.path().join("input.bin");
        let (client, server) = job_input_client(declared, body);
        let result = client
            .download_recipe_job_input(
                Uuid::parse_str("45ea6921-50c9-4971-be2a-4cd04ce05069").unwrap(),
                &digest,
                7,
                &destination,
            )
            .await;
        server.finish().unwrap();
        assert_eq!(result.is_ok(), succeeds);
        assert_eq!(destination.exists(), succeeds);
        assert!(
            !std::fs::read_dir(directory.path())
                .unwrap()
                .filter_map(Result::ok)
                .any(|entry| entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".job-input-"))
        );
    }
}

#[tokio::test]
async fn unreadable_upload_ack_reobserves_exact_content_before_any_second_put() {
    let directory = tempfile::tempdir().unwrap();
    let archive = directory.path().join("image.tar");
    std::fs::write(&archive, b"accepted archive").unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let client = authenticated_test_client(&format!("http://{address}"), TEST_NODE_ID);
    let server = spawn_peer(move || {
        let deadline = std::time::Instant::now() + PEER_BUDGET;
        let mut uploads = 0;
        let mut accepted = Vec::new();
        for step in 0..4 {
            let mut stream = accept_peer(&listener, deadline);
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            loop {
                assert!(std::time::Instant::now() < deadline);
                let count = stream.read(&mut buffer).unwrap();
                assert!(count > 0);
                request.extend_from_slice(&buffer[..count]);
                if request.windows(4).any(|part| part == b"\r\n\r\n")
                    && (step != 1 || request.ends_with(b"accepted archive"))
                {
                    break;
                }
            }
            if step == 1 {
                assert!(request.starts_with(b"PUT "));
                uploads += 1;
                accepted = request[request.len() - 16..].to_vec();
                // The effect was received, but this is not the acknowledgement
                // shape the client can consume. It must inspect exact status.
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    .unwrap();
            } else {
                assert!(request.starts_with(b"HEAD "));
                let complete = step > 1;
                let offset = if complete { 16 } else { 0 };
                write!(stream, "HTTP/1.1 200 OK\r\nx-vonk-upload-offset: {offset}\r\nx-vonk-upload-complete: {complete}\r\nConnection: close\r\n\r\n").unwrap();
            }
        }
        (uploads, accepted)
    });
    let build = Uuid::new_v4();
    let digest = format!("sha256:{}", "b".repeat(64));
    let layout = hex_sha256(b"accepted archive");
    let error = client
        .upload_recipe_image(build, &digest, &layout, 16, &archive, |_| {})
        .await
        .unwrap_err();
    assert!(!error.retryable());
    client
        .upload_recipe_image(build, &digest, &layout, 16, &archive, |_| {})
        .await
        .unwrap();
    // A fresh observer is admitted and reuses the accepted exact bytes too.
    client
        .clone()
        .upload_recipe_image(build, &digest, &layout, 16, &archive, |_| {})
        .await
        .unwrap();
    let (uploads, accepted) = server.finish().unwrap();
    assert_eq!(uploads, 1);
    assert_eq!(accepted, std::fs::read(archive).unwrap());
}
