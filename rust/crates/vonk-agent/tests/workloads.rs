#![forbid(unsafe_code)]

use std::{
    cell::RefCell,
    collections::VecDeque,
    fs,
    io::{Read, Write},
    net::{IpAddr, TcpListener},
    os::unix::fs::symlink,
    path::Path,
    thread,
    time::Duration,
};

use sha2::{Digest, Sha256};
use tempfile::tempdir;
use vonk_agent::{
    oci::OciRuntime,
    process::{ProcessError, ProcessOutput, ProcessRunner, Program, SystemProcessRunner},
    workloads::{
        ArgumentValue, ArtifactMountSpec, ArtifactSpec, EndpointSpec, LifecycleSpec,
        ModelDependencySpec, MountSpec, Placement, PlacementEnvironmentSpec, RuntimeArgument,
        RuntimeEnvironment, RuntimeSpec, SecuritySpec, TopologySpec, WorkloadIdentitySpec,
        WorkloadSpec,
    },
};

const DIGEST: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const DS4_FILE: &str =
    "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf";
const DS4_DRAFTER_FILE: &str = "DeepSeek-V4-Flash-DSpark-support-0731.gguf";

struct FakeRunner {
    calls: RefCell<Vec<(Program, Vec<String>)>>,
    outputs: RefCell<VecDeque<ProcessOutput>>,
}

impl ProcessRunner for FakeRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.calls.borrow_mut().push((program, arguments.to_vec()));
        if program == Program::Curl {
            let destination = arguments
                .windows(2)
                .find(|values| values[0] == "--output")
                .map(|values| &values[1])
                .unwrap();
            if destination.ends_with(".huggingface-model.json") {
                fs::write(
                    destination,
                    br#"{"siblings":[{"rfilename":"weights.bin","lfs":{"sha256":"9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"}}]}"#,
                )?;
            } else {
                fs::write(destination, b"weights")?;
            }
        }
        Ok(self.outputs.borrow_mut().pop_front().unwrap())
    }
}

struct BudgetRunner {
    inner: FakeRunner,
    budgets: RefCell<Vec<u64>>,
}

struct ObservationRunner {
    calls: RefCell<Vec<(Program, Vec<String>)>>,
    podman_outputs: RefCell<VecDeque<ProcessOutput>>,
}

impl ProcessRunner for ObservationRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.calls.borrow_mut().push((program, arguments.to_vec()));
        if program == Program::Podman {
            return Ok(self.podman_outputs.borrow_mut().pop_front().unwrap());
        }
        SystemProcessRunner.run(program, arguments, timeout)
    }
}

impl ProcessRunner for BudgetRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.inner.run(program, arguments, timeout)
    }

    fn run_bounded_directory(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        directory: &Path,
        maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        self.budgets.borrow_mut().push(maximum_bytes);
        fs::write(directory.join("artifact"), b"weights")?;
        self.inner.run(program, arguments, timeout)
    }
}

fn spec() -> WorkloadSpec {
    WorkloadSpec {
        identity: WorkloadIdentitySpec {
            recipe_revision_sha256: DIGEST.to_owned(),
            model_version_sha256: DIGEST.to_owned(),
            harness_sha256: DIGEST.to_owned(),
            runtime_distribution_sha256: DIGEST.to_owned(),
            patch_bundle_sha256: None,
        },
        model_dependencies: Vec::<ModelDependencySpec>::new(),
        runtime: RuntimeSpec {
            interface: "vonk.runtime.v1".to_owned(),
            adapter: "vllm".to_owned(),
            adapter_version: 1,
            image: format!(
                "localhost/vonk/recipe-build-00000000-0000-4000-8000-000000000001@sha256:{DIGEST}"
            ),
            architecture: "linux/arm64".to_owned(),
            entrypoint: vec!["vllm".to_owned(), "serve".to_owned(), "/models".to_owned()],
            arguments: vec![
                RuntimeArgument {
                    name: "max_model_len".to_owned(),
                    value: ArgumentValue::Integer(32768),
                },
                RuntimeArgument {
                    name: "enable_prefix_caching".to_owned(),
                    value: ArgumentValue::Boolean(true),
                },
            ],
            environment: vec![],
            placement_environment: None,
        },
        artifacts: vec![ArtifactSpec {
            id: "model".to_owned(),
            kind: "huggingface.snapshot".to_owned(),
            repository: "publisher/model".to_owned(),
            revision: "b".repeat(40),
            download_bytes: 7,
            installed_bytes: 7,
            mount: ArtifactMountSpec {
                target: "/models".to_owned(),
                read_only: true,
            },
            roles: vec!["entrypoint".to_owned(), "worker".to_owned()],
        }],
        endpoint: EndpointSpec {
            protocol: "openai".to_owned(),
            port: 8000,
            model_aliases: vec!["model".to_owned()],
            health_path: "/v1/models".to_owned(),
        },
        security: SecuritySpec {
            devices: vec!["nvidia.com/gpu=all".to_owned()],
            capabilities: vec![],
            host_network: false,
            privileged: false,
            user: "10001:10001".to_owned(),
            mounts: vec![
                MountSpec {
                    source: "model".to_owned(),
                    target: "/models".to_owned(),
                    read_only: true,
                },
                MountSpec {
                    source: "outputs".to_owned(),
                    target: "/outputs".to_owned(),
                    read_only: false,
                },
            ],
        },
        lifecycle: LifecycleSpec {
            pre_start: vec![],
            post_stop: vec![],
            stop_timeout_seconds: 30,
        },
        topology: TopologySpec {
            name: "solo".to_owned(),
            node_count: 1,
            rank: 0,
            role: "entrypoint".to_owned(),
        },
    }
}

fn legacy_ds4_spec() -> WorkloadSpec {
    let mut workload = spec();
    workload.identity.recipe_revision_sha256 =
        "373169b0ef24f8d21b0aa40e918e13554bb4d788b4bd426df9f14b64b47d184a".to_owned();
    workload.identity.model_version_sha256 =
        "a54f12dd8653ff220efed3d5b1efa667ab95f060e16211f1cdba7e0a2dcfeafb".to_owned();
    workload.identity.harness_sha256 =
        "ac139f771cc97b27c1cf6fd97404b6a4db56d6d1725b4282cc5af0289a5421b3".to_owned();
    workload.identity.runtime_distribution_sha256 =
        "337c9d850a70b6a8907e588d4fee1d447f770bc004cb15bbc45283d017dca389".to_owned();
    workload.runtime.adapter = "ds4".to_owned();
    workload.runtime.entrypoint = vec![
        "/opt/vonk/bin/ds4-serve".to_owned(),
        "--model".to_owned(),
        format!("/models/{DS4_FILE}"),
        "--mtp".to_owned(),
        format!("/models/{DS4_DRAFTER_FILE}"),
        "--ctx".to_owned(),
        "131072".to_owned(),
        "--batched-session".to_owned(),
        "2".to_owned(),
        "--dspark".to_owned(),
        "--cuda".to_owned(),
        "--host".to_owned(),
        "0.0.0.0".to_owned(),
        "--port".to_owned(),
        "8080".to_owned(),
    ];
    workload.runtime.arguments.clear();
    workload.runtime.environment = vec![
        RuntimeEnvironment {
            name: "DS4_LOG_LEVEL".to_owned(),
            value: Some(ArgumentValue::String("INFO".to_owned())),
            secret: None,
        },
        RuntimeEnvironment {
            name: "HF_HUB_OFFLINE".to_owned(),
            value: Some(ArgumentValue::String("1".to_owned())),
            secret: None,
        },
    ];
    workload.endpoint.port = 8080;
    workload.endpoint.model_aliases = vec!["deepseek-v4-flash".to_owned()];
    workload.lifecycle.stop_timeout_seconds = 120;
    workload.artifacts[0] = ArtifactSpec {
        id: "target".to_owned(),
        kind: "http.file".to_owned(),
        repository: format!(
            "https://huggingface.co/antirez/deepseek-v4-gguf/resolve/{}/{DS4_FILE}",
            "e7f04037032990db0346398d249baf9fb9df1ccc"
        ),
        revision: "sha256:ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0"
            .to_owned(),
        download_bytes: 86_720_111_488,
        installed_bytes: 86_720_111_488,
        mount: ArtifactMountSpec {
            target: "/models".to_owned(),
            read_only: true,
        },
        roles: vec!["entrypoint".to_owned()],
    };
    let mut drafter = workload.artifacts[0].clone();
    drafter.id = "drafter".to_owned();
    drafter.repository = format!(
        "https://huggingface.co/antirez/deepseek-v4-gguf/resolve/{}/{DS4_DRAFTER_FILE}",
        "e7f04037032990db0346398d249baf9fb9df1ccc"
    );
    drafter.revision =
        "sha256:7e319924541db3f7a163ed7e11d7532a70d48228ab59d36cb81e1d4511885360".to_owned();
    drafter.download_bytes = 5_989_114_272;
    drafter.installed_bytes = 5_989_114_272;
    workload.artifacts.push(drafter);
    workload
}

