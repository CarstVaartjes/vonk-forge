#![forbid(unsafe_code)]

use std::{
    cell::{Cell, RefCell},
    collections::BTreeMap,
    fs::{self, File},
    io::{Cursor, Read, Seek, SeekFrom},
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Child, Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};

use tempfile::{TempDir, tempdir};
use uuid::Uuid;
use vonk_agent::{
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    recipe_builder::{RecipeBuildError, RecipeBuilder},
};
use vonk_agent_protocol::{
    RecipeBuildAdditionalContext, RecipeBuildArgument, RecipeBuildBaseImage, RecipeBuildLimits,
    RecipeBuildMetadata, RecipeBuildNetwork, RecipeBuildOptions, RecipeBuildRequest,
    canonical_json, hex_sha256,
};

struct Runner {
    calls: RefCell<Vec<(Program, Vec<String>)>>,
    fail_build: bool,
    oversize_base: bool,
    registry: Option<OciRegistryFixture>,
    substitute_base: bool,
}

struct RetryManifestRunner {
    inner: Runner,
    remaining_failures: Cell<usize>,
}

struct FailedImportRunner {
    inner: Runner,
    stderr: Vec<u8>,
    temporary_directory: RefCell<Option<PathBuf>>,
    monitored_directory: RefCell<Option<PathBuf>>,
    maximum_bytes: Cell<u64>,
}

impl ProcessRunner for FailedImportRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.inner.run(program, arguments, timeout)
    }

    fn run_bounded_directory_with_input(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
        input: &File,
        directory: &Path,
        maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        assert_eq!(program, Program::Podman);
        assert!(arguments.iter().any(|argument| argument == "load"));
        assert!(arguments.iter().any(|argument| argument == "--quiet"));
        let storage = arguments
            .windows(2)
            .find(|pair| pair[0] == "--root")
            .map(|pair| Path::new(&pair[1]))
            .unwrap();
        let temporary_directory = storage.parent().unwrap().join("podman-image-tmp");
        assert!(temporary_directory.is_dir());
        assert_eq!(temporary_directory.parent(), Some(directory));
        assert!(input.metadata()?.len() > 0);
        self.temporary_directory
            .borrow_mut()
            .replace(temporary_directory);
        self.monitored_directory
            .borrow_mut()
            .replace(directory.to_path_buf());
        self.maximum_bytes.set(maximum_bytes);
        Ok(ProcessOutput {
            success: false,
            stdout: Vec::new(),
            stderr: self.stderr.clone(),
        })
    }
}

struct CancellingRunner {
    inner: Runner,
    cancelled: Cell<bool>,
}

impl ProcessRunner for CancellingRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        let output = self.inner.run(program, arguments, timeout);
        if arguments.iter().any(|value| value == "build") {
            self.cancelled.set(true);
        }
        output
    }
}

impl ProcessRunner for RetryManifestRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.inner.run(program, arguments, timeout)
    }

    fn run_to_file(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        sink: &mut File,
        maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        if program == Program::Oras
            && arguments.iter().any(|argument| argument == "manifest")
            && self.remaining_failures.get() > 0
        {
            self.remaining_failures
                .set(self.remaining_failures.get() - 1);
            return Ok(ProcessOutput {
                success: false,
                stdout: Vec::new(),
                stderr: b"bounded registry failure".to_vec(),
            });
        }
        self.inner
            .run_to_file(program, arguments, timeout, sink, maximum_bytes)
    }
}

#[derive(Clone)]
struct OciRegistryFixture {
    config: Vec<u8>,
    config_digest: String,
    layer: Vec<u8>,
    layer_digest: String,
    manifest: Vec<u8>,
    manifest_digest: String,
    reference: String,
}

fn registry_fixture() -> OciRegistryFixture {
    let mut layer = Vec::new();
    {
        let mut archive = tar::Builder::new(&mut layer);
        let content = b"faithful OCI layer\n";
        let mut header = tar::Header::new_ustar();
        header.set_path("fixture.txt").unwrap();
        header.set_size(content.len() as u64);
        header.set_mode(0o644);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        archive.append(&header, Cursor::new(content)).unwrap();
        archive.finish().unwrap();
    }
    let layer_digest = format!("sha256:{}", hex_sha256(&layer));
    let config = serde_json::to_vec(&serde_json::json!({
        "architecture": "arm64",
        "config": {"User": "10001:10001"},
        "os": "linux",
        "rootfs": {"diff_ids": [layer_digest.clone()], "type": "layers"}
    }))
    .unwrap();
    let config_digest = format!("sha256:{}", hex_sha256(&config));
    let manifest = serde_json::to_vec(&serde_json::json!({
        "config": {
            "digest": config_digest,
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": config.len()
        },
        "layers": [{
            "digest": layer_digest,
            "mediaType": "application/vnd.oci.image.layer.v1.tar",
            "size": layer.len()
        }],
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "schemaVersion": 2
    }))
    .unwrap();
    let manifest_digest = format!("sha256:{}", hex_sha256(&manifest));
    OciRegistryFixture {
        config,
        config_digest,
        layer,
        layer_digest,
        manifest,
        reference: format!("1.1.1.1/vonkforge/base:ignored-tag@{manifest_digest}"),
        manifest_digest,
    }
}

fn duplicate_layer_registry_fixture() -> OciRegistryFixture {
    let mut fixture = registry_fixture();
    let mut manifest: serde_json::Value = serde_json::from_slice(&fixture.manifest).unwrap();
    let layer = manifest["layers"][0].clone();
    manifest["layers"].as_array_mut().unwrap().push(layer);
    fixture.manifest = serde_json::to_vec(&manifest).unwrap();
    fixture.manifest_digest = format!("sha256:{}", hex_sha256(&fixture.manifest));
    fixture.reference = format!(
        "1.1.1.1/vonkforge/base:ignored-tag@{}",
        fixture.manifest_digest
    );
    fixture
}

fn conflicting_duplicate_layer_registry_fixture() -> OciRegistryFixture {
    let mut fixture = duplicate_layer_registry_fixture();
    let mut manifest: serde_json::Value = serde_json::from_slice(&fixture.manifest).unwrap();
    manifest["layers"][1]["size"] = serde_json::json!(fixture.layer.len() + 1);
    fixture.manifest = serde_json::to_vec(&manifest).unwrap();
    fixture.manifest_digest = format!("sha256:{}", hex_sha256(&fixture.manifest));
    fixture.reference = format!(
        "1.1.1.1/vonkforge/base:ignored-tag@{}",
        fixture.manifest_digest
    );
    fixture
}

