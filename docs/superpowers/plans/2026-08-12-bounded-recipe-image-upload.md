# Bounded Recipe Image Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow large recipe-image transfers to complete without ordinary-request timeout failures or near-expiry host-runtime grant rejection.

**Architecture:** Keep the existing shared `reqwest::Client` and its 75-second default. Override the timeout only on the authenticated recipe-image `PUT`; the existing operation heartbeat task continues to renew the controller lease while the upload is in progress. Request a 10-second one-use host-runtime grant so it fits safely inside the 30-second renewed operation lease without weakening the controller's strict lease bound.

**Tech Stack:** Rust 2024, Tokio, reqwest, existing loopback TCP test server, Cargo workspace tests.

## Global Constraints

- Ordinary claim, inventory, result, metadata, and range-download requests retain the existing 75-second timeout.
- Recipe-image upload has a one-hour total timeout and no unbounded request path.
- Existing media type, content length, digest, identity, authority, and controller-side atomic-file checks remain unchanged.
- Host-runtime grants request exactly 10 seconds of validity and remain strictly bounded by the active operation lease.
- No chunked/resumable protocol or controller schema change is part of this repair.
- Production code is not written until the focused regression test has failed for the expected timeout reason.

---

### Task 1: Add a request-specific recipe-image upload timeout

**Files:**
- Modify: `rust/crates/vonk-agent/src/client.rs`
- Test: `rust/crates/vonk-agent/src/client.rs`

**Interfaces:**
- Consumes: `AgentHttpClient::upload_recipe_image(build_id, image_digest, oci_layout_sha256, image_bytes, path)` and the existing shared `reqwest::Client`.
- Produces: `const RECIPE_IMAGE_UPLOAD_TIMEOUT: Duration = Duration::from_secs(60 * 60)` used only by the upload request builder.

- [ ] **Step 1: Write the failing real-transport test**

Add a loopback HTTP helper in `client.rs`'s existing test module. Construct its `AgentHttpClient` with a 50-millisecond shared timeout, read the complete request body, delay the `204 No Content` response for 150 milliseconds, and ignore a response-write error so the pre-fix client timeout is the assertion failure. Add this test:

```rust
fn delayed_upload_client(
    response_delay: Duration,
) -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = Vec::new();
        let mut buffer = [0_u8; 4096];
        let header_end = loop {
            let size = stream.read(&mut buffer).unwrap();
            assert_ne!(size, 0);
            request.extend_from_slice(&buffer[..size]);
            if let Some(index) = request.windows(4).position(|value| value == b"\r\n\r\n") {
                break index + 4;
            }
        };
        let headers = std::str::from_utf8(&request[..header_end]).unwrap();
        let content_length = headers
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().unwrap())
            })
            .unwrap();
        while request.len() - header_end < content_length {
            let size = stream.read(&mut buffer).unwrap();
            assert_ne!(size, 0);
            request.extend_from_slice(&buffer[..size]);
        }
        thread::sleep(response_delay);
        let _ = stream.write_all(
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        );
        request
    });
    let client = reqwest::Client::builder()
        .timeout(Duration::from_millis(50))
        .build()
        .unwrap();
    (
        AgentHttpClient {
            client,
            controller: Url::parse(&format!("http://{address}/")).unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
        },
        server,
    )
}

#[tokio::test]
async fn recipe_image_upload_overrides_the_short_ordinary_request_timeout() {
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
        )
        .await;
    let request = server.join().unwrap();

    assert!(result.is_ok(), "large upload inherited ordinary timeout: {result:?}");
    assert!(request.starts_with(b"PUT /agent/v1/recipe-builds/"));
    assert!(request.ends_with(b"accepted archive"));
}
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cargo test -p vonk-agent client::tests::recipe_image_upload_overrides_the_short_ordinary_request_timeout -- --exact --nocapture
```

Expected: FAIL after approximately 50 milliseconds with `ClientError::Transport` caused by a request timeout. The server must have received the full 16-byte body before delaying its response.

- [ ] **Step 3: Implement the minimal request override**

Add beside `MAX_BODY_BYTES`:

```rust
const RECIPE_IMAGE_UPLOAD_TIMEOUT: Duration = Duration::from_secs(60 * 60);
```

In `upload_recipe_image`, add exactly this request-builder call before `.body(...)`:

```rust
.timeout(RECIPE_IMAGE_UPLOAD_TIMEOUT)
```

Do not alter the shared client builder or any other request.

- [ ] **Step 4: Verify GREEN and the surrounding crate**

Run:

```bash
cargo test -p vonk-agent client::tests::recipe_image_upload_overrides_the_short_ordinary_request_timeout -- --exact --nocapture
cargo test -p vonk-agent
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
```

Expected: all commands succeed without warnings.

- [ ] **Step 5: Commit the repair**

```bash
git add rust/crates/vonk-agent/src/client.rs
git commit -m "fix(agent): bound large recipe image uploads"
```

### Task 2: Keep the one-use runtime grant inside the renewed lease