fn artifact_key_for_test(artifact: &ArtifactSpec) -> String {
    hex::encode(Sha256::digest(serde_json::to_vec(artifact).unwrap()))
}

fn write_legacy_ds4_installation(root: &Path, installation_id: &str, workload: &WorkloadSpec) {
    let installation = root.join("installations").join(installation_id);
    fs::create_dir_all(&installation).unwrap();
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(workload).unwrap(),
    )
    .unwrap();
    fs::write(
        installation.join("recipe-content.sha256"),
        &workload.identity.recipe_revision_sha256,
    )
    .unwrap();
    for artifact in &workload.artifacts {
        let stored = root
            .join("models")
            .join("sha256")
            .join(artifact_key_for_test(artifact));
        fs::create_dir_all(&stored).unwrap();
        fs::write(stored.join("artifact"), b"weights").unwrap();
        fs::write(
            stored.join(".vonk-manifest.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 1,
                "files": {"artifact": &artifact.revision[7..]},
                "total_bytes": artifact.download_bytes
            }))
            .unwrap(),
        )
        .unwrap();
    }
}

fn bind_distributed_placement(workload: &mut WorkloadSpec) {
    workload.runtime.placement_environment = Some(PlacementEnvironmentSpec {
        local_address: "VONK_LOCAL_ADDR".to_owned(),
        master_address: "VONK_MASTER_ADDR".to_owned(),
        master_port: "VONK_MASTER_PORT".to_owned(),
    });
}

fn write_managed_run(root: &Path, run_id: &str, installation_id: &str, port: u16) {
    let installation = root.join("installations").join(installation_id);
    fs::create_dir_all(&installation).unwrap();
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&spec()).unwrap(),
    )
    .unwrap();
    let run = root.join("runs").join(run_id);
    fs::create_dir_all(&run).unwrap();
    let metadata = root.join("run-metadata").join(run_id);
    fs::create_dir_all(&metadata).unwrap();
    fs::write(
        metadata.join("lifecycle.json"),
        serde_json::to_vec(&serde_json::json!({
            "installation_id": installation_id,
            "placement": {
                "rank": 0,
                "role": "entrypoint",
                "world_size": 1,
                "local_address": null,
                "master_address": null,
                "master_port": null,
                "port": port,
                "reserved_memory_bytes": 1024
            }
        }))
        .unwrap(),
    )
    .unwrap();
}

fn one_response_server(status: u16) -> (u16, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = [0_u8; 4096];
        let size = stream.read(&mut request).unwrap();
        let request = std::str::from_utf8(&request[..size]).unwrap();
        assert!(request.starts_with("GET /v1/models HTTP/1.1\r\n"));
        write!(
            stream,
            "HTTP/1.1 {status} Test\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        .unwrap();
    });
    (port, server)
}

#[test]
fn workload_schema_rejects_shell_privilege_environment_and_host_paths() {
    let original = serde_json::to_value(spec()).unwrap();
    for (field, value) in [
        ("shell", serde_json::json!("curl evil")),
        ("environment", serde_json::json!({"TOKEN": "secret"})),
        ("host_path", serde_json::json!("/etc")),
    ] {
        let mut mutated = original.clone();
        mutated
            .as_object_mut()
            .unwrap()
            .insert(field.to_owned(), value);
        assert!(serde_json::from_value::<WorkloadSpec>(mutated).is_err());
    }
    let mut privileged = spec();
    privileged.security.privileged = true;
    assert!(privileged.validate().is_err());

    let mut private_interface = spec();
    private_interface.runtime.interface = "publisher-specific.v1".to_owned();
    assert!(private_interface.validate().is_err());

    let mut incomplete_mounts = spec();
    incomplete_mounts.security.mounts.pop();
    assert!(incomplete_mounts.validate().is_err());
}

#[test]
fn workload_authority_bindings_are_strictly_validated() {
    let mut invalid_identity = spec();
    invalid_identity.identity.recipe_revision_sha256 = "A".repeat(64);
    assert!(invalid_identity.validate().is_err());

    let mut invalid_dependency = spec();
    invalid_dependency
        .model_dependencies
        .push(ModelDependencySpec {
            kind: "mutable-model".to_owned(),
            publisher: "publisher".to_owned(),
            slug: "model".to_owned(),
            content_sha256: DIGEST.to_owned(),
        });
    assert!(invalid_dependency.validate().is_err());

    let mut invalid_topology = spec();
    invalid_topology.topology.rank = invalid_topology.topology.node_count;
    assert!(invalid_topology.validate().is_err());
}

#[test]
fn accepted_local_image_identity_is_validated_without_runtime_process_access() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .verify_image(&spec())
    .unwrap();
    assert!(runner.calls.borrow().is_empty());

    let mut external = spec();
    external.runtime.image = format!("registry.example/vonk/vllm@sha256:{DIGEST}");
    assert!(
        OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .verify_image(&external)
        .is_err()
    );
}

#[test]
fn container_arguments_are_typed_and_hardened() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    bind_distributed_placement(&mut workload);
    let arguments = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .start_arguments(
        &workload,
        "cb555393-764b-4eb6-8f15-b416d289428f",
        "45ea6921-50c9-4971-be2a-4cd04ce05069",
        &Placement {
            endpoint_address: Some("192.168.1.212".parse::<IpAddr>().unwrap()),
            rank: 1,
            role: "worker".to_owned(),
            world_size: 2,
            local_address: Some("192.168.100.11".parse::<IpAddr>().unwrap()),
            master_address: Some("192.168.100.10".parse::<IpAddr>().unwrap()),
            master_port: Some(29500),
            port: 8101,
            reserved_memory_bytes: 64 * 1024 * 1024 * 1024,
        },
    )
    .unwrap();

    for required in [
        "--read-only",
        "--init",
        "--pull",
        "never",
        "--log-driver",
        "local",
        "max-size=10m",
        "max-file=3",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "bridge",
        "--memory-swap",
        "--shm-size",
        "VONK_RANK=1",
        "VONK_WORLD_SIZE=2",
        "VONK_MASTER_ADDR=192.168.100.10",
        "VONK_LOCAL_ADDR=192.168.100.11",
        "VONK_MASTER_PORT=29500",
        "VONK_RUNTIME_SPEC=/run/vonk/runtime.json",
        "VONK_MODEL_ROOT=/models",
        "VONK_LISTEN_HOST=0.0.0.0",
        "VONK_LISTEN_PORT=8000",
    ] {
        assert!(
            arguments.iter().any(|value| value == required),
            "{required}"
        );
    }
    for environment in [
        "VONK_RANK=1",
        "VONK_WORLD_SIZE=2",
        "VONK_MASTER_ADDR=192.168.100.10",
        "VONK_LOCAL_ADDR=192.168.100.11",
        "VONK_MASTER_PORT=29500",
        "VONK_RUNTIME_SPEC=/run/vonk/runtime.json",
        "VONK_MODEL_ROOT=/models",
        "VONK_LISTEN_HOST=0.0.0.0",
        "VONK_LISTEN_PORT=8000",
    ] {
        assert!(
            arguments
                .windows(2)
                .any(|values| values == ["--env", environment]),
            "{environment}"
        );
    }
    assert!(
        !arguments
            .iter()
            .any(|value| value == "--privileged" || value == "--network=host")
    );
    assert!(
        arguments
            .windows(2)
            .any(|values| values == ["--device", "nvidia.com/gpu=all"])
    );
    assert!(
        arguments
            .windows(2)
            .any(|values| values == ["--publish", "192.168.1.212:8101:8000"])
    );
    assert!(
        arguments.windows(2).any(|values| {
            values == ["--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=1073741824"]
        })
    );
    assert!(
        arguments
            .iter()
            .any(|value| value.contains("/models/sha256/")
                && value.ends_with("dst=/models,readonly"))
    );
    assert!(!arguments.iter().any(|value| value
        == &format!(
            "type=bind,src={},dst=/models,readonly",
            directory.path().join("models").display()
        )));
    assert!(arguments.iter().any(|value| {
        value.ends_with("/outputs,dst=/outputs") && !value.ends_with(",readonly")
    }));
    assert!(
        !arguments
            .iter()
            .any(|value| { value == "VONK_STATE_ROOT=/state" || value.contains("dst=/state") })
    );
    assert!(
        arguments
            .iter()
            .any(|value| value.ends_with("dst=/run/vonk/runtime.json,readonly"))
    );
    assert!(
        arguments
            .windows(2)
            .any(|values| values == ["--max-model-len", "32768"])
    );
}

