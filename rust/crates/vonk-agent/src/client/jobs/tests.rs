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