impl ProcessRunner for Runner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.calls.borrow_mut().push((program, arguments.to_vec()));
        let registry_stdout = self.registry.as_ref().and_then(|fixture| {
            if program != Program::Oras {
                return None;
            }
            if arguments.iter().any(|value| value == "manifest") {
                return Some(fixture.manifest.clone());
            }
            let reference = arguments.last()?;
            if reference.ends_with(&fixture.config_digest) {
                Some(fixture.config.clone())
            } else if reference.ends_with(&fixture.layer_digest) {
                Some(fixture.layer.clone())
            } else {
                None
            }
        });
        let stdout = if let Some(payload) = registry_stdout {
            payload
        } else if arguments.iter().any(|value| value.contains("{{.Digest}}")) {
            format!(
                "sha256:{}\tlinux\tarm64\n",
                if self.substitute_base {
                    "e".repeat(64)
                } else if let Some(fixture) = &self.registry {
                    fixture
                        .manifest_digest
                        .strip_prefix("sha256:")
                        .unwrap()
                        .to_owned()
                } else {
                    registry_fixture()
                        .manifest_digest
                        .strip_prefix("sha256:")
                        .unwrap()
                        .to_owned()
                }
            )
            .into_bytes()
        } else if arguments.iter().any(|value| value == "inspect") {
            b"linux\tarm64\tv1\t10001:10001\n".to_vec()
        } else {
            Vec::new()
        };
        if self.oversize_base && arguments.iter().any(|value| value == "load") {
            let storage = arguments
                .windows(2)
                .find(|pair| pair[0] == "--root")
                .map(|pair| Path::new(&pair[1]))
                .unwrap();
            fs::write(storage.join("oversized-layer"), [0_u8; 1024])?;
        }
        if arguments.iter().any(|value| value == "push") {
            let digest_file = arguments
                .windows(2)
                .find(|pair| pair[0] == "--digestfile")
                .map(|pair| &pair[1])
                .unwrap();
            fs::write(digest_file, format!("sha256:{}\n", "d".repeat(64)))?;
            let output = arguments
                .last()
                .unwrap()
                .strip_prefix("docker-archive:")
                .unwrap();
            fs::write(output, b"exact docker archive")?;
        }
        if arguments.iter().any(|value| value == "build") {
            let storage = arguments
                .windows(2)
                .find(|pair| pair[0] == "--root")
                .map(|pair| Path::new(&pair[1]))
                .unwrap();
            let readonly_layer = storage.join("overlay/diff/readonly");
            fs::create_dir_all(&readonly_layer)?;
            fs::write(readonly_layer.join("layer"), b"podman layer")?;
            fs::set_permissions(&readonly_layer, fs::Permissions::from_mode(0o555))?;
        }
        Ok(ProcessOutput {
            success: !(self.fail_build && arguments.iter().any(|value| value == "build")),
            stdout,
            stderr: Vec::new(),
        })
    }
}

fn bundle() -> (Vec<u8>, String) {
    bundle_for(&registry_fixture().reference)
}

fn bundle_for(reference: &str) -> (Vec<u8>, String) {
    let dockerfile = format!("FROM {reference}\nUSER 10001:10001\n");
    let mut payload = Vec::new();
    {
        let mut archive = tar::Builder::new(&mut payload);
        let mut header = tar::Header::new_ustar();
        header.set_path("Dockerfile").unwrap();
        header.set_size(dockerfile.len() as u64);
        header.set_mode(0o644);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        archive
            .append(&header, Cursor::new(dockerfile.as_bytes()))
            .unwrap();
        archive.finish().unwrap();
    }
    let manifest = BTreeMap::from([
        (
            "files",
            serde_json::json!([{
                "mode": 420,
                "path": "Dockerfile",
                "sha256": hex_sha256(dockerfile.as_bytes()),
                "size": dockerfile.len()
            }]),
        ),
        ("schema_version", serde_json::json!(1)),
        ("total_bytes", serde_json::json!(dockerfile.len())),
    ]);
    let digest = hex_sha256(&canonical_json(&manifest).unwrap());
    (payload, digest)
}

fn request(bundle_bytes: usize, digest: String) -> RecipeBuildRequest {
    let base = registry_fixture();
    RecipeBuildRequest {
        arguments: vec![RecipeBuildArgument {
            name: "runtime-version".to_owned(),
            value: serde_json::json!("1"),
        }],
        base_image_storage_bytes: 64 * 1024 * 1024,
        base_images: vec![RecipeBuildBaseImage {
            manifest_digest: base.manifest_digest,
            reference: base.reference,
        }],
        capabilities: vec!["DAC_OVERRIDE".to_owned()],
        build_id: Uuid::parse_str("00000000-0000-4000-8000-000000000009").unwrap(),
        build_input_sha256: "c".repeat(64),
        dockerfile: "Dockerfile".to_owned(),
        kind: "recipe.build.v1".to_owned(),
        limits: RecipeBuildLimits {
            container_socket: false,
            cpu_cores: 8,
            gpu: 0,
            host_mounts: false,
            memory_bytes: 8 * 1024 * 1024 * 1024,
            output_bytes: 64 * 1024 * 1024,
            privileged: false,
            processes: 4096,
            temporary_bytes: 64 * 1024 * 1024,
            timeout_seconds: 3600,
        },
        network: RecipeBuildNetwork {
            hosts: Vec::new(),
            mode: "none".to_owned(),
        },
        options: RecipeBuildOptions {
            additional_contexts: vec![RecipeBuildAdditionalContext {
                name: "assets".to_owned(),
                path: "assets".to_owned(),
            }],
            annotations: vec![RecipeBuildMetadata {
                name: "org.example.annotation".to_owned(),
                value: "present".to_owned(),
            }],
            environment: vec![RecipeBuildArgument {
                name: "BUILD_MODE".to_owned(),
                value: serde_json::json!("release"),
            }],
            format: "oci".to_owned(),
            identity_label: true,
            ignorefile: Some(".containerignore".to_owned()),
            jobs: 2,
            labels: vec![RecipeBuildMetadata {
                name: "org.example.label".to_owned(),
                value: "value".to_owned(),
            }],
            layer_compression: "disabled".to_owned(),
            layer_labels: vec![RecipeBuildMetadata {
                name: "org.example.layer".to_owned(),
                value: "value".to_owned(),
            }],
            layers: true,
            no_hostname: false,
            no_hosts: false,
            omit_history: false,
            os_features: vec!["feature-a".to_owned()],
            os_version: Some("1.0".to_owned()),
            shm_bytes: 67_108_864,
            skip_unused_stages: true,
            squash: "none".to_owned(),
            timestamp: Some(0),
            unset_environment: vec!["OLD_ENV".to_owned()],
            unset_labels: vec!["org.example.old".to_owned()],
        },
        platform: "linux/arm64".to_owned(),
        recipe_content_sha256: "a".repeat(64),
        recipe_revision_id: Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
        schema_version: 1,
        source_bundle_bytes: bundle_bytes as u64,
        source_bundle_sha256: digest,
        target: None,
    }
}

fn stage_base_archive(root: &Path) {
    let fixture = registry_fixture();
    let directory = base_archive_path(root).parent().unwrap().to_path_buf();
    fs::create_dir_all(&directory).unwrap();
    fs::write(directory.join("image.oci.tar"), oci_archive(&fixture)).unwrap();
}

fn base_archive_path(root: &Path) -> std::path::PathBuf {
    let fixture = registry_fixture();
    root.join("base-images")
        .join("sha256")
        .join(fixture.manifest_digest.strip_prefix("sha256:").unwrap())
        .join("image.oci.tar")
}