#[test]
fn direct_fabric_host_mode_has_one_compiled_privilege_shape() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    bind_distributed_placement(&mut workload);
    workload.security.host_network = true;
    let placement = Placement {
        endpoint_address: Some("192.168.1.211".parse::<IpAddr>().unwrap()),
        rank: 0,
        role: "entrypoint".to_owned(),
        world_size: 2,
        local_address: Some("192.168.100.10".parse::<IpAddr>().unwrap()),
        master_address: Some("192.168.100.10".parse::<IpAddr>().unwrap()),
        master_port: Some(29500),
        port: 8000,
        reserved_memory_bytes: 120_000_000_000,
    };

    workload.validate().unwrap();
    let arguments = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .start_arguments(
        &workload,
        "cb555393-764b-4eb6-8f15-b416d289428f",
        "45ea6921-50c9-4971-be2a-4cd04ce05069",
        &placement,
    )
    .unwrap();

    for pair in [
        ["--network", "host"],
        ["--ipc", "host"],
        ["--device", "/dev/infiniband:/dev/infiniband"],
        ["--ulimit", "memlock=-1:-1"],
        ["--ulimit", "stack=67108864:67108864"],
    ] {
        assert!(
            arguments.windows(2).any(|values| values == pair),
            "{pair:?}"
        );
    }
    assert!(!arguments.iter().any(|value| value == "--publish"));

    let mut single = placement;
    single.rank = 0;
    single.world_size = 1;
    single.local_address = None;
    single.master_address = None;
    single.master_port = None;
    assert!(
        OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .start_arguments(
            &workload,
            "cb555393-764b-4eb6-8f15-b416d289428f",
            "45ea6921-50c9-4971-be2a-4cd04ce05069",
            &single,
        )
        .is_err()
    );
}

#[test]
fn distributed_launch_refuses_an_unbound_rendezvous_projection() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let workload = spec();
    let placement = Placement {
        endpoint_address: Some("192.168.1.212".parse::<IpAddr>().unwrap()),
        rank: 1,
        role: "worker".to_owned(),
        world_size: 2,
        local_address: Some("192.168.100.11".parse::<IpAddr>().unwrap()),
        master_address: Some("192.168.100.10".parse::<IpAddr>().unwrap()),
        master_port: Some(29500),
        port: 8101,
        reserved_memory_bytes: 64 * 1024 * 1024 * 1024,
    };

    assert!(
        OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .start_arguments(
            &workload,
            "cb555393-764b-4eb6-8f15-b416d289428f",
            "45ea6921-50c9-4971-be2a-4cd04ce05069",
            &placement,
        )
        .is_err()
    );
}

#[test]
fn coordinator_publishes_rendezvous_only_on_declared_master_fabric_address() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    bind_distributed_placement(&mut workload);
    let arguments = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .start_arguments(
        &workload,
        "cb555393-764b-4eb6-8f15-b416d289428f",
        "45ea6921-50c9-4971-be2a-4cd04ce05069",
        &Placement {
            endpoint_address: Some("192.168.1.211".parse::<IpAddr>().unwrap()),
            rank: 0,
            role: "entrypoint".to_owned(),
            world_size: 2,
            local_address: Some("192.168.100.10".parse::<IpAddr>().unwrap()),
            master_address: Some("192.168.100.10".parse::<IpAddr>().unwrap()),
            master_port: Some(29500),
            port: 8100,
            reserved_memory_bytes: 64 * 1024 * 1024 * 1024,
        },
    )
    .unwrap();

    assert!(
        arguments
            .windows(2)
            .any(|values| values == ["--publish", "192.168.100.10:29500:29500"])
    );
    assert!(!arguments.iter().any(|value| value == "29500:29500"));
}

#[test]
fn canonical_runtime_mounts_are_order_independent() {
    let mut workload = spec();
    workload.security.mounts.reverse();

    workload.validate().unwrap();
}

#[test]
fn mutable_artifact_revisions_are_rejected_at_the_agent_boundary() {
    let mut workload = spec();
    workload.artifacts[0].revision = "main".to_owned();
    assert!(workload.validate().is_err());
}

#[test]
fn artifact_mount_targets_are_canonical_read_only_and_unique() {
    for target in [
        "/model",
        "/models/other",
        "/models/model/nested",
        "/models/model/",
    ] {
        let mut workload = spec();
        workload.artifacts[0].mount.target = target.to_owned();
        assert!(workload.validate().is_err(), "accepted {target}");
    }

    let mut writable = spec();
    writable.artifacts[0].mount.read_only = false;
    assert!(writable.validate().is_err());

    for id in [".", ".."] {
        let mut traversal = spec();
        traversal.artifacts[0].id = id.to_owned();
        traversal.artifacts[0].mount.target = format!("/models/{id}");
        assert!(traversal.validate().is_err(), "accepted {id}");
    }

    let mut duplicate = spec();
    duplicate.artifacts[0].mount.target = "/models/model".to_owned();
    duplicate.artifacts.push(duplicate.artifacts[0].clone());
    assert!(duplicate.validate().is_err());
}

#[test]
fn multiple_artifacts_require_and_use_exact_declared_targets() {
    let mut workload = spec();
    workload.artifacts[0].mount.target = "/models/model".to_owned();
    workload.validate().unwrap();
    let mut tokenizer = workload.artifacts[0].clone();
    tokenizer.id = "tokenizer".to_owned();
    tokenizer.repository = "publisher/tokenizer".to_owned();
    tokenizer.mount.target = "/models/tokenizer".to_owned();
    workload.artifacts.push(tokenizer);
    workload.validate().unwrap();

    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let arguments = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .start_arguments(
        &workload,
        "cb555393-764b-4eb6-8f15-b416d289428f",
        "45ea6921-50c9-4971-be2a-4cd04ce05069",
        &Placement {
            endpoint_address: None,
            rank: 0,
            role: "entrypoint".to_owned(),
            world_size: 1,
            local_address: None,
            master_address: None,
            master_port: None,
            port: 8101,
            reserved_memory_bytes: 64 * 1024 * 1024 * 1024,
        },
    )
    .unwrap();

    let artifact_mounts = arguments
        .iter()
        .filter(|value| value.contains("/models/sha256/"))
        .collect::<Vec<_>>();
    assert_eq!(artifact_mounts.len(), 2);
    assert!(
        artifact_mounts
            .iter()
            .any(|value| value.ends_with("dst=/models/model,readonly"))
    );
    assert!(
        artifact_mounts
            .iter()
            .any(|value| value.ends_with("dst=/models/tokenizer,readonly"))
    );

    workload.artifacts[0].mount.target = "/models".to_owned();
    assert!(workload.validate().is_err());
}

