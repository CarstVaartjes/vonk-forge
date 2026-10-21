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
    process::{ProcessDiskReserve, ProcessError, ProcessOutput, ProcessRunner, Program},
    recipe_builder::RecipeBuilder,
};
use vonk_agent_protocol::{
    RecipeBuildAdapter, RecipeBuildAdapterDefinition, RecipeBuildAdditionalContext,
    RecipeBuildBaseImage, RecipeBuildLimits, RecipeBuildMetadata, RecipeBuildNetwork,
    RecipeBuildOptions, RecipeBuildRequest, canonical_json, hex_sha256,
};

fn fresh_build(root: &Path, runtime: &Path) {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: Some(registry_fixture()),
        substitute_base: false,
    };
    RecipeBuilder {
        runner: &runner,
        data_root: root,
        runtime_root: runtime,
        egress_binary: Path::new("/bin/true"),
    }
    .build(&request(archive.len(), digest), Uuid::new_v4(), &archive)
    .unwrap();
}

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
    minimum_free_bytes: Cell<u64>,
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

    fn run_with_input_disk_reserve_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
        input: &File,
        reserve: ProcessDiskReserve<'_>,
        _cancelled: &dyn Fn() -> bool,
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
        assert_eq!(temporary_directory.parent(), Some(reserve.filesystem()));
        assert!(input.metadata()?.len() > 0);
        self.temporary_directory
            .borrow_mut()
            .replace(temporary_directory);
        self.monitored_directory
            .borrow_mut()
            .replace(reserve.filesystem().to_path_buf());
        self.minimum_free_bytes.set(reserve.minimum_free_bytes());
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

struct DeadlineRunner {
    inner: Runner,
    timeouts: RefCell<Vec<(String, Duration)>>,
}

impl DeadlineRunner {
    fn record(&self, arguments: &[String], timeout: Duration) {
        // The adaptation is a second, ordered build stage. Name it separately
        // so the deadline test proves the adapter stage shares the one recipe
        // deadline instead of recording two indistinguishable builds.
        let phase = if arguments.iter().any(|value| value == "load") {
            "load"
        } else if arguments
            .iter()
            .any(|value| value.starts_with("--unit=vonk-runtime-adapter-"))
        {
            "adapt"
        } else if arguments.iter().any(|value| value == "build") {
            "build"
        } else if arguments.iter().any(|value| value == "push") {
            "push"
        } else {
            return;
        };
        self.timeouts.borrow_mut().push((phase.to_owned(), timeout));
    }
}