fn oci_archive(fixture: &OciRegistryFixture) -> Vec<u8> {
    fn append(archive: &mut tar::Builder<&mut Vec<u8>>, path: &str, content: &[u8]) {
        let mut header = tar::Header::new_ustar();
        header.set_path(path).unwrap();
        header.set_size(content.len() as u64);
        header.set_mode(0o644);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        archive.append(&header, Cursor::new(content)).unwrap();
    }

    let reference_name = fixture.reference.rsplit_once('@').unwrap().0;
    let index = serde_json::to_vec(&serde_json::json!({
        "manifests": [{
            "annotations": {"org.opencontainers.image.ref.name": reference_name},
            "digest": fixture.manifest_digest,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "size": fixture.manifest.len()
        }],
        "schemaVersion": 2
    }))
    .unwrap();
    let mut payload = Vec::new();
    {
        let mut archive = tar::Builder::new(&mut payload);
        append(
            &mut archive,
            "oci-layout",
            br#"{"imageLayoutVersion":"1.0.0"}"#,
        );
        append(&mut archive, "index.json", &index);
        for (digest, content) in [
            (&fixture.manifest_digest, &fixture.manifest),
            (&fixture.config_digest, &fixture.config),
            (&fixture.layer_digest, &fixture.layer),
        ] {
            append(
                &mut archive,
                &format!("blobs/sha256/{}", digest.strip_prefix("sha256:").unwrap()),
                content,
            );
        }
        archive.finish().unwrap();
    }
    payload
}

fn docker_archive(fixture: &OciRegistryFixture, image_name: &str) -> Vec<u8> {
    fn append(archive: &mut tar::Builder<&mut Vec<u8>>, path: &str, content: &[u8]) {
        let mut header = tar::Header::new_ustar();
        header.set_path(path).unwrap();
        header.set_size(content.len() as u64);
        header.set_mode(0o644);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        archive.append(&header, Cursor::new(content)).unwrap();
    }

    let config_name = format!(
        "{}.json",
        fixture.config_digest.strip_prefix("sha256:").unwrap()
    );
    let layer_name = format!(
        "{}/layer.tar",
        fixture.layer_digest.strip_prefix("sha256:").unwrap()
    );
    let manifest = serde_json::to_vec(&serde_json::json!([{
        "Config": config_name.clone(),
        "Layers": [layer_name.clone()],
        "RepoTags": [image_name]
    }]))
    .unwrap();
    let mut payload = Vec::new();
    {
        let mut archive = tar::Builder::new(&mut payload);
        append(&mut archive, &config_name, &fixture.config);
        append(&mut archive, &layer_name, &fixture.layer);
        append(&mut archive, "manifest.json", &manifest);
        archive.finish().unwrap();
    }
    payload
}

struct PrivateContainerd {
    _root: TempDir,
    child: Child,
    socket: std::path::PathBuf,
}

impl PrivateContainerd {
    fn start() -> Self {
        let root = tempdir().unwrap();
        let metadata = fs::metadata(root.path()).unwrap();
        let config = root.path().join("config.toml");
        fs::write(
            &config,
            format!(
                "version = 3\ndisabled_plugins = [\"io.containerd.nri.v1.nri\"]\n[grpc]\n  uid = {}\n  gid = {}\n[ttrpc]\n  uid = {}\n  gid = {}\n",
                metadata.uid(),
                metadata.gid(),
                metadata.uid(),
                metadata.gid(),
            ),
        )
        .unwrap();
        let socket = root.path().join("containerd.sock");
        let stderr_path = root.path().join("containerd.stderr");
        let stderr = File::create(&stderr_path).unwrap();
        // containerd owns host-global plugin resources even when its root,
        // state, and sockets are isolated. The runner-wide flock serializes
        // real-daemon fixtures across test binaries and concurrent CI jobs;
        // --no-fork keeps the returned child bound to the daemon lifecycle.
        let mut child = Command::new("/usr/bin/flock")
            .args([
                "--exclusive",
                "--no-fork",
                "/tmp/vonk-agent-private-containerd.lock",
                "/usr/bin/containerd",
                "--config",
                config.to_str().unwrap(),
                "--root",
                root.path().join("content").to_str().unwrap(),
                "--state",
                root.path().join("state").to_str().unwrap(),
                "--address",
                socket.to_str().unwrap(),
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::from(stderr))
            .spawn()
            .expect("the local containerd integration fixture must be installed");
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                let diagnostic = bounded_text(&fs::read(&stderr_path).unwrap_or_default(), 8_192);
                panic!("private containerd exited before readiness: {status}: {diagnostic}");
            }
            let ready = Command::new("/usr/bin/ctr")
                .args(["--address", socket.to_str().unwrap(), "version"])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .is_ok_and(|status| status.success());
            if ready {
                break;
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                let diagnostic = bounded_text(&fs::read(&stderr_path).unwrap_or_default(), 8_192);
                panic!("private containerd did not become ready: {diagnostic}");
            }
            thread::sleep(Duration::from_millis(25));
        }
        Self {
            _root: root,
            child,
            socket,
        }
    }

    fn ctr(&self, arguments: &[&str]) -> Output {
        Command::new("/usr/bin/ctr")
            .args([
                "--address",
                self.socket.to_str().unwrap(),
                "--namespace",
                "vonk-test",
            ])
            .args(arguments)
            .stdin(Stdio::null())
            .output()
            .expect("the local ctr integration fixture must be installed")
    }
}

fn bounded_text(bytes: &[u8], maximum_bytes: usize) -> String {
    let start = bytes.len().saturating_sub(maximum_bytes);
    String::from_utf8_lossy(&bytes[start..]).into_owned()
}

#[test]
fn bounded_containerd_diagnostic_keeps_the_failure_tail() {
    assert_eq!(
        bounded_text(b"startup-noise-fatal-cause", 11),
        "fatal-cause"
    );
}