#[test]
fn persisted_legacy_ds4_spec_loads_as_named_mounts_and_rewrites_exact_paths() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let legacy = legacy_ds4_spec();
    assert!(legacy.validate().is_err());
    write_legacy_ds4_installation(directory.path(), installation_id, &legacy);

    let loaded = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .load_spec(installation_id)
    .unwrap();

    loaded.validate().unwrap();
    assert_eq!(loaded.artifacts[0].mount.target, "/models/target");
    assert_eq!(loaded.artifacts[1].mount.target, "/models/drafter");
    assert!(loaded.runtime.arguments.is_empty());
    assert!(
        loaded
            .runtime
            .entrypoint
            .windows(2)
            .any(|values| { values == ["--model", format!("/models/target/{DS4_FILE}").as_str()] })
    );
    assert!(loaded.runtime.entrypoint.windows(2).any(|values| {
        values
            == [
                "--mtp",
                format!("/models/drafter/{DS4_DRAFTER_FILE}").as_str(),
            ]
    }));
    for preserved in [
        "--ctx",
        "131072",
        "--batched-session",
        "2",
        "--dspark",
        "--cuda",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
    ] {
        assert!(
            loaded
                .runtime
                .entrypoint
                .iter()
                .any(|value| value == preserved)
        );
    }
    let persisted: WorkloadSpec = serde_json::from_slice(
        &fs::read(
            directory
                .path()
                .join("installations")
                .join(installation_id)
                .join("spec.json"),
        )
        .unwrap(),
    )
    .unwrap();
    assert!(
        persisted
            .artifacts
            .iter()
            .all(|artifact| artifact.mount.target == "/models")
    );
}

#[test]
fn persisted_legacy_built_in_ds4_identity_uses_its_exact_port() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let mut legacy = legacy_ds4_spec();
    legacy.identity.recipe_revision_sha256 =
        "32f09e39052ec5c13292c9bec5577d8536690d74576a4c3ac6c8ef4cf493927e".to_owned();
    legacy.identity.harness_sha256 =
        "a1dbca13724678dbce47a1caff4a7ae4b6c557a6ac6ca5c0e3a99733fcc3f2b0".to_owned();
    legacy.identity.runtime_distribution_sha256 =
        "73e2ec403510447cfbc067d0bdba20cfd941bd741e8b90a764edef3bae83c12a".to_owned();
    legacy.endpoint.port = 8080;
    *legacy.runtime.entrypoint.last_mut().unwrap() = "8080".to_owned();
    write_legacy_ds4_installation(directory.path(), installation_id, &legacy);

    let loaded = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .load_spec(installation_id)
    .unwrap();

    assert_eq!(loaded.endpoint.port, 8080);
    assert_eq!(loaded.runtime.entrypoint.last().unwrap(), "8080");
    assert_eq!(loaded.artifacts[0].mount.target, "/models/target");
    assert_eq!(loaded.artifacts[1].mount.target, "/models/drafter");
}

#[test]
fn persisted_legacy_ds4_spec_prepares_restart_with_original_cache_keys() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    let legacy = legacy_ds4_spec();
    write_legacy_ds4_installation(directory.path(), installation_id, &legacy);
    let runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };

    assert!(runtime.installed_bytes(installation_id).unwrap() >= 92_709_225_760);
    let artifact_set_digest = runtime.artifact_set_digest(installation_id).unwrap();
    assert_eq!(artifact_set_digest.len(), 64);
    assert!(
        artifact_set_digest
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit())
    );
    let loaded = runtime.load_spec(installation_id).unwrap();
    let plan = runtime
        .prepare_start(
            &loaded,
            installation_id,
            run_id,
            &Placement {
                endpoint_address: None,
                rank: 0,
                role: "entrypoint".to_owned(),
                world_size: 1,
                local_address: None,
                master_address: None,
                master_port: None,
                port: 8101,
                reserved_memory_bytes: 64 * 1024 * 1024 * 1024,
            },
        )
        .unwrap();

    for (artifact, target) in [
        (&legacy.artifacts[0], "/models/target"),
        (&legacy.artifacts[1], "/models/drafter"),
    ] {
        let stored = directory
            .path()
            .join("models")
            .join("sha256")
            .join(artifact_key_for_test(artifact));
        assert!(stored.join("artifact").is_file());
        assert!(plan.main.iter().any(|value| {
            value.starts_with(&format!("type=bind,src={}", stored.display()))
                && value.ends_with(&format!("dst={target},readonly"))
        }));
    }
    assert!(
        plan.main
            .windows(2)
            .any(|values| { values == ["--model", format!("/models/target/{DS4_FILE}").as_str()] })
    );
    assert!(plan.main.windows(2).any(|values| {
        values
            == [
                "--mtp",
                format!("/models/drafter/{DS4_DRAFTER_FILE}").as_str(),
            ]
    }));
}

#[test]
fn persisted_legacy_ds4_spec_can_be_uninstalled() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let legacy = legacy_ds4_spec();
    write_legacy_ds4_installation(directory.path(), installation_id, &legacy);

    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .uninstall(installation_id, &legacy.identity.recipe_revision_sha256)
    .unwrap();

    assert!(
        !directory
            .path()
            .join("installations")
            .join(installation_id)
            .exists()
    );
}

#[test]
fn persisted_legacy_mount_compatibility_rejects_non_ds4_or_inexact_paths() {
    for mutation in [
        "adapter",
        "path",
        "repository",
        "flag",
        "arguments",
        "identity",
        "stored-digest",
        "ctx",
        "batch",
        "endpoint",
    ] {
        let runner = FakeRunner {
            calls: RefCell::new(vec![]),
            outputs: RefCell::new(VecDeque::new()),
        };
        let directory = tempdir().unwrap();
        let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
        let mut legacy = legacy_ds4_spec();
        match mutation {
            "adapter" => legacy.runtime.adapter = "vllm".to_owned(),
            "path" => {
                let model = legacy
                    .runtime
                    .entrypoint
                    .iter()
                    .position(|value| value == "--model")
                    .unwrap();
                legacy.runtime.entrypoint[model + 1] = format!("/models/other/{DS4_FILE}");
            }
            "repository" => {
                legacy.artifacts[0].repository = format!("https://example.com/{DS4_FILE}");
            }
            "flag" => legacy.runtime.entrypoint.push("--unknown".to_owned()),
            "arguments" => legacy.runtime.arguments.push(RuntimeArgument {
                name: "model".to_owned(),
                value: ArgumentValue::String(format!("/models/{DS4_FILE}")),
            }),
            "identity" => legacy.identity.harness_sha256 = DIGEST.to_owned(),
            "stored-digest" => {}
            "ctx" => {
                let ctx = legacy
                    .runtime
                    .entrypoint
                    .iter()
                    .position(|value| value == "--ctx")
                    .unwrap();
                legacy.runtime.entrypoint[ctx + 1] = "32768".to_owned();
            }
            "batch" => {
                let batch = legacy
                    .runtime
                    .entrypoint
                    .iter()
                    .position(|value| value == "--batched-session")
                    .unwrap();
                legacy.runtime.entrypoint[batch + 1] = "1".to_owned();
            }
            "endpoint" => legacy.endpoint.port = 8000,
            _ => unreachable!(),
        }
        write_legacy_ds4_installation(directory.path(), installation_id, &legacy);
        if mutation == "stored-digest" {
            fs::write(
                directory
                    .path()
                    .join("installations")
                    .join(installation_id)
                    .join("recipe-content.sha256"),
                DIGEST,
            )
            .unwrap();
        }

        assert!(
            OciRuntime {
                runner: &runner,
                data_root: directory.path(),
                huggingface_curl_config: None,
            }
            .load_spec(installation_id)
            .is_err(),
            "accepted {mutation}"
        );
    }
}