**Files:**
- Modify: `rust/crates/vonk-agent/src/client.rs`
- Test: `rust/crates/vonk-agent/src/client.rs`

**Interfaces:**
- Consumes: `AgentHttpClient::host_runtime_grant(&AgentClaim, HostRuntimeAction, &str)` and the controller's existing strict grant-expiry validation.
- Produces: `const HOST_RUNTIME_GRANT_TTL_SECONDS: u16 = 10` serialized as `expires_in_seconds` only for one-use host-runtime grants.

- [ ] **Step 1: Write the failing real-transport test**

Add a loopback helper beside `heartbeat_client` that reads the complete request and returns `{"grant":{}}` with status 200. Add a test that constructs a valid `AgentClaim`, requests an `ImageImport` grant, parses the captured request body, and asserts the exact TTL:

```rust
#[tokio::test]
async fn host_runtime_grant_ttl_fits_inside_renewed_operation_lease() {
    let payload = serde_json::json!({});
    let claim = AgentClaim {
        schema_version: 1,
        job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
        operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
        attempt: 1,
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
        operation: "recipe.image.import.v1".to_owned(),
        base_commit: "a".repeat(40),
        payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
        payload,
        deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
    };
    let (client, server) = host_runtime_grant_client();

    client
        .host_runtime_grant(&claim, HostRuntimeAction::ImageImport, &"c".repeat(64))
        .await
        .unwrap();
    let request = server.join().unwrap();
    let body = request
        .windows(4)
        .position(|value| value == b"\r\n\r\n")
        .map(|index| &request[index + 4..])
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(body).unwrap();

    assert_eq!(body["expires_in_seconds"], 10);
}
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cargo test -p vonk-agent client::tests::host_runtime_grant_ttl_fits_inside_renewed_operation_lease -- --exact --nocapture
```

Expected: FAIL with left value `30` and right value `10`, proving the test observes the existing production request.

- [ ] **Step 3: Implement the minimal TTL change**

Add beside the other client constants:

```rust
const HOST_RUNTIME_GRANT_TTL_SECONDS: u16 = 10;
```

Replace only the `expires_in_seconds: 30` literal in `host_runtime_grant` with:

```rust
expires_in_seconds: HOST_RUNTIME_GRANT_TTL_SECONDS,
```

Do not relax the controller authority check or lengthen operation leases.

- [ ] **Step 4: Verify GREEN and the surrounding crate**

Run:

```bash
cargo test -p vonk-agent client::tests::host_runtime_grant_ttl_fits_inside_renewed_operation_lease -- --exact --nocapture
cargo test -p vonk-agent
cargo fmt --all -- --check
cargo clippy -p vonk-agent --all-targets --all-features -- -D warnings
```

Expected: all commands succeed without warnings.

- [ ] **Step 5: Commit the repair**

```bash
git add rust/crates/vonk-agent/src/client.rs
git commit -m "fix(agent): fit runtime grants inside active leases"
```

### Task 3: Publish and physically verify the accepted repair

**Files:**
- Modify: `docs/runbooks/development-agent-workloads.md`
- Evidence (private and ignored): `.state/development-acceptance/model-single-physical-20260812.json`

**Interfaces:**
- Consumes: accepted `main` Rust agent package from `.github/workflows/agent-apt-publish.yml` and the durable recipe build operation.
- Produces: stable A/B supervisor state on both Sparks and completed single-node physical acceptance evidence.

- [ ] **Step 1: Run repository verification and request independent review**

Run:

```bash
cargo test --workspace --locked
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
git diff --check origin/main...HEAD
```

Obtain independent spec-compliance and code-quality review; address every Critical or Important finding before publication.

- [ ] **Step 2: Publish through the protected GitHub path**

Push the branch, open a PR, wait for all required checks, merge to `main`, and wait for the Rust agent development workflow to publish the monotonic `dev` APT version. Do not build or publish a release locally.

- [ ] **Step 3: Upgrade and activate canary-first**

On Spark 2, update APT, verify the candidate embeds the accepted merge SHA, upgrade, hash the package-staged slot, activate it through `/usr/lib/vonk-forge/vonk-agent-supervisor`, and require `stable`, the expected active digest, and `rollback_performed=false`. Repeat on Spark 1 only after Spark 2 is healthy.

- [ ] **Step 4: Resume the durable acceptance slice**

Resume `scripts/run-development-slices` with the identical `model-single` arguments and evidence path. Require the exact 2,592,110,592-byte archive upload to complete, then continue through distribution, artifact acquisition, install, start, route publication, and first successful inference.

- [ ] **Step 5: Record the operator restart distinction**

Update the development runbook to say the NAS UI durability action is **Stop project**, wait until stopped, then **Start project**. Its CLI equivalent is an ordered project stop followed by full `docker compose up -d --wait`; explicitly forbid combining `docker compose restart` with a dependency-reconciling `up`, because that can run the cohort reset after API/worker have already started.