impl Drop for PrivateContainerd {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

struct DockerCleanup {
    container: String,
    image: String,
}

impl Drop for DockerCleanup {
    fn drop(&mut self) {
        let _ = Command::new("/usr/bin/docker")
            .args(["container", "rm", "--force", &self.container])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        let _ = Command::new("/usr/bin/docker")
            .args(["image", "rm", "--force", &self.image])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

fn docker(arguments: &[&str]) -> Output {
    Command::new("/usr/bin/docker")
        .args(arguments)
        .stdin(Stdio::null())
        .output()
        .expect("the local Docker OCI integration fixture must be installed")
}

fn local_oci_integration_fixture_available() -> bool {
    [
        "/usr/bin/containerd",
        "/usr/bin/ctr",
        "/usr/bin/docker",
        "/usr/bin/flock",
    ]
    .iter()
    .all(|path| Path::new(path).is_file())
}

#[test]
fn faithful_oci_layout_imports_into_a_real_private_content_store() {
    if !local_oci_integration_fixture_available() {
        eprintln!("skipping local OCI integration fixture: containerd/ctr/docker unavailable");
        return;
    }
    let mut fixture = registry_fixture();
    let fixture_id = format!("{}-good", std::process::id());
    fixture.reference = format!(
        "localhost/vonk/round5-{fixture_id}:fixture@{}",
        fixture.manifest_digest
    );
    let image_name = fixture.reference.rsplit_once('@').unwrap().0.to_owned();
    let container_name = format!("vonk-round5-{fixture_id}");
    let _cleanup = DockerCleanup {
        container: container_name.clone(),
        image: image_name.clone(),
    };
    let archive_root = tempdir().unwrap();
    let archive = archive_root.path().join("image.oci.tar");
    fs::write(&archive, oci_archive(&fixture)).unwrap();
    let store = PrivateContainerd::start();

    let imported = store.ctr(&["images", "import", "--no-unpack", archive.to_str().unwrap()]);

    assert!(
        imported.status.success(),
        "real OCI import failed: {}",
        String::from_utf8_lossy(&imported.stderr)
    );
    let images = store.ctr(&["images", "list", "--quiet"]);
    assert!(images.status.success());
    assert!(
        String::from_utf8_lossy(&images.stdout)
            .lines()
            .any(|image| image == fixture.reference.rsplit_once('@').unwrap().0)
    );
    for (digest, expected) in [
        (&fixture.manifest_digest, &fixture.manifest),
        (&fixture.config_digest, &fixture.config),
        (&fixture.layer_digest, &fixture.layer),
    ] {
        let stored = store.ctr(&["content", "get", digest]);
        assert!(stored.status.success(), "missing imported content {digest}");
        assert_eq!(&stored.stdout, expected);
    }
    let config: serde_json::Value = serde_json::from_slice(&fixture.config).unwrap();
    assert_eq!(
        config["rootfs"]["diff_ids"],
        serde_json::json!([fixture.layer_digest])
    );

    let docker_input = archive_root.path().join("image.docker.tar");
    fs::write(&docker_input, docker_archive(&fixture, &image_name)).unwrap();
    let loaded = docker(&["image", "load", "--input", docker_input.to_str().unwrap()]);
    assert!(
        loaded.status.success(),
        "real Docker graph load failed: {}",
        String::from_utf8_lossy(&loaded.stderr)
    );
    let inspected = docker(&[
        "image",
        "inspect",
        "--format",
        "{{json .RootFS.Layers}}",
        &image_name,
    ]);
    assert!(inspected.status.success());
    let diff_ids: serde_json::Value = serde_json::from_slice(&inspected.stdout).unwrap();
    assert_eq!(diff_ids, serde_json::json!([fixture.layer_digest]));
    let created = docker(&[
        "container",
        "create",
        "--platform",
        "linux/arm64",
        "--name",
        &container_name,
        &image_name,
        "/bin/true",
    ]);
    assert!(
        created.status.success(),
        "real Docker container creation failed: {}",
        String::from_utf8_lossy(&created.stderr)
    );
    let rootfs = archive_root.path().join("rootfs.tar");
    let exported = docker(&[
        "container",
        "export",
        "--output",
        rootfs.to_str().unwrap(),
        &container_name,
    ]);
    assert!(exported.status.success());
    let layer_file = tar::Archive::new(File::open(rootfs).unwrap())
        .entries()
        .unwrap()
        .find_map(|entry| {
            let mut entry = entry.unwrap();
            (entry.path().unwrap() == Path::new("fixture.txt")).then(|| {
                let mut content = Vec::new();
                entry.read_to_end(&mut content).unwrap();
                content
            })
        })
        .expect("the real loaded rootfs must contain its declared layer file");
    assert_eq!(layer_file, b"faithful OCI layer\n");
}

#[test]
fn faithful_untagged_oci_layout_loads_with_spark_podman() {
    if !Path::new("/usr/bin/podman").is_file() {
        eprintln!("skipping local Podman OCI integration fixture: podman unavailable");
        return;
    }
    let mut fixture = registry_fixture();
    let fixture_id = format!("{}-podman", std::process::id());
    fixture.reference = format!(
        "localhost/vonk/round5-{fixture_id}@{}",
        fixture.manifest_digest
    );
    let archive_root = tempdir().unwrap();
    let archive = archive_root.path().join("image.oci.tar");
    fs::write(&archive, oci_archive(&fixture)).unwrap();
    let podman_arguments = |storage: &Path, runroot: &Path| {
        vec![
            "--cgroup-manager=systemd".to_owned(),
            "--root".to_owned(),
            storage.display().to_string(),
            "--runroot".to_owned(),
            runroot.display().to_string(),
            "--storage-opt".to_owned(),
            "overlay.ignore_chown_errors=true".to_owned(),
            "--storage-opt".to_owned(),
            "overlay.mount_program=/usr/bin/fuse-overlayfs".to_owned(),
            "--storage-opt".to_owned(),
            "overlay.force_mask=shared".to_owned(),
        ]
    };

    let storage = archive_root.path().join("storage");
    let runroot = archive_root.path().join("run");
    let image_tmp = archive_root.path().join("image-tmp");
    let home = archive_root.path().join("home");
    let user_runtime = archive_root.path().join("user-runtime");
    let storage_config = archive_root.path().join("containers-storage.conf");
    fs::create_dir_all(&storage).unwrap();
    fs::create_dir_all(&runroot).unwrap();
    fs::create_dir_all(&image_tmp).unwrap();
    fs::create_dir_all(&home).unwrap();
    fs::create_dir_all(&user_runtime).unwrap();
    fs::write(
        &storage_config,
        format!(
            "[storage]\ndriver = \"overlay\"\nrunroot = \"{}\"\ngraphroot = \"{}\"\n\n[storage.options.overlay]\nmount_program = \"/usr/bin/fuse-overlayfs\"\n",
            runroot.display(),
            storage.display()
        ),
    )
    .unwrap();
    let configure_podman = |command: &mut Command| {
        command
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", &home)
            .env("XDG_DATA_HOME", &home)
            .env("XDG_RUNTIME_DIR", &user_runtime)
            .env(
                "DBUS_SESSION_BUS_ADDRESS",
                format!("unix:path={}", user_runtime.join("bus").display()),
            )
            .env("CONTAINERS_STORAGE_CONF", &storage_config)
            .env("TMPDIR", &image_tmp);
    };
    let storage_arguments = podman_arguments(&storage, &runroot);
    let mut load_arguments = storage_arguments.clone();
    load_arguments.extend(["load".to_owned(), "--quiet".to_owned()]);
    let mut load = Command::new("/usr/bin/podman");
    configure_podman(&mut load);
    let loaded = load
        .args(&load_arguments)
        .stdin(Stdio::from(File::open(&archive).unwrap()))
        .output()
        .unwrap();
    assert!(
        loaded.status.success(),
        "real rootless Podman OCI load failed: {}",
        String::from_utf8_lossy(&loaded.stderr)
    );

    let mut inspect_arguments = storage_arguments;
    inspect_arguments.extend([
        "image".to_owned(),
        "inspect".to_owned(),
        "--format".to_owned(),
        "{{.Digest}}\t{{.Os}}\t{{.Architecture}}".to_owned(),
        fixture.reference.clone(),
    ]);
    let mut inspect = Command::new("/usr/bin/podman");
    configure_podman(&mut inspect);
    let inspected = inspect
        .args(&inspect_arguments)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(
        inspected.status.success(),
        "real rootless Podman exact-reference inspect failed: {}",
        String::from_utf8_lossy(&inspected.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&inspected.stdout).trim(),
        format!("{}\tlinux\tarm64", fixture.manifest_digest)
    );
}

#[test]
fn real_private_content_store_rejects_absent_and_substituted_oci_content() {
    if !local_oci_integration_fixture_available() {
        eprintln!("skipping local OCI integration fixture: containerd/ctr/docker unavailable");
        return;
    }
    let store = PrivateContainerd::start();
    let archive_root = tempdir().unwrap();
    let absent = archive_root.path().join("absent.oci.tar");
    let missing = store.ctr(&["images", "import", "--no-unpack", absent.to_str().unwrap()]);
    assert!(!missing.status.success());

    let mut substituted = registry_fixture();
    let fixture_id = format!("{}-substituted", std::process::id());
    substituted.reference = format!(
        "localhost/vonk/round5-substituted-{fixture_id}:fixture@{}",
        substituted.manifest_digest
    );
    substituted.layer.push(b'!');
    let archive = archive_root.path().join("substituted.oci.tar");
    fs::write(&archive, oci_archive(&substituted)).unwrap();
    let imported = store.ctr(&["images", "import", "--no-unpack", archive.to_str().unwrap()]);
    let stored = store.ctr(&["content", "get", &substituted.layer_digest]);
    assert!(
        !imported.status.success()
            || !stored.status.success()
            || format!("sha256:{}", hex_sha256(&stored.stdout)) != substituted.layer_digest,
        "private OCI store resolved substituted content under the claimed digest"
    );
}

#[test]
fn build_exports_a_docker_load_archive_from_the_rootless_builder() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let runtime = tempdir().unwrap();
    let operation = Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap();

    let mut build_request = request(archive.len(), digest);
    build_request.target = Some("runtime".to_owned());
    let evidence = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(&build_request, operation, &archive)
    .unwrap();

    assert_eq!(evidence.image_digest, format!("sha256:{}", "d".repeat(64)));
    assert_eq!(evidence.image_bytes, 20);
    let calls = runner.calls.borrow();
    let load_index = calls
        .iter()
        .position(|call| call.1.iter().any(|value| value == "load"))
        .unwrap();
    let base_inspect_index = calls
        .iter()
        .position(|call| call.1.iter().any(|value| value.contains("{{.Digest}}")))
        .unwrap();
    let build_index = calls
        .iter()
        .position(|call| call.1.iter().any(|value| value == "build"))
        .unwrap();
    assert!(load_index < base_inspect_index && base_inspect_index < build_index);
    assert!(!calls[load_index].1.iter().any(|value| {
        value == "--input" || value.contains("base-images") || value.ends_with("image.oci.tar")
    }));
    assert!(
        calls
            .iter()
            .all(|call| !call.1.iter().any(|value| value == "pull"))
    );
    let build = calls
        .iter()
        .find(|call| call.1.iter().any(|value| value == "build"))
        .unwrap();
    assert_eq!(build.0, Program::SystemdRun);
    for required in [
        "--user",
        "--wait",
        "--pipe",
        "--collect",
        "--quiet",
        "--service-type=exec",
        "--unit=vonk-recipe-build-00000000-0000-4000-8000-000000000002",
        "--setenv=HOME=/var/lib/vonk-forge-agent",
        "--setenv=XDG_DATA_HOME=/var/lib/vonk-forge-agent",
        "--setenv=CONTAINERS_STORAGE_CONF=/etc/vonk-forge-agent/containers-storage.conf",
        "--property=MemoryMax=8589934592",
        "--property=CPUQuota=800%",
        "--property=TasksMax=4096",
        "--property=RuntimeMaxSec=3600s",
        "--property=TimeoutStopSec=5s",
        "--property=KillMode=control-group",
        "/usr/bin/podman",
        "--cgroup-manager=cgroupfs",
        "--runtime=/usr/bin/crun",
        "--no-cache",
        "--pull=never",
        "--cap-drop=all",
        "--cap-add=DAC_OVERRIDE",
        "--security-opt=no-new-privileges",
        "--storage-opt",
        "overlay.ignore_chown_errors=true",
        "overlay.mount_program=/usr/bin/fuse-overlayfs",
        "--ulimit=nproc=4096:4096",
        "--network=none",
        "--format=oci",
        "--identity-label=true",
        "--jobs=2",
        "--disable-compression=true",
        "--layers=true",
        "--no-hostname=false",
        "--no-hosts=false",
        "--omit-history=false",
        "--shm-size=67108864",
        "--skip-unused-stages=true",
        "--annotation",
        "org.example.annotation=present",
        "--env",
        "BUILD_MODE=release",
        "--ignorefile",
        "--label",
        "org.example.label=value",
        "--layer-label",
        "org.example.layer=value",
        "--os-feature",
        "feature-a",
        "--os-version",
        "1.0",
        "--timestamp=0",
        "--unsetenv",
        "OLD_ENV",
        "--unsetlabel",
        "org.example.old",
        "--platform",
        "linux/arm64",
        "--target",
        "runtime",
        "--root",
        "--runroot",
    ] {
        assert!(build.1.iter().any(|value| value == required), "{required}");
    }
    assert!(
        build
            .1
            .iter()
            .any(|value| value.starts_with("--setenv=TMPDIR=")
                && value.ends_with("/podman-image-tmp"))
    );
    assert!(
        build
            .1
            .iter()
            .any(|value| value.starts_with("--setenv=XDG_RUNTIME_DIR=") && value.ends_with("/xdg"))
    );
    assert!(!build.1.iter().any(|value| value == "--scope"));
    assert!(!build.1.iter().any(|value| {
        value.contains("privileged")
            || value.contains("docker.sock")
            || value.contains("podman.sock")
            || value == "--device"
            || value == "--volume"
            || value.starts_with("--cpus=")
            || value.starts_with("--cpu-period=")
            || value.starts_with("--cpu-quota=")
            || value.starts_with("--memory=")
            || value.starts_with("--pids-limit=")
    }));
    for (program, arguments) in calls.iter() {
        assert!(
            arguments.iter().any(|value| value
                == if *program == Program::SystemdRun {
                    "--cgroup-manager=cgroupfs"
                } else {
                    "--cgroup-manager=systemd"
                }),
            "the build must inherit its user-service envelope; other Podman calls use the user manager"
        );
        for option in [
            "overlay.ignore_chown_errors=true",
            "overlay.mount_program=/usr/bin/fuse-overlayfs",
            "overlay.force_mask=shared",
        ] {
            assert!(
                arguments.iter().any(|value| value == option),
                "every isolated Podman call must preserve {option}"
            );
        }
        for option in ["--root", "--runroot"] {
            let path = arguments
                .windows(2)
                .find(|pair| pair[0] == option)
                .map(|pair| Path::new(&pair[1]))
                .expect("every Podman call must use per-build storage");
            if option == "--root" {
                assert!(path.starts_with(root.path().join("build-staging")));
            } else {
                assert!(path.starts_with(runtime.path()));
                assert!(
                    path.as_os_str().len() <= 50,
                    "Podman 4.9 rejects runroot paths longer than 50 characters"
                );
            }
        }
    }
    assert!(
        Path::new(
            calls
                .iter()
                .find(|call| call.1.iter().any(|value| value == "push"))
                .unwrap()
                .1
                .last()
                .unwrap()
                .strip_prefix("docker-archive:")
                .unwrap()
        )
        .ends_with("00000000-0000-4000-8000-000000000002/image.docker.tar")
    );
    assert_eq!(
        fs::read_dir(root.path().join("build-staging"))
            .unwrap()
            .count(),
        0,
        "private Podman graphroots must not survive a completed build"
    );
}

#[test]
fn fresh_node_produces_verified_exact_digest_oci_archive_before_offline_build() {
    let mut fixture = registry_fixture();
    fixture.reference = format!("1.1.1.1/vonkforge/base@{}", fixture.manifest_digest);
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_images = vec![RecipeBuildBaseImage {
        manifest_digest: fixture.manifest_digest.clone(),
        reference: fixture.reference.clone(),
    }];
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: Some(fixture.clone()),
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000000a").unwrap(),
        &archive,
    )
    .unwrap();