#[test]
fn installation_records_and_rechecks_a_content_manifest() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: true,
                stdout: b"200\t\n".to_vec(),
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: b"200\t\n".to_vec(),
                stderr: vec![],
            },
        ])),
    };
    let directory = tempdir().unwrap();
    let runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";

    runtime.install(&spec(), installation_id, DIGEST).unwrap();
    let calls = runner.calls.borrow();
    let metadata_request = calls
        .iter()
        .find(|(program, arguments)| {
            *program == Program::Curl
                && arguments
                    .iter()
                    .any(|value| value.ends_with(".huggingface-model.json"))
        })
        .unwrap();
    let expected_metadata_url = format!(
        "https://huggingface.co/api/models/publisher/model/revision/{}",
        "b".repeat(40)
    );
    assert_eq!(
        metadata_request.1.last().map(String::as_str),
        Some(expected_metadata_url.as_str())
    );
    drop(calls);
    runtime.verify_installation(installation_id).unwrap();
    let weights = fs::read_dir(directory.path().join("models").join("sha256"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path()
        .join("weights.bin");
    fs::write(weights, b"tampered").unwrap();

    assert!(runtime.verify_installation(installation_id).is_err());
}

#[test]
fn absent_installation_has_no_recipe_identity() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };

    assert_eq!(
        runtime
            .recipe_digest_if_present("cb555393-764b-4eb6-8f15-b416d289428f")
            .unwrap(),
        None
    );
}

#[test]
fn present_installation_exposes_its_recipe_identity() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let installation = directory.path().join("installations").join(installation_id);
    fs::create_dir_all(&installation).unwrap();
    fs::write(installation.join("recipe-content.sha256"), DIGEST).unwrap();
    let runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };

    assert_eq!(
        runtime
            .recipe_digest_if_present(installation_id)
            .unwrap()
            .as_deref(),
        Some(DIGEST)
    );
}

#[test]
fn unsafe_installation_metadata_is_not_treated_as_absent() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let installations = directory.path().join("installations");
    fs::create_dir(&installations).unwrap();
    symlink(
        directory.path().join("outside"),
        installations.join("cb555393-764b-4eb6-8f15-b416d289428f"),
    )
    .unwrap();
    let runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };

    assert!(
        runtime
            .recipe_digest_if_present("cb555393-764b-4eb6-8f15-b416d289428f")
            .is_err()
    );
}

#[test]
fn uninstall_removes_matching_metadata_without_rehashing_model_artifacts() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let installation = directory.path().join("installations").join(installation_id);
    fs::create_dir_all(&installation).unwrap();
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&spec()).unwrap(),
    )
    .unwrap();
    fs::write(installation.join("recipe-content.sha256"), DIGEST).unwrap();
    let runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };

    runtime.uninstall(installation_id, DIGEST).unwrap();

    assert!(!installation.exists());
    assert!(runner.calls.borrow().is_empty());
}

#[test]
fn uninstall_preserves_metadata_when_recipe_identity_does_not_match() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let installation = directory.path().join("installations").join(installation_id);
    fs::create_dir_all(&installation).unwrap();
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&spec()).unwrap(),
    )
    .unwrap();
    fs::write(installation.join("recipe-content.sha256"), DIGEST).unwrap();
    let runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };

    assert!(runtime.uninstall(installation_id, &"b".repeat(64)).is_err());
    assert!(installation.exists());
}

#[test]
fn uninstall_rejects_symlinked_recipe_identity() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let installation = directory.path().join("installations").join(installation_id);
    fs::create_dir_all(&installation).unwrap();
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&spec()).unwrap(),
    )
    .unwrap();
    let outside_digest = directory.path().join("outside-digest");
    fs::write(&outside_digest, DIGEST).unwrap();
    symlink(&outside_digest, installation.join("recipe-content.sha256")).unwrap();
    let runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };

    assert!(runtime.uninstall(installation_id, DIGEST).is_err());
    assert!(installation.exists());
}

#[test]
fn http_artifacts_reject_private_hosts_before_curl_runs() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t10001:10001\n").into_bytes(),
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: b"200\t\n".to_vec(),
                stderr: vec![],
            },
        ])),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        id: "model".to_owned(),
        kind: "http.file".to_owned(),
        repository: "https://127.0.0.1/private".to_owned(),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        download_bytes: 7,
        installed_bytes: 7,
        mount: ArtifactMountSpec {
            target: "/models".to_owned(),
            read_only: true,
        },
        roles: vec!["entrypoint".to_owned()],
    };

    assert!(
        OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
        .is_err()
    );
    assert!(
        !runner
            .calls
            .borrow()
            .iter()
            .any(|call| call.0 == Program::Curl)
    );
}

#[test]
fn http_artifacts_reject_embedded_credentials_before_curl_runs() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t10001:10001\n").into_bytes(),
                stderr: vec![],
            },
        ])),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        id: "model".to_owned(),
        kind: "http.file".to_owned(),
        repository: "https://user:password@93.184.216.34/artifact".to_owned(),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        download_bytes: 7,
        installed_bytes: 7,
        mount: ArtifactMountSpec {
            target: "/models".to_owned(),
            read_only: true,
        },
        roles: vec!["entrypoint".to_owned()],
    };

    assert!(
        OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
        .is_err()
    );
    assert!(
        !runner
            .calls
            .borrow()
            .iter()
            .any(|call| call.0 == Program::Curl)
    );
}

#[test]
fn http_artifacts_reject_more_than_five_explicit_redirects() {
    let mut outputs = VecDeque::new();
    for redirect in 0..6 {
        outputs.push_back(ProcessOutput {
            success: true,
            stdout: format!("302\thttps://93.184.216.34/redirect-{redirect}\n").into_bytes(),
            stderr: vec![],
        });
    }
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(outputs),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        id: "model".to_owned(),
        kind: "http.file".to_owned(),
        repository: "https://93.184.216.34/artifact".to_owned(),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        download_bytes: 7,
        installed_bytes: 7,
        mount: ArtifactMountSpec {
            target: "/models".to_owned(),
            read_only: true,
        },
        roles: vec!["entrypoint".to_owned()],
    };

    assert!(
        OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
        .is_err()
    );
    assert_eq!(
        runner
            .calls
            .borrow()
            .iter()
            .filter(|call| call.0 == Program::Curl)
            .count(),
        6
    );
}

#[test]
fn http_artifacts_are_https_only_and_byte_limited_without_implicit_redirects() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([ProcessOutput {
            success: true,
            stdout: b"200\t\n".to_vec(),
            stderr: vec![],
        }])),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        id: "model".to_owned(),
        kind: "http.file".to_owned(),
        repository: "https://93.184.216.34/artifact".to_owned(),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        download_bytes: 7,
        installed_bytes: 7,
        mount: ArtifactMountSpec {
            target: "/models".to_owned(),
            read_only: true,
        },
        roles: vec!["entrypoint".to_owned()],
    };

    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
    .unwrap();

    let calls = runner.calls.borrow();
    let arguments = &calls.iter().find(|call| call.0 == Program::Curl).unwrap().1;
    assert!(
        arguments
            .windows(2)
            .any(|pair| pair == ["--max-filesize", "7"])
    );
    assert!(
        arguments
            .windows(2)
            .any(|pair| pair == ["--max-redirs", "0"])
    );
    assert!(!arguments.iter().any(|argument| argument == "--location"));
}

#[test]
fn http_artifacts_use_the_immutable_url_basename_and_manifest_key() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([ProcessOutput {
            success: true,
            stdout: b"200\t\n".to_vec(),
            stderr: vec![],
        }])),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        id: "model".to_owned(),
        kind: "http.file".to_owned(),
        repository: format!("https://93.184.216.34/releases/{DS4_FILE}"),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        download_bytes: 7,
        installed_bytes: 7,
        mount: ArtifactMountSpec {
            target: "/models".to_owned(),
            read_only: true,
        },
        roles: vec!["entrypoint".to_owned()],
    };

    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
    .unwrap();

    let calls = runner.calls.borrow();
    let curl_arguments = &calls.iter().find(|call| call.0 == Program::Curl).unwrap().1;
    let destination = curl_arguments
        .windows(2)
        .find(|pair| pair[0] == "--output")
        .map(|pair| Path::new(&pair[1]))
        .unwrap();
    assert_eq!(destination.file_name().unwrap(), DS4_FILE);
    let installed = fs::read_dir(directory.path().join("models").join("sha256"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    assert_eq!(fs::read(installed.join(DS4_FILE)).unwrap(), b"weights");
    assert!(!installed.join("artifact").exists());

    let manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(installed.join(".vonk-manifest.json")).unwrap()).unwrap();
    assert_eq!(
        manifest["files"][DS4_FILE],
        &workload.artifacts[0].revision[7..]
    );
    assert!(manifest["files"].get("artifact").is_none());
}

