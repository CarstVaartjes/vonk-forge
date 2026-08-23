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

use tempfile::tempdir;
use vonk_agent::{
    oci::OciRuntime,
    process::{ProcessError, ProcessOutput, ProcessRunner, Program, SystemProcessRunner},
    workloads::{
        ArgumentValue, ArtifactMountSpec, ArtifactSpec, EndpointSpec, LifecycleSpec,
        ModelDependencySpec, MountSpec, Placement, PlacementEnvironmentSpec, RuntimeArgument,
        RuntimeSpec, SecuritySpec, TopologySpec, WorkloadIdentitySpec, WorkloadSpec,
    },
};

const DIGEST: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

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
        "--gpus",
        "all",
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
    assert!(
        !arguments.iter().any(|value| value == "--privileged"
            || value == "--network=host"
            || value == "--device")
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
            .any(|value| value.ends_with("dst=/models,readonly"))
    );
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
    assert_eq!(
        contract["artifacts"][0]["path"]
            .as_str()
            .unwrap()
            .split('/')
            .count(),
        4
    );
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