    let stored = root
        .path()
        .join("base-images/sha256")
        .join(fixture.manifest_digest.strip_prefix("sha256:").unwrap())
        .join("image.oci.tar");
    let mut entries = BTreeMap::new();
    for entry in tar::Archive::new(fs::File::open(stored).unwrap())
        .entries()
        .unwrap()
    {
        let mut entry = entry.unwrap();
        let path = entry.path().unwrap().into_owned();
        let mut payload = Vec::new();
        entry.read_to_end(&mut payload).unwrap();
        entries.insert(path, payload);
    }
    assert_eq!(
        entries[Path::new("oci-layout")],
        br#"{"imageLayoutVersion":"1.0.0"}"#
    );
    assert_eq!(
        entries[Path::new(&format!(
            "blobs/sha256/{}",
            fixture.manifest_digest.strip_prefix("sha256:").unwrap()
        ))],
        fixture.manifest
    );
    assert_eq!(
        entries[Path::new(&format!(
            "blobs/sha256/{}",
            fixture.config_digest.strip_prefix("sha256:").unwrap()
        ))],
        fixture.config
    );
    assert_eq!(
        entries[Path::new(&format!(
            "blobs/sha256/{}",
            fixture.layer_digest.strip_prefix("sha256:").unwrap()
        ))],
        fixture.layer
    );
    let calls = runner.calls.borrow();
    let oras = calls
        .iter()
        .filter(|(program, _)| *program == Program::Oras)
        .collect::<Vec<_>>();
    assert_eq!(oras.len(), 3);
    let exact_remote = format!("1.1.1.1/vonkforge/base@{}", fixture.manifest_digest);
    assert!(oras[0].1.iter().any(|value| value == &exact_remote));
    assert!(oras.iter().all(|(_, arguments)| {
        arguments.iter().any(|value| value == "--resolve")
            && arguments.iter().all(|value| value != "--no-tty")
            && arguments.iter().all(|value| !value.contains("ignored-tag"))
    }));
    let load = calls
        .iter()
        .position(|(_, arguments)| arguments.iter().any(|value| value == "load"))
        .unwrap();
    let build = calls
        .iter()
        .position(|(_, arguments)| arguments.iter().any(|value| value == "build"))
        .unwrap();
    assert!(load < build);
    assert!(calls.iter().all(|(_, arguments)| {
        !arguments.iter().any(|value| value == "pull")
            && !arguments.iter().any(|value| value == "--pull")
    }));
}

#[test]
fn failed_base_image_import_uses_accounted_tmpdir_and_safe_diagnostics() {
    let cases = [
        (
            b"write /private/secret/cache: no space left on device".as_slice(),
            "temporary-storage-exhausted",
        ),
        (
            b"potentially insufficient UIDs or GIDs; inspect /etc/subuid".as_slice(),
            "subordinate-id-mapping-unavailable",
        ),
        (
            b"payload does not match any of the supported image formats: oci-archive".as_slice(),
            "archive-format-rejected",
        ),
        (
            b"open /private/secret/archive: permission denied".as_slice(),
            "permission-denied",
        ),
        (
            b"opaque failure containing /private/secret".as_slice(),
            "unclassified-podman-load-failure",
        ),
    ];
    for (stderr, diagnostic) in cases {
        let (archive, digest) = bundle();
        let request = request(archive.len(), digest);
        let root = tempdir().unwrap();
        stage_base_archive(root.path());
        let runtime = tempdir().unwrap();
        let runner = FailedImportRunner {
            inner: Runner {
                calls: RefCell::new(Vec::new()),
                fail_build: false,
                oversize_base: false,
                registry: None,
                substitute_base: false,
            },
            stderr: stderr.to_vec(),
            temporary_directory: RefCell::new(None),
            monitored_directory: RefCell::new(None),
            maximum_bytes: Cell::new(0),
        };

        let error = RecipeBuilder {
            runner: &runner,
            data_root: root.path(),
            runtime_root: runtime.path(),
            egress_binary: Path::new("/bin/true"),
        }
        .build(
            &request,
            Uuid::parse_str("00000000-0000-4000-8000-00000000003a").unwrap(),
            &archive,
        )
        .unwrap_err();

        assert_eq!(
            error.to_string(),
            format!("Podman could not import the verified base image ({diagnostic})")
        );
        assert!(!error.to_string().contains("private"));
        assert!(!error.to_string().contains("secret"));
        let temporary_directory = runner.temporary_directory.borrow();
        let temporary_directory = temporary_directory.as_ref().unwrap();
        let monitored_directory = runner.monitored_directory.borrow();
        let monitored_directory = monitored_directory.as_ref().unwrap();
        assert!(temporary_directory.starts_with(root.path().join("build-staging")));
        assert!(monitored_directory.starts_with(root.path().join("build-staging")));
        assert_eq!(
            runner.maximum_bytes.get(),
            request.base_image_storage_bytes
                + request.limits.temporary_bytes
                + request.source_bundle_bytes
        );
    }
}

#[test]
fn fresh_node_retries_a_failed_manifest_transfer() {
    let fixture = registry_fixture();
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_images = vec![RecipeBuildBaseImage {
        manifest_digest: fixture.manifest_digest.clone(),
        reference: fixture.reference.clone(),
    }];
    let runner = RetryManifestRunner {
        inner: Runner {
            calls: RefCell::new(Vec::new()),
            fail_build: false,
            oversize_base: false,
            registry: Some(fixture),
            substitute_base: false,
        },
        remaining_failures: Cell::new(1),
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000002c").unwrap(),
        &archive,
    )
    .unwrap();

    assert_eq!(runner.remaining_failures.get(), 0);
}

#[test]
fn fresh_node_reports_manifest_stage_after_bounded_retries() {
    let fixture = registry_fixture();
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_images = vec![RecipeBuildBaseImage {
        manifest_digest: fixture.manifest_digest.clone(),
        reference: fixture.reference.clone(),
    }];
    let runner = RetryManifestRunner {
        inner: Runner {
            calls: RefCell::new(Vec::new()),
            fail_build: false,
            oversize_base: false,
            registry: Some(fixture),
            substitute_base: false,
        },
        remaining_failures: Cell::new(3),
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000002d").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::BaseImageManifest));
    assert_eq!(runner.remaining_failures.get(), 0);
}

#[test]
fn repeated_identical_base_image_layer_is_materialized_once() {
    let fixture = duplicate_layer_registry_fixture();
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_images = vec![RecipeBuildBaseImage {
        manifest_digest: fixture.manifest_digest.clone(),
        reference: fixture.reference.clone(),
    }];
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: Some(fixture.clone()),
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000002a").unwrap(),
        &archive,
    )
    .unwrap();

    let stored = root
        .path()
        .join("base-images/sha256")
        .join(fixture.manifest_digest.strip_prefix("sha256:").unwrap())
        .join("image.oci.tar");
    let layer_path = format!(
        "blobs/sha256/{}",
        fixture.layer_digest.strip_prefix("sha256:").unwrap()
    );
    let layer_entries = tar::Archive::new(fs::File::open(stored).unwrap())
        .entries()
        .unwrap()
        .map(|entry| entry.unwrap().path().unwrap().into_owned())
        .filter(|path| path == Path::new(&layer_path))
        .count();
    assert_eq!(layer_entries, 1);
    let oras = runner
        .calls
        .borrow()
        .iter()
        .filter(|(program, _)| *program == Program::Oras)
        .count();
    assert_eq!(
        oras, 3,
        "manifest, config, and repeated layer are each fetched once"
    );
}

#[test]
fn conflicting_repeated_base_image_layer_is_rejected_before_blob_fetch() {
    let fixture = conflicting_duplicate_layer_registry_fixture();
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_images = vec![RecipeBuildBaseImage {
        manifest_digest: fixture.manifest_digest.clone(),
        reference: fixture.reference.clone(),
    }];
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: Some(fixture),
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000002b").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::BaseImageManifest));
    let oras = runner
        .calls
        .borrow()
        .iter()
        .filter(|(program, _)| *program == Program::Oras)
        .count();
    assert_eq!(oras, 1, "only the conflicting manifest may be fetched");
}

#[test]
fn base_image_producer_rejects_declared_archive_above_bound_before_blob_fetch() {
    let fixture = registry_fixture();
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_image_storage_bytes = oci_archive(&fixture).len() as u64 - 1;
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: Some(fixture),
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000001a").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::OutputLimit));
    let calls = runner.calls.borrow();
    let oras = calls
        .iter()
        .filter(|(program, _)| *program == Program::Oras)
        .collect::<Vec<_>>();
    assert_eq!(oras.len(), 1, "only the bounded manifest may be fetched");
    assert!(oras[0].1.iter().any(|value| value == "manifest"));
    assert!(!base_archive_path(root.path()).exists());
}

#[test]
fn base_image_storage_rejects_symlinked_data_and_supply_roots() {
    let (archive, digest) = bundle();
    let runtime = tempdir().unwrap();

    let real_data = tempdir().unwrap();
    stage_base_archive(real_data.path());
    let linked_parent = tempdir().unwrap();
    let linked_data = linked_parent.path().join("agent-data");
    symlink(real_data.path(), &linked_data).unwrap();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let error = RecipeBuilder {
        runner: &runner,
        data_root: &linked_data,
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest.clone()),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000b").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::BaseImageContent));
    assert!(runner.calls.borrow().is_empty());

    let data = tempdir().unwrap();
    let external = tempdir().unwrap();
    stage_base_archive(external.path());
    symlink(
        external.path().join("base-images"),
        data.path().join("base-images"),
    )
    .unwrap();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let error = RecipeBuilder {
        runner: &runner,
        data_root: data.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000c").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::BaseImageContent));
    assert!(runner.calls.borrow().is_empty());
}

#[test]
fn base_image_storage_rejects_symlinked_digest_directory_and_archive() {
    let (archive, digest) = bundle();
    let runtime = tempdir().unwrap();

    let data = tempdir().unwrap();
    let external = tempdir().unwrap();
    stage_base_archive(external.path());
    let digest_name = registry_fixture()
        .manifest_digest
        .strip_prefix("sha256:")
        .unwrap()
        .to_owned();
    fs::create_dir_all(data.path().join("base-images/sha256")).unwrap();
    symlink(
        external
            .path()
            .join("base-images/sha256")
            .join(&digest_name),
        data.path().join("base-images/sha256").join(&digest_name),
    )
    .unwrap();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let error = RecipeBuilder {
        runner: &runner,
        data_root: data.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest.clone()),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000d").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::BaseImageContent));

    let data = tempdir().unwrap();
    let external_archive = data.path().join("external.oci.tar");
    fs::write(&external_archive, oci_archive(&registry_fixture())).unwrap();
    let archive_path = base_archive_path(data.path());
    fs::create_dir_all(archive_path.parent().unwrap()).unwrap();
    symlink(&external_archive, &archive_path).unwrap();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let error = RecipeBuilder {
        runner: &runner,
        data_root: data.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000e").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::BaseImageContent));
}

#[test]
fn base_image_storage_rejects_digest_path_escape_before_registry_or_podman() {
    let (archive, digest) = bundle();
    let mut build_request = request(archive.len(), digest);
    build_request.base_images[0].manifest_digest = "sha256:../../escape".to_owned();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000000f").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::BaseImageContent));
    assert!(runner.calls.borrow().is_empty());
}

struct ReplacementRaceRunner {
    archive_path: std::path::PathBuf,
    inner: Runner,
    loaded: RefCell<Vec<u8>>,
}

impl ProcessRunner for ReplacementRaceRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.inner.run(program, arguments, timeout)
    }

    fn run_bounded_directory_with_input(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        input: &File,
        _directory: &Path,
        _maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        if program == Program::Podman && arguments.iter().any(|value| value == "load") {
            let replaced = self.archive_path.with_extension("verified");
            fs::rename(&self.archive_path, &replaced)?;
            fs::write(&self.archive_path, b"substituted after verification")?;
            let mut held = input.try_clone()?;
            held.seek(SeekFrom::Start(0))?;
            held.read_to_end(&mut self.loaded.borrow_mut())?;
        }
        self.inner.run(program, arguments, timeout)
    }
}

#[test]
fn base_image_consumer_holds_verified_descriptor_across_path_replacement() {
    let (archive, digest) = bundle();
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let archive_path = base_archive_path(root.path());
    let verified = fs::read(&archive_path).unwrap();
    let runner = ReplacementRaceRunner {
        archive_path: archive_path.clone(),
        inner: Runner {
            calls: RefCell::new(Vec::new()),
            fail_build: false,
            oversize_base: false,
            registry: None,
            substitute_base: false,
        },
        loaded: RefCell::new(Vec::new()),
    };
    let runtime = tempdir().unwrap();

    RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-000000000010").unwrap(),
        &archive,
    )
    .unwrap();