#[test]
fn verify_installation_migrates_valid_legacy_http_cache_without_redownloading() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([ProcessOutput {
            success: true,
            stdout: b"200\t\n".to_vec(),
            stderr: vec![],
        }])),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        id: "model".to_owned(),
        kind: "http.file".to_owned(),
        repository: format!("https://93.184.216.34/releases/{DS4_FILE}"),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        download_bytes: 7,
        installed_bytes: 7,
        mount: ArtifactMountSpec {
            target: "/models".to_owned(),
            read_only: true,
        },
        roles: vec!["entrypoint".to_owned()],
    };
    {
        let runtime = OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        };
        runtime
            .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
            .unwrap();
    }
    let installed = fs::read_dir(directory.path().join("models").join("sha256"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    fs::rename(installed.join(DS4_FILE), installed.join("artifact")).unwrap();
    let manifest_path = installed.join(".vonk-manifest.json");
    let mut legacy_manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    let digest = legacy_manifest["files"]
        .as_object_mut()
        .unwrap()
        .remove(DS4_FILE)
        .unwrap();
    legacy_manifest["files"]
        .as_object_mut()
        .unwrap()
        .insert("artifact".to_owned(), digest);
    fs::write(
        &manifest_path,
        serde_json::to_vec(&legacy_manifest).unwrap(),
    )
    .unwrap();
    runner.calls.borrow_mut().clear();
    let restarted_runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };
    restarted_runtime
        .verify_installation("cb555393-764b-4eb6-8f15-b416d289428f")
        .unwrap();

    assert!(runner.calls.borrow().is_empty());
    assert_eq!(fs::read(installed.join(DS4_FILE)).unwrap(), b"weights");
    assert!(!installed.join("artifact").exists());
    let migrated_manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    assert_eq!(
        migrated_manifest["files"][DS4_FILE],
        &workload.artifacts[0].revision[7..]
    );
    assert!(migrated_manifest["files"].get("artifact").is_none());
    restarted_runtime
        .verify_installation("cb555393-764b-4eb6-8f15-b416d289428f")
        .unwrap();
}

#[test]
fn verify_installation_repairs_interrupted_http_cache_migration() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([ProcessOutput {
            success: true,
            stdout: b"200\t\n".to_vec(),
            stderr: vec![],
        }])),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        id: "model".to_owned(),
        kind: "http.file".to_owned(),
        repository: format!("https://93.184.216.34/releases/{DS4_FILE}"),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        download_bytes: 7,
        installed_bytes: 7,
        mount: ArtifactMountSpec {
            target: "/models".to_owned(),
            read_only: true,
        },
        roles: vec!["entrypoint".to_owned()],
    };
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    {
        let runtime = OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        };
        runtime.install(&workload, installation_id, DIGEST).unwrap();
    }
    let installed = fs::read_dir(directory.path().join("models").join("sha256"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let manifest_path = installed.join(".vonk-manifest.json");
    let mut interrupted_manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    let digest = interrupted_manifest["files"]
        .as_object_mut()
        .unwrap()
        .remove(DS4_FILE)
        .unwrap();
    interrupted_manifest["files"]
        .as_object_mut()
        .unwrap()
        .insert("artifact".to_owned(), digest);
    fs::write(
        &manifest_path,
        serde_json::to_vec(&interrupted_manifest).unwrap(),
    )
    .unwrap();
    let orphan = installed.join("..vonk-manifest.json.4242.tmp");
    fs::write(&orphan, b"{\"schema_version\":1,\"files\":").unwrap();
    runner.calls.borrow_mut().clear();
    let restarted_runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };
    restarted_runtime
        .verify_installation(installation_id)
        .unwrap();

    assert!(runner.calls.borrow().is_empty());
    assert!(!orphan.exists());
    assert_eq!(fs::read(installed.join(DS4_FILE)).unwrap(), b"weights");
    assert!(!installed.join("artifact").exists());
    let repaired_manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    assert_eq!(
        repaired_manifest["files"][DS4_FILE],
        &workload.artifacts[0].revision[7..]
    );
    assert!(repaired_manifest["files"].get("artifact").is_none());

    fs::rename(installed.join(DS4_FILE), installed.join("artifact")).unwrap();
    restarted_runtime
        .verify_installation(installation_id)
        .unwrap();
    assert_eq!(fs::read(installed.join(DS4_FILE)).unwrap(), b"weights");
    assert!(!installed.join("artifact").exists());
}

#[test]
fn http_artifacts_reject_unsafe_or_missing_url_basenames_before_curl_runs() {
    let long_name = "a".repeat(256);
    let repositories = [
        "https://93.184.216.34/".to_owned(),
        "https://93.184.216.34/.".to_owned(),
        "https://93.184.216.34/..".to_owned(),
        "https://93.184.216.34/bad%20name.gguf".to_owned(),
        "https://93.184.216.34/bad%2Fname.gguf".to_owned(),
        "https://93.184.216.34/bad\\name.gguf".to_owned(),
        "https://93.184.216.34/bad\nname.gguf".to_owned(),
        format!("https://93.184.216.34/{long_name}"),
    ];

    for repository in repositories {
        let runner = FakeRunner {
            calls: RefCell::new(vec![]),
            outputs: RefCell::new(VecDeque::new()),
        };
        let directory = tempdir().unwrap();
        let mut workload = spec();
        workload.artifacts[0] = ArtifactSpec {
            id: "model".to_owned(),
            kind: "http.file".to_owned(),
            repository: repository.clone(),
            revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
                .to_owned(),
            download_bytes: 7,
            installed_bytes: 7,
            mount: ArtifactMountSpec {
                target: "/models".to_owned(),
                read_only: true,
            },
            roles: vec!["entrypoint".to_owned()],
        };

        assert!(
            OciRuntime {
                runner: &runner,
                data_root: directory.path(),
                huggingface_curl_config: None,
            }
            .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
            .is_err(),
            "accepted {repository:?}"
        );
        assert!(
            runner.calls.borrow().is_empty(),
            "ran curl for {repository:?}"
        );
    }
}

#[test]
fn oci_artifacts_reject_private_registry_hosts_before_oras_runs() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t10001:10001\n").into_bytes(),
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
        ])),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        id: "model".to_owned(),
        kind: "oci.artifact".to_owned(),
        repository: "127.0.0.1/private/artifact".to_owned(),
        revision: format!("sha256:{DIGEST}"),
        download_bytes: 7,
        installed_bytes: 7,
        mount: ArtifactMountSpec {
            target: "/models".to_owned(),
            read_only: true,
        },
        roles: vec!["entrypoint".to_owned()],
    };

    assert!(
        OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
        .is_err()
    );
    assert!(
        !runner
            .calls
            .borrow()
            .iter()
            .any(|call| call.0 == Program::Oras)
    );
}

