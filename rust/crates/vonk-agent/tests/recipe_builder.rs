#![forbid(unsafe_code)]

use std::{
    cell::RefCell, collections::BTreeMap, fs, io::Cursor, os::unix::fs::PermissionsExt, path::Path,
    time::Duration,
};

use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::{
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    recipe_builder::{RecipeBuildError, RecipeBuilder},
};
use vonk_agent_protocol::{
    RecipeBuildArgument, RecipeBuildLimits, RecipeBuildNetwork, RecipeBuildRequest, canonical_json,
    hex_sha256,
};

struct Runner {
    calls: RefCell<Vec<(Program, Vec<String>)>>,
    fail_build: bool,
}

impl ProcessRunner for Runner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.calls.borrow_mut().push((program, arguments.to_vec()));
        let stdout = if arguments.iter().any(|value| value == "inspect") {
            b"linux\tarm64\tv1\t10001:10001\n".to_vec()
        } else {
            Vec::new()
        };
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
    let dockerfile = format!(
        "FROM ghcr.io/vonkforge/base@sha256:{}\nUSER 10001:10001\n",
        "a".repeat(64)
    );
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
    RecipeBuildRequest {
        arguments: vec![RecipeBuildArgument {
            name: "runtime-version".to_owned(),
            value: serde_json::json!("1"),
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

#[test]
fn build_exports_a_docker_load_archive_from_the_rootless_builder() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
    };
    let root = tempdir().unwrap();
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
    let build = &calls[0];
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
            calls[2]
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
fn build_removes_readonly_private_graphroot_after_process_failure() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: true,
    };
    let root = tempdir().unwrap();
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
    };
    let root = tempdir().unwrap();
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