    assert_eq!(*runner.loaded.borrow(), verified);
    assert_eq!(
        fs::read(archive_path).unwrap(),
        b"substituted after verification"
    );
}

#[test]
fn base_image_archive_rejects_a_layer_substituted_under_the_exact_manifest() {
    let (archive, digest) = bundle();
    let root = tempdir().unwrap();
    let mut fixture = registry_fixture();
    fixture.layer.push(b'!');
    let archive_path = base_archive_path(root.path());
    fs::create_dir_all(archive_path.parent().unwrap()).unwrap();
    fs::write(&archive_path, oci_archive(&fixture)).unwrap();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-000000000011").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::BaseImageArchive));
    assert!(
        !runner
            .calls
            .borrow()
            .iter()
            .any(|(_, arguments)| arguments.iter().any(|value| value == "load"))
    );
}

#[test]
fn build_removes_readonly_private_graphroot_after_process_failure() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: true,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let runtime = tempdir().unwrap();
    let operation = Uuid::parse_str("00000000-0000-4000-8000-000000000004").unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(&request(archive.len(), digest), operation, &archive)
    .unwrap_err();

    assert!(matches!(
        error,
        RecipeBuildError::ImageBuild {
            diagnostic: vonk_agent::recipe_builder::PodmanBuildDiagnostic::NonzeroWithoutOutput
        }
    ));
    assert_eq!(
        error.to_string(),
        "Podman recipe image build failed (nonzero-without-output)"
    );
    assert_eq!(
        fs::read_dir(root.path().join("build-staging"))
            .unwrap()
            .count(),
        0,
        "private Podman graphroots must not survive a failed build"
    );
}