#[test]
fn oci_artifacts_run_under_the_declared_staging_budget() {
    let runner = BudgetRunner {
        inner: FakeRunner {
            calls: RefCell::new(vec![]),
            outputs: RefCell::new(VecDeque::from([
                ProcessOutput {
                    success: true,
                    stdout: vec![],
                    stderr: vec![],
                },
                ProcessOutput {
                    success: true,
                    stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t10001:10001\n")
                        .into_bytes(),
                    stderr: vec![],
                },
                ProcessOutput {
                    success: true,
                    stdout: vec![],
                    stderr: vec![],
                },
            ])),
        },
        budgets: RefCell::new(vec![]),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        id: "model".to_owned(),
        kind: "oci.artifact".to_owned(),
        repository: "ghcr.io/vonkforge/public-artifact".to_owned(),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        download_bytes: 7,
        installed_bytes: 7,
        mount: ArtifactMountSpec {
            target: "/models".to_owned(),
            read_only: true,
        },
        roles: vec!["entrypoint".to_owned()],
    };

    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
    .unwrap();

    assert_eq!(*runner.budgets.borrow(), [7]);
    let calls = runner.inner.calls.borrow();
    let oras = calls.iter().find(|call| call.0 == Program::Oras).unwrap();
    let resolve = oras
        .1
        .iter()
        .position(|value| value == "--resolve")
        .unwrap();
    assert!(oras.1[resolve + 1].starts_with("ghcr.io:443:"));
}

#[test]
fn start_keeps_agent_metadata_outside_workload_writable_state() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .prepare_start(
        &spec(),
        "cb555393-764b-4eb6-8f15-b416d289428f",
        run_id,
        &Placement {
            endpoint_address: Some("192.168.1.211".parse::<IpAddr>().unwrap()),
            rank: 0,
            role: "entrypoint".to_owned(),
            world_size: 1,
            local_address: None,
            master_address: None,
            master_port: None,
            port: 8101,
            reserved_memory_bytes: 64 * 1024 * 1024 * 1024,
        },
    )
    .unwrap();

    let contract: serde_json::Value = serde_json::from_slice(
        &fs::read(
            directory
                .path()
                .join("run-metadata")
                .join(run_id)
                .join("runtime.json"),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(contract["interface"], "vonk.runtime.v1");
    assert_eq!(contract["artifacts"][0]["id"], "model");
    assert_eq!(contract["artifacts"][0]["path"], "/models");
    assert_eq!(contract["endpoint"]["listen_port"], 8000);
    assert_eq!(contract["placement"]["rank"], 0);
    assert!(
        directory
            .path()
            .join("run-metadata")
            .join(run_id)
            .join("lifecycle.json")
            .is_file()
    );
    let writable_run_root = directory.path().join("runs").join(run_id);
    assert_eq!(
        fs::read_dir(&writable_run_root)
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect::<Vec<_>>(),
        vec!["outputs"]
    );
    assert!(
        fs::read_dir(writable_run_root.join("outputs"))
            .unwrap()
            .next()
            .is_none()
    );
}

#[test]
fn managed_recipe_run_observation_reports_running_healthy_container() {
    let directory = tempdir().unwrap();
    let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    let (port, server) = one_response_server(204);
    write_managed_run(
        directory.path(),
        run_id,
        "cb555393-764b-4eb6-8f15-b416d289428f",
        port,
    );
    let runner = ObservationRunner {
        calls: RefCell::new(vec![]),
        podman_outputs: RefCell::new(VecDeque::from([ProcessOutput {
            success: true,
            stdout: format!("true\tvonk-{run_id}\n").into_bytes(),
            stderr: vec![],
        }])),
    };

    let observations = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .recipe_run_observations()
    .unwrap();

    assert_eq!(
        serde_json::to_value(&observations).unwrap(),
        serde_json::json!([{"run_id": run_id, "ready": true}])
    );
    assert!(
        directory
            .path()
            .join("run-metadata")
            .join(run_id)
            .join("lifecycle.json")
            .is_file()
    );
    server.join().unwrap();
    let calls = runner.calls.borrow();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].0, Program::Curl);
    assert!(calls[0].1.iter().any(|value| value == "--max-time"));
    assert!(
        calls[0]
            .1
            .iter()
            .any(|value| value == &format!("http://127.0.0.1:{port}/v1/models"))
    );
}

#[test]
fn managed_recipe_run_observation_reports_running_unhealthy_container() {
    let directory = tempdir().unwrap();
    let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    let (port, server) = one_response_server(503);
    write_managed_run(
        directory.path(),
        run_id,
        "cb555393-764b-4eb6-8f15-b416d289428f",
        port,
    );
    let runner = ObservationRunner {
        calls: RefCell::new(vec![]),
        podman_outputs: RefCell::new(VecDeque::from([ProcessOutput {
            success: true,
            stdout: format!("true\tvonk-{run_id}\n").into_bytes(),
            stderr: vec![],
        }])),
    };

    let observations = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .recipe_run_observations()
    .unwrap();

    assert_eq!(observations.len(), 1);
    assert!(!observations[0].ready);
    server.join().unwrap();
}

#[test]
fn managed_recipe_run_observation_reports_unreachable_endpoints_without_docker_access() {
    let directory = tempdir().unwrap();
    let first = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    let second = "55ea6921-50c9-4971-be2a-4cd04ce05069";
    for run_id in [first, second] {
        write_managed_run(
            directory.path(),
            run_id,
            "cb555393-764b-4eb6-8f15-b416d289428f",
            8101,
        );
    }
    let runner = ObservationRunner {
        calls: RefCell::new(vec![]),
        podman_outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: false,
                stdout: vec![],
                stderr: b"no such container".to_vec(),
            },
            ProcessOutput {
                success: true,
                stdout: format!("false\tvonk-{second}\n").into_bytes(),
                stderr: vec![],
            },
        ])),
    };

    let observations = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .recipe_run_observations()
    .unwrap();

    assert_eq!(observations.len(), 2);
    assert!(observations.iter().all(|observation| !observation.ready));
    assert!(
        runner
            .calls
            .borrow()
            .iter()
            .all(|call| call.0 == Program::Curl)
    );
}

#[test]
fn managed_recipe_run_snapshot_skips_safe_historical_directory_without_lifecycle_marker() {
    let directory = tempdir().unwrap();
    let run = directory
        .path()
        .join("runs")
        .join("45ea6921-50c9-4971-be2a-4cd04ce05069");
    fs::create_dir_all(&run).unwrap();
    fs::write(run.join("runtime.json"), b"historical runtime evidence").unwrap();
    let runner = ObservationRunner {
        calls: RefCell::new(vec![]),
        podman_outputs: RefCell::new(VecDeque::new()),
    };

    let observations = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .recipe_run_observations()
    .unwrap();

    assert!(observations.is_empty());
    assert!(runner.calls.borrow().is_empty());
}

#[test]
fn managed_recipe_run_snapshot_skips_one_corrupt_record_and_reports_other_runs() {
    let directory = tempdir().unwrap();
    let corrupt = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    let healthy = "55ea6921-50c9-4971-be2a-4cd04ce05069";
    let corrupt_run = directory.path().join("runs").join(corrupt);
    fs::create_dir_all(&corrupt_run).unwrap();
    let corrupt_metadata = directory.path().join("run-metadata").join(corrupt);
    fs::create_dir_all(&corrupt_metadata).unwrap();
    fs::write(corrupt_metadata.join("lifecycle.json"), b"not-json").unwrap();
    write_managed_run(
        directory.path(),
        healthy,
        "cb555393-764b-4eb6-8f15-b416d289428f",
        8101,
    );
    let runner = ObservationRunner {
        calls: RefCell::new(vec![]),
        podman_outputs: RefCell::new(VecDeque::from([ProcessOutput {
            success: false,
            stdout: vec![],
            stderr: b"no such container".to_vec(),
        }])),
    };

    let observations = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .recipe_run_observations()
    .unwrap();

    assert_eq!(observations.len(), 1);
    assert_eq!(observations[0].run_id, healthy);
    assert!(!observations[0].ready);
    assert_eq!(runner.calls.borrow().len(), 1);
}

