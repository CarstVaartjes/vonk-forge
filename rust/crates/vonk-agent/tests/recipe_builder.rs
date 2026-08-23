#![forbid(unsafe_code)]

use std::{
    cell::RefCell,
    collections::BTreeMap,
    fs::{self, File},
    io::{Cursor, Read, Seek, SeekFrom},
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::Path,
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
    RecipeBuildArgument, RecipeBuildBaseImage, RecipeBuildLimits, RecipeBuildNetwork,
    RecipeBuildRequest, canonical_json, hex_sha256,
};

struct Runner {
    calls: RefCell<Vec<(Program, Vec<String>)>>,
    fail_build: bool,
    oversize_base: bool,
    registry: Option<OciRegistryFixture>,
    substitute_base: bool,
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
        platform: "linux/arm64".to_owned(),
        recipe_content_sha256: "a".repeat(64),
        recipe_revision_id: Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
        schema_version: 1,
        source_bundle_bytes: bundle_bytes as u64,
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
                "version = 3\n[grpc]\n  uid = {}\n  gid = {}\n[ttrpc]\n  uid = {}\n  gid = {}\n",
                metadata.uid(),
                metadata.gid(),
                metadata.uid(),
                metadata.gid(),
            ),
        )
        .unwrap();
        let socket = root.path().join("containerd.sock");
        let mut child = Command::new("/usr/bin/containerd")
            .args([
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
            .stderr(Stdio::null())
            .spawn()
            .expect("the local containerd integration fixture must be installed");
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                panic!("private containerd exited before readiness: {status}");
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
            assert!(
                Instant::now() < deadline,
                "private containerd did not become ready"
            );
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
    ["/usr/bin/containerd", "/usr/bin/ctr", "/usr/bin/docker"]
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

    let evidence = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
    }
    .build(&request(archive.len(), digest), operation, &archive)
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
    assert_eq!(build.0, Program::Podman);
    for required in [
        "--cgroup-manager=systemd",
        "--no-cache",
        "--pull=never",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--storage-opt",
        "overlay.ignore_chown_errors=true",
        "overlay.mount_program=/usr/bin/fuse-overlayfs",
        "--cpu-period=100000",
        "--cpu-quota=800000",
        "--memory=8589934592b",
        "--ulimit=nproc=4096:4096",
        "--network=none",
        "--platform",
        "linux/arm64",
        "--root",
        "--runroot",
    ] {
        assert!(build.1.iter().any(|value| value == required), "{required}");
    }
    assert!(!build.1.iter().any(|value| {
        value.contains("privileged")
            || value.contains("docker.sock")
            || value.contains("podman.sock")
            || value == "--device"
            || value == "--volume"
            || value.starts_with("--cpus=")
            || value.starts_with("--pids-limit=")
    }));
    for (_, arguments) in calls.iter() {
        assert!(
            arguments
                .iter()
                .any(|value| value == "--cgroup-manager=systemd"),
            "every isolated Podman call must use the rootless user systemd manager"
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
    let fixture = registry_fixture();
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
    }
    .build(
        &request(archive.len(), digest.clone()),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000b").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::Evidence));
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
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000c").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::Evidence));
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
    }
    .build(
        &request(archive.len(), digest.clone()),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000d").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::Evidence));

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
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000e").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::Evidence));
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
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000000f").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::Evidence));
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
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-000000000011").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::Evidence));
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
    }
    .build(&request(archive.len(), digest), operation, &archive)
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::Evidence));
    assert_eq!(
        fs::read_dir(root.path().join("build-staging"))
            .unwrap()
            .count(),
        0,
        "private Podman graphroots must not survive a failed build"
    );
}

#[test]
fn build_rejects_declared_public_hosts_before_running_podman() {
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
    }
    .build(&build_request, operation, &archive)
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::NetworkPolicy));
    assert!(runner.calls.borrow().is_empty());
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
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-000000000005").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::Evidence));
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
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-000000000006").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::Evidence));
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