#[test]
fn build_routes_declared_public_hosts_through_an_ephemeral_internal_proxy() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let runtime = tempdir().unwrap();
    let operation = Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap();
    let mut build_request = request(archive.len(), digest);
    build_request.network = RecipeBuildNetwork {
        mode: "public".to_owned(),
        hosts: vec!["pypi.org".to_owned()],
    };

    RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(&build_request, operation, &archive)
    .unwrap();

    let calls = runner.calls.borrow();
    let proxy = calls
        .iter()
        .find(|(_, arguments)| arguments.iter().any(|value| value == "run"))
        .unwrap();
    assert!(
        proxy
            .1
            .windows(2)
            .any(|pair| pair == ["--allow-host", "pypi.org"])
    );
    assert!(proxy.1.iter().any(|value| value == "--read-only"));
    assert!(proxy.1.iter().any(|value| value == "--cap-drop=all"));
    let build = calls
        .iter()
        .find(|(_, arguments)| arguments.iter().any(|value| value == "build"))
        .unwrap();
    assert!(
        build
            .1
            .iter()
            .any(|value| value.starts_with("--network=vonk-build-in-"))
    );
    assert!(
        build
            .1
            .iter()
            .any(|value| value.starts_with("HTTP_PROXY=http://vonk-build-proxy-"))
    );
    assert!(
        calls
            .iter()
            .any(|(_, arguments)| arguments.windows(2).any(|pair| pair == ["network", "rm"]))
    );
}