impl ProcessRunner for DeadlineRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.inner.run(program, arguments, timeout)
    }

    fn run_with_disk_reserve_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        _reserve: ProcessDiskReserve<'_>,
        _cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        self.record(arguments, timeout);
        self.inner.run(program, arguments, timeout)
    }

    fn run_with_input_disk_reserve_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        _input: &File,
        _reserve: ProcessDiskReserve<'_>,
        _cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        self.record(arguments, timeout);
        self.inner.run(program, arguments, timeout)
    }
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
        let stdout = if program == Program::Getent {
            b"1.1.1.1 STREAM fixture\n".to_vec()
        } else if let Some(payload) = registry_stdout {
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
        } else if arguments
            .iter()
            .any(|value| value.contains(".NetworkSettings.Networks"))
        {
            b"10.89.0.2\n".to_vec()
        } else if arguments.iter().any(|value| value == "inspect") {
            inspect_fixture(arguments.last().map(String::as_str).unwrap_or_default())
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
        let mut success = !(self.fail_build && arguments.iter().any(|value| value == "build"));
        if arguments.iter().any(|value| value == "inspect")
            && arguments
                .last()
                .is_some_and(|value| value.starts_with("localhost/vonk/runtime-adapter-"))
        {
            let storage = arguments
                .windows(2)
                .find(|pair| pair[0] == "--root")
                .map(|pair| Path::new(&pair[1]))
                .unwrap();
            success = storage.join("adapter-complete").exists();
        }
        if success
            && arguments
                .iter()
                .any(|value| value.starts_with("--unit=vonk-runtime-adapter-"))
        {
            let storage = arguments
                .windows(2)
                .find(|pair| pair[0] == "--root")
                .map(|pair| Path::new(&pair[1]))
                .unwrap();
            fs::write(storage.join("adapter-complete"), b"completed mock image")?;
        }
        Ok(ProcessOutput {
            success,
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
    bundle_contents(&[("Dockerfile", dockerfile.as_bytes())])
}

fn bundle_contents(files: &[(&str, &[u8])]) -> (Vec<u8>, String) {
    let mut payload = Vec::new();
    let mut manifest = Vec::new();
    let mut total = 0_u64;
    {
        let mut archive = tar::Builder::new(&mut payload);
        for (path, content) in files {
            let mut header = tar::Header::new_ustar();
            header.set_path(path).unwrap();
            header.set_size(content.len() as u64);
            header.set_mode(0o644);
            header.set_uid(0);
            header.set_gid(0);
            header.set_mtime(0);
            header.set_cksum();
            archive.append(&header, Cursor::new(content)).unwrap();
            total += content.len() as u64;
            manifest.push(vonk_agent_protocol::generated::SourceBundleFile {
                mode: 420_i64.try_into().unwrap(),
                path: (*path).into(),
                sha256: hex_sha256(content),
                size: (content.len() as u64).try_into().unwrap(),
            });
        }
        archive.finish().unwrap();
    }
    manifest.sort_by(|left, right| left.path.cmp(&right.path));
    let manifest = vonk_agent_protocol::generated::SourceBundleDigestManifest {
        files: manifest,
        schema_version: 1,
        total_bytes: total.try_into().unwrap(),
    };
    (payload, hex_sha256(&canonical_json(&manifest).unwrap()))
}

fn adapter_fixture() -> RecipeBuildAdapter {
    let definition = RecipeBuildAdapterDefinition {
        adapter_id: "vonk.runtime-contract.vllm.v1".to_owned(),
        containerfile: "ARG VONK_RECIPE_IMAGE\nFROM ${VONK_RECIPE_IMAGE}\n".to_owned(),
        engine: "vllm".to_owned(),
        image_user: "10001:10001".to_owned(),
    };
    let adapter_sha256 = hex_sha256(&canonical_json(&definition).unwrap());
    RecipeBuildAdapter {
        adapter_sha256,
        definition,
    }
}

/// The two references one build produces: the recipe image is the adaptation
/// stage's input, and the adapted image is the exported artifact.
const RECIPE_BUILD_TAG_PREFIX: &str = "localhost/vonk/recipe-build-";
const ADAPTED_BUILD_TAG_PREFIX: &str = "localhost/vonk/runtime-adapter-";

fn recipe_build_tag(operation: Uuid) -> String {
    format!("{RECIPE_BUILD_TAG_PREFIX}{operation}")
}

fn adapted_build_tag(operation: Uuid) -> String {
    format!("{ADAPTED_BUILD_TAG_PREFIX}{operation}")
}

/// Scripted `podman image inspect` output for one build reference.
///
/// The recipe image legitimately lacks the adapter labels, so its template
/// fields render as Go's `<no value>`. The adapted image is the one that must
/// carry the interface label, the resolved adapter identity and the adapter's
/// runtime user; returning those only for the adapted reference is what lets
/// the tests distinguish an applied adaptation from a skipped one.
fn inspect_fixture(reference: &str) -> Vec<u8> {
    if reference.starts_with(ADAPTED_BUILD_TAG_PREFIX) {
        let adapter = adapter_fixture();
        format!(
            "linux\tarm64\tv1\t{}\t{}\t{}\n",
            adapter.definition.adapter_id, adapter.adapter_sha256, adapter.definition.image_user
        )
        .into_bytes()
    } else {
        b"linux\tarm64\t<no value>\t<no value>\t<no value>\t10001:10001\n".to_vec()
    }
}

fn request(bundle_bytes: usize, digest: String) -> RecipeBuildRequest {
    let base = registry_fixture();
    RecipeBuildRequest {
        adapter: adapter_fixture(),
        base_image_storage_bytes: 64 * 1024 * 1024,
        base_images: vec![RecipeBuildBaseImage {
            manifest_digest: base.manifest_digest,
            reference: base.reference,
        }],
        capabilities: vec!["DAC_OVERRIDE".to_owned()],
        build_id: Uuid::parse_str("00000000-0000-4000-8000-000000000009").unwrap(),
        build_input_sha256: "c".repeat(64),
        dockerfile: "Dockerfile".to_owned(),
        limits: RecipeBuildLimits {
            cpu_cores: 8,
            memory_bytes: 8 * 1024 * 1024 * 1024,
            output_bytes: 64 * 1024 * 1024,
            processes: 4096,
            temporary_bytes: 64 * 1024 * 1024,
            timeout_seconds: 3600,
        },
        network: RecipeBuildNetwork { hosts: Vec::new() },
        options: RecipeBuildOptions {
            additional_contexts: vec![RecipeBuildAdditionalContext {
                name: "assets".to_owned(),
                path: "assets".to_owned(),
            }],
            annotations: vec![RecipeBuildMetadata {
                name: "org.example.annotation".to_owned(),
                value: "present".to_owned(),
            }],
            environment: vec![
                vonk_agent_protocol::generated::RecipeBuildEnvironmentArgument {
                    name: "BUILD_MODE".to_owned(),
                    value:
                        vonk_agent_protocol::generated::RecipeBuildEnvironmentArgumentValue::String(
                            "release".to_owned(),
                        ),
                },
            ],
            format: "oci".parse().unwrap(),
            identity_label: true,
            ignorefile: Some(".containerignore".to_owned()),
            jobs: 2,
            labels: vec![RecipeBuildMetadata {
                name: "org.example.label".to_owned(),
                value: "value".to_owned(),
            }],
            layer_compression: "disabled".parse().unwrap(),
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
            squash: "none".parse().unwrap(),
            timestamp: Some(0),
            unset_environment: vec!["OLD_ENV".to_owned()],
            unset_labels: vec!["org.example.old".to_owned()],
        },
        recipe_content_sha256: "a".repeat(64),
        recipe_revision_id: Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
        source_bundle_bytes: u32::try_from(bundle_bytes).unwrap(),
        source_bundle_sha256: digest,
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

mod archives;
mod base_images;
mod build;
mod egress;
mod storage;
