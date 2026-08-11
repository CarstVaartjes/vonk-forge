#![forbid(unsafe_code)]

use std::{cell::RefCell, collections::BTreeMap, fs, io::Cursor, path::Path, time::Duration};

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
                .strip_prefix("oci-archive:")
                .unwrap();
            fs::write(output, b"exact oci archive")?;
        }
        Ok(ProcessOutput {
            success: true,
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
fn build_uses_only_typed_rootless_podman_arguments_and_records_exact_layout() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
    };
    let root = tempdir().unwrap();
    let operation = Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap();

    let evidence = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
    }
    .build(&request(archive.len(), digest), operation, &archive)
    .unwrap();

    assert_eq!(evidence.image_digest, format!("sha256:{}", "d".repeat(64)));
    assert_eq!(evidence.image_bytes, 17);
    let calls = runner.calls.borrow();
    let build = &calls[0];
    assert_eq!(build.0, Program::Podman);
    for required in [
        "--no-cache",
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
        for option in [
            "overlay.ignore_chown_errors=true",
            "overlay.mount_program=/usr/bin/fuse-overlayfs",
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
            assert!(path.starts_with(root.path().join("build-staging")));
        }
    }
    assert!(
        Path::new(
            calls[2]
                .1
                .last()
                .unwrap()
                .strip_prefix("oci-archive:")
                .unwrap()
        )
        .ends_with("00000000-0000-4000-8000-000000000002/image.oci.tar")
    );
}

#[test]
fn build_rejects_declared_public_hosts_before_running_podman() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
    };
    let root = tempdir().unwrap();
    let operation = Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap();
    let mut build_request = request(archive.len(), digest);
    build_request.network = RecipeBuildNetwork {
        mode: "public".to_owned(),
        hosts: vec!["pypi.org".to_owned()],
    };

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
    }
    .build(&build_request, operation, &archive)
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::NetworkPolicy));
    assert!(runner.calls.borrow().is_empty());
}

#[test]
fn build_rejects_an_oci_layout_larger_than_declared_output_limit() {
    let (archive, digest) = bundle();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
    };
    let root = tempdir().unwrap();
    let operation = Uuid::parse_str("00000000-0000-4000-8000-000000000003").unwrap();
    let mut build_request = request(archive.len(), digest);
    build_request.limits.output_bytes = 8;

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
    }
    .build(&build_request, operation, &archive)
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::OutputLimit));
}