#[test]
fn managed_recipe_run_snapshot_rejects_malformed_entries_and_skips_corrupt_records() {
    for corrupt in [
        "run-symlink",
        "name",
        "marker-symlink",
        "oversized-marker",
        "invalid-marker",
        "invalid-installation-id",
        "unsafe-health-path",
    ] {
        let directory = tempdir().unwrap();
        let runs = directory.path().join("runs");
        fs::create_dir_all(&runs).unwrap();
        match corrupt {
            "run-symlink" => {
                let target = directory.path().join("outside");
                fs::create_dir(&target).unwrap();
                symlink(target, runs.join("45ea6921-50c9-4971-be2a-4cd04ce05069")).unwrap();
            }
            "name" => fs::create_dir(runs.join("not-a-run-uuid")).unwrap(),
            "marker-symlink" => {
                let run = runs.join("45ea6921-50c9-4971-be2a-4cd04ce05069");
                fs::create_dir(&run).unwrap();
                let metadata = directory
                    .path()
                    .join("run-metadata")
                    .join("45ea6921-50c9-4971-be2a-4cd04ce05069");
                fs::create_dir_all(&metadata).unwrap();
                symlink(
                    directory.path().join("outside.json"),
                    metadata.join("lifecycle.json"),
                )
                .unwrap();
            }
            "oversized-marker" => {
                let run = runs.join("45ea6921-50c9-4971-be2a-4cd04ce05069");
                fs::create_dir(&run).unwrap();
                let metadata = directory
                    .path()
                    .join("run-metadata")
                    .join("45ea6921-50c9-4971-be2a-4cd04ce05069");
                fs::create_dir_all(&metadata).unwrap();
                fs::write(metadata.join("lifecycle.json"), vec![b'x'; 16 * 1024 + 1]).unwrap();
            }
            "invalid-marker" => {
                let run = runs.join("45ea6921-50c9-4971-be2a-4cd04ce05069");
                fs::create_dir(&run).unwrap();
                let metadata = directory
                    .path()
                    .join("run-metadata")
                    .join("45ea6921-50c9-4971-be2a-4cd04ce05069");
                fs::create_dir_all(&metadata).unwrap();
                fs::write(metadata.join("lifecycle.json"), b"{}").unwrap();
            }
            "invalid-installation-id" => write_managed_run(
                directory.path(),
                "45ea6921-50c9-4971-be2a-4cd04ce05069",
                "not-an-installation-id",
                8101,
            ),
            "unsafe-health-path" => {
                let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
                write_managed_run(
                    directory.path(),
                    "45ea6921-50c9-4971-be2a-4cd04ce05069",
                    installation_id,
                    8101,
                );
                let mut workload = spec();
                workload.endpoint.health_path = "/v1/models\r\nInjected: true".to_owned();
                fs::write(
                    directory
                        .path()
                        .join("installations")
                        .join(installation_id)
                        .join("spec.json"),
                    serde_json::to_vec(&workload).unwrap(),
                )
                .unwrap();
            }
            _ => unreachable!(),
        }
        let runner = ObservationRunner {
            calls: RefCell::new(vec![]),
            podman_outputs: RefCell::new(VecDeque::from([ProcessOutput {
                success: false,
                stdout: vec![],
                stderr: b"no such container".to_vec(),
            }])),
        };

        let result = OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .recipe_run_observations();
        if matches!(corrupt, "run-symlink" | "name") {
            assert!(result.is_err(), "{corrupt}");
        } else {
            assert!(result.unwrap().is_empty(), "{corrupt}");
        }
        assert!(runner.calls.borrow().is_empty(), "{corrupt}");
    }
}

#[test]
fn managed_recipe_run_snapshot_rejects_more_than_64_lifecycle_markers_before_inspection() {
    let directory = tempdir().unwrap();
    for value in 1..=65_u128 {
        write_managed_run(
            directory.path(),
            &uuid::Uuid::from_u128(value).to_string(),
            "cb555393-764b-4eb6-8f15-b416d289428f",
            8101,
        );
    }
    let runner = ObservationRunner {
        calls: RefCell::new(vec![]),
        podman_outputs: RefCell::new(VecDeque::new()),
    };

    assert!(
        OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .recipe_run_observations()
        .is_err()
    );
    assert!(runner.calls.borrow().is_empty());
}

#[test]
fn stopped_historical_directories_do_not_consume_the_64_managed_run_limit() {
    let directory = tempdir().unwrap();
    let runs = directory.path().join("runs");
    fs::create_dir_all(&runs).unwrap();
    for value in 1..=65_u128 {
        fs::create_dir(runs.join(uuid::Uuid::from_u128(value).to_string())).unwrap();
    }
    let active = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    write_managed_run(
        directory.path(),
        active,
        "cb555393-764b-4eb6-8f15-b416d289428f",
        8101,
    );
    let runner = ObservationRunner {
        calls: RefCell::new(vec![]),
        podman_outputs: RefCell::new(VecDeque::from([ProcessOutput {
            success: false,
            stdout: vec![],
            stderr: b"no such container".to_vec(),
        }])),
    };

    let observations = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .recipe_run_observations()
    .unwrap();

    assert_eq!(observations.len(), 1);
    assert_eq!(observations[0].run_id, active);
    assert!(!observations[0].ready);
}

#[test]
fn stop_is_idempotent_when_a_gang_rank_never_started() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";

    let plan = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .prepare_stop(run_id)
    .unwrap();

    assert_eq!(plan.remove, [run_id, "30"]);
    assert!(plan.post_stop.is_empty());
    assert!(runner.calls.borrow().is_empty());
}

#[test]
fn lifecycle_hooks_run_as_typed_hardened_one_shot_containers() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };
    let mut workload = spec();
    workload.lifecycle.pre_start = vec![vec![
        "python".to_owned(),
        "-m".to_owned(),
        "prepare".to_owned(),
    ]];
    workload.lifecycle.post_stop = vec![vec![
        "python".to_owned(),
        "-m".to_owned(),
        "cleanup".to_owned(),
    ]];
    let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let installation = directory.path().join("installations").join(installation_id);
    fs::create_dir_all(&installation).unwrap();
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&workload).unwrap(),
    )
    .unwrap();

    let start = runtime
        .prepare_start(
            &workload,
            installation_id,
            run_id,
            &Placement {
                endpoint_address: Some("192.168.1.211".parse::<IpAddr>().unwrap()),
                rank: 0,
                role: "entrypoint".to_owned(),
                world_size: 1,
                local_address: None,
                master_address: None,
                master_port: None,
                port: 8101,
                reserved_memory_bytes: 64 * 1024 * 1024 * 1024,
            },
        )
        .unwrap();
    let stop = runtime.prepare_stop(run_id).unwrap();
    runtime.complete_stop(run_id).unwrap();

    assert!(
        !directory
            .path()
            .join("run-metadata")
            .join(run_id)
            .join("lifecycle.json")
            .exists()
    );
    assert!(
        directory
            .path()
            .join("run-metadata")
            .join(run_id)
            .join("runtime.json")
            .exists()
    );

    assert_eq!(start.pre_start[0][0..2], ["run", "--rm"]);
    assert!(
        !start.pre_start[0]
            .iter()
            .any(|value| value == "--detach" || value == "--name")
    );
    assert_eq!(
        &start.pre_start[0][start.pre_start[0].len() - 3..],
        ["python", "-m", "prepare"]
    );
    assert!(start.main.iter().any(|value| value == "--detach"));
    assert_eq!(stop.remove, [run_id, "30"]);
    assert_eq!(
        &stop.post_stop[0][stop.post_stop[0].len() - 3..],
        ["python", "-m", "cleanup"]
    );
    assert!(runner.calls.borrow().is_empty());
    assert!(stop.post_stop[0].iter().any(|value| value == "--read-only"));
}

#[test]
fn post_stop_marker_is_retained_until_host_hook_success_is_finalized() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let mut workload = spec();
    workload.lifecycle.post_stop = vec![vec!["false".to_owned()]];
    write_managed_run(directory.path(), run_id, installation_id, 8101);
    fs::write(
        directory
            .path()
            .join("installations")
            .join(installation_id)
            .join("spec.json"),
        serde_json::to_vec(&workload).unwrap(),
    )
    .unwrap();

    let plan = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .prepare_stop(run_id)
    .unwrap();
    assert_eq!(plan.post_stop.len(), 1);
    assert!(
        directory
            .path()
            .join("run-metadata")
            .join(run_id)
            .join("lifecycle.json")
            .exists()
    );
}