#[test]
fn build_rejects_recipe_proxy_argument_override_before_starting_the_boundary() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let runtime = tempdir().unwrap();
    let mut build_request = request(archive.len(), digest);
    build_request.network = RecipeBuildNetwork {
        mode: "public".to_owned(),
        hosts: vec!["pypi.org".to_owned()],
    };
    build_request.arguments[0].name = "HTTPS_PROXY".to_owned();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::NetworkPolicy));
}

#[test]
fn public_build_cancellation_stops_work_and_removes_the_egress_boundary() {
    let (archive, digest) = bundle();
    let runner = CancellingRunner {
        inner: Runner {
            calls: RefCell::new(Vec::new()),
            fail_build: false,
            oversize_base: false,
            registry: None,
            substitute_base: false,
        },
        cancelled: Cell::new(false),
    };
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let runtime = tempdir().unwrap();
    let operation = Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap();
    let mut build_request = request(archive.len(), digest);
    build_request.network = RecipeBuildNetwork {
        mode: "public".to_owned(),
        hosts: vec!["pypi.org".to_owned()],
    };

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build_cancellable(&build_request, operation, &archive, &|| {
        runner.cancelled.get()
    })
    .unwrap_err();

    assert!(matches!(
        error,
        RecipeBuildError::Process(ProcessError::Cancelled)
    ));
    let calls = runner.inner.calls.borrow();
    assert!(calls.iter().any(|(_, arguments)| {
        arguments
            .windows(2)
            .any(|pair| pair == ["stop", "--time=1"])
    }));
    assert!(
        calls
            .iter()
            .any(|(_, arguments)| arguments.windows(2).any(|pair| pair == ["network", "rm"]))
    );
    assert!(
        !root
            .path()
            .join("build-staging")
            .read_dir()
            .unwrap()
            .any(|_| true)
    );
}

#[test]
fn build_rejects_a_docker_archive_larger_than_declared_output_limit() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let runtime = tempdir().unwrap();
    let operation = Uuid::parse_str("00000000-0000-4000-8000-000000000003").unwrap();
    let mut build_request = request(archive.len(), digest);
    build_request.limits.output_bytes = 8;

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(&build_request, operation, &archive)
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::OutputLimit));
}

#[test]
fn build_fails_closed_when_declared_base_archive_is_absent() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-000000000005").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::BaseImageManifest));
    assert!(
        runner
            .calls
            .borrow()
            .iter()
            .any(|call| call.0 == Program::Oras)
    );
    assert!(
        !runner
            .calls
            .borrow()
            .iter()
            .any(|call| call.1.contains(&"build".to_owned()))
    );
}

#[test]
fn build_rejects_substituted_base_before_offline_build() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: true,
    };
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-000000000006").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::BaseImageInspect));
    let calls = runner.calls.borrow();
    assert!(calls.iter().any(|call| call.1.contains(&"load".to_owned())));
    assert!(
        !calls
            .iter()
            .any(|call| call.1.contains(&"build".to_owned()))
    );
}

#[test]
fn base_import_is_bounded_before_offline_build() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: true,
        registry: None,
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let runtime = tempdir().unwrap();
    let mut build_request = request(archive.len(), digest);
    build_request.base_image_storage_bytes = 100;

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-000000000007").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::OutputLimit));
    assert!(
        !runner
            .calls
            .borrow()
            .iter()
            .any(|call| call.1.contains(&"build".to_owned()))
    );
}
