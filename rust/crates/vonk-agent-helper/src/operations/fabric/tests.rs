#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn signed_endpoint_placement_is_not_vetoed_by_stale_firewall_observation() {
    // Wrong implementation: generated policy overrides signed placement.
    // Reapplication uses only the kit's existing network envelope.
    enum Answer {
        Policy,
        Missing,
        Unapplied,
    }
    struct EndpointRunner(Answer);
    impl CommandRunner for EndpointRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new(super::DOCKER_FIREWALL));
            if arguments[2] == "apply" {
                return match self.0 {
                    Answer::Missing => Err("compiled command could not start".to_owned()),
                    _ => Ok(docker_output(true, "", 0)),
                };
            }
            assert_eq!(arguments[2], "check-endpoint-port");
            match self.0 {
                Answer::Missing => Err("compiled command could not start".to_owned()),
                Answer::Unapplied => Ok(CommandOutput {
                    success: false,
                    stdout: Vec::new(),
                    stderr: b"vonk-forge-docker-firewall: managed firewall chain is unavailable\n"
                        .to_vec(),
                    exit_code: Some(1),
                }),
                Answer::Policy => {
                    let authorised = matches!(arguments[3].as_str(), "8000" | "8101");
                    Ok(CommandOutput {
                        success: authorised,
                        stdout: Vec::new(),
                        stderr: if authorised {
                            Vec::new()
                        } else {
                            b"vonk-forge-docker-firewall: published endpoint host port 30000 is not \
                              authorized (authorized endpoint host ports: 8000,8101)\n"
                                .to_vec()
                        },
                        exit_code: Some(if authorised { 0 } else { 3 }),
                    })
                }
            }
        }
    }
    let run_for = |answer: Answer, host_port: &str| {
        let (_temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::create_dir_all(&model).unwrap();
        let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
        let network = arguments.iter().position(|value| value == "none").unwrap();
        arguments[network] = "bridge".to_owned();
        let image = arguments
            .iter()
            .position(|value| value.starts_with("localhost/vonk/"))
            .unwrap();
        arguments.splice(
            image..image,
            [
                "--publish".to_owned(),
                format!("192.168.1.211:{host_port}:8000"),
                "--device".to_owned(),
                "nvidia.com/gpu=all".to_owned(),
                "--env".to_owned(),
                "VONK_LISTEN_PORT=8000".to_owned(),
            ],
        );
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], EndpointRunner(answer), None).unwrap();
        let run = validate_docker_run(&arguments, &roots, None).unwrap();
        (executor.require_authorised_published_endpoint(&run), run)
    };

    let (accepted, run) = run_for(Answer::Policy, "8101");
    assert!(accepted.is_ok());
    assert_eq!(run.published_endpoint_port, Some(8101));
    assert!(run_for(Answer::Policy, "30000").0.is_ok());
    assert!(run_for(Answer::Missing, "30000").0.is_err());
    assert!(run_for(Answer::Policy, "30000").0.is_ok());
    assert!(run_for(Answer::Unapplied, "30000").0.is_ok());
    assert!(run_for(Answer::Policy, "8101").0.is_ok());
}

#[test]
fn firewall_unknown_ends_boundedly_then_kernel_repair_admits_fresh_binding() {
    // Wrong implementation: nonzero/malformed firewall observations become
    // admission authority, or leave a busy owner after kernel observation ends.
    enum Behavior {
        Refuses,
        DoesNotRun,
    }
    struct FirewallRunner(Behavior, Arc<std::sync::atomic::AtomicUsize>);
    impl CommandRunner for FirewallRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new(super::DOCKER_FIREWALL));
            if arguments[2] == "apply" {
                self.1.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                return Ok(docker_output(true, "", 0));
            }
            match self.0 {
                Behavior::Refuses => Ok(CommandOutput {
                    success: false,
                    stdout: Vec::new(),
                    stderr: b"vonk-forge-docker-firewall: host endpoint port 8000 is not \
                              authorized (authorized host endpoint ports: 8888)\n"
                        .to_vec(),
                    exit_code: Some(1),
                }),
                Behavior::DoesNotRun => Err("command timed out".to_owned()),
            }
        }
    }
    let observe = |behavior| {
        let (temp, roots) = runtime_fixture();
        let model = artifact_path(&roots, 'a');
        fs::create_dir_all(&model).unwrap();
        let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
        let network = arguments.iter().position(|value| value == "none").unwrap();
        arguments[network] = "host".to_owned();
        let image = arguments
            .iter()
            .position(|value| value.starts_with("localhost/vonk/"))
            .unwrap();
        arguments.splice(
            image..image,
            [
                "--device",
                "nvidia.com/gpu=all",
                "--device",
                "/dev/infiniband:/dev/infiniband",
                "--ulimit",
                "memlock=-1:-1",
                "--ulimit",
                "stack=67108864:67108864",
                "--env",
                "VONK_MASTER_PORT=29500",
                "--env",
                "VONK_RANK=0",
                "--env",
                "VONK_WORLD_SIZE=2",
                "--env",
                "VONK_LOCAL_ADDR=192.168.100.10",
                "--env",
                "VONK_MASTER_ADDR=192.168.100.10",
                "--env",
                "VONK_LISTEN_PORT=8000",
            ]
            .map(str::to_owned),
        );
        let applications = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let executor = OperationExecutor::new(
            roots.clone(),
            &[0; 32],
            FirewallRunner(behavior, Arc::clone(&applications)),
            None,
        )
        .unwrap();
        let mut run = validate_docker_run(&arguments, &roots, None).unwrap();
        let sysfs = temp.path().join("sysfs");
        let began = Instant::now();
        assert!(executor.bind_native_fabric(&mut run, &sysfs).is_err());
        assert!(began.elapsed() < Duration::from_secs(2));
        assert!(
            !run.arguments
                .iter()
                .any(|argument| argument.starts_with("NCCL_SOCKET_IFNAME="))
        );
        crate::runtime_fabric::tests::gid(
            &sysfs,
            "rocep1s0f1",
            "3",
            "::ffff:192.168.100.10",
            "RoCE v2",
        );
        // The same current signed placement re-observes the actual kernel
        // binding even while the unrelated firewall projection stays damaged.
        executor.bind_native_fabric(&mut run, &sysfs).unwrap();
        let before = applications.load(std::sync::atomic::Ordering::SeqCst);
        let mut inspected = validate_docker_run(&arguments, &roots, None).unwrap();
        executor.observe_native_fabric(&mut inspected, &sysfs).unwrap();
        assert_eq!(applications.load(std::sync::atomic::Ordering::SeqCst), before);
        assert_eq!(inspected.arguments, run.arguments);
        assert!(
            run.arguments
                .iter()
                .any(|argument| argument == "NCCL_SOCKET_IFNAME==enp1s0f1np1")
        );
    };
    observe(Behavior::Refuses);
    observe(Behavior::DoesNotRun);
}

#[test]
fn native_fabric_requires_complete_bounded_shape_without_publications() {
    let (temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    fs::create_dir_all(&model).unwrap();
    let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
    let network = arguments.iter().position(|value| value == "none").unwrap();
    arguments[network] = "host".to_owned();
    let image = arguments
        .iter()
        .position(|value| value.starts_with("localhost/vonk/"))
        .unwrap();
    arguments.splice(
        image..image,
        [
            "--device",
            "nvidia.com/gpu=all",
            "--device",
            "/dev/infiniband:/dev/infiniband",
            "--ulimit",
            "memlock=-1:-1",
            "--ulimit",
            "stack=67108864:67108864",
            "--env",
            "VONK_MASTER_PORT=29500",
            "--env",
            "VONK_RANK=0",
            "--env",
            "VONK_WORLD_SIZE=2",
            "--env",
            "VONK_LOCAL_ADDR=192.168.100.10",
            "--env",
            "VONK_MASTER_ADDR=192.168.100.10",
            "--env",
            "VONK_LISTEN_PORT=8000",
        ]
        .map(str::to_owned),
    );
    assert!(validate_docker_run(&arguments, &roots, None).is_ok());
    let world_size_index = arguments
        .iter()
        .position(|arg| arg == "VONK_WORLD_SIZE=2")
        .unwrap();
    // These are decimal environment arguments, not machine-sized indexes.
    // Preserve the exact canonical topology domain before native launch.
    for world_size in ["18446744073709551616".to_owned(), "9".repeat(200)] {
        let mut wide = arguments.clone();
        wide[world_size_index] = format!("VONK_WORLD_SIZE={world_size}");
        assert!(validate_docker_run(&wide, &roots, None).is_ok());
    }
    for invalid in [
        "-1",
        "0",
        "1.0",
        "1e0",
        "true",
        r#"{"$serde_json::private::Number":"2"}"#,
    ] {
        let mut invalid_shape = arguments.clone();
        invalid_shape[world_size_index] = format!("VONK_WORLD_SIZE={invalid}");
        assert!(validate_docker_run(&invalid_shape, &roots, None).is_err());
    }
    let mut nonzero_owner = arguments.clone();
    let rank = nonzero_owner
        .iter()
        .position(|arg| arg == "VONK_RANK=0")
        .unwrap();
    let mut negative_zero = arguments.clone();
    negative_zero[rank] = "VONK_RANK=-0".to_owned();
    assert!(validate_docker_run(&negative_zero, &roots, None).is_ok());
    nonzero_owner[rank] = "VONK_RANK=1".to_owned();
    assert!(validate_docker_run(&nonzero_owner, &roots, None).is_ok());
    let mut wide_rank = arguments.clone();
    wide_rank[world_size_index] = format!("VONK_WORLD_SIZE={}", "9".repeat(200));
    wide_rank[rank] = format!("VONK_RANK={}", "8".repeat(200));
    assert!(validate_docker_run(&wide_rank, &roots, None).is_ok());
    wide_rank[rank] = format!("VONK_RANK={}", "9".repeat(200));
    assert!(validate_docker_run(&wide_rank, &roots, None).is_err());
    for invalid in [
        "-1",
        "1.0",
        "1e0",
        "true",
        r#"{"$serde_json::private::Number":"1"}"#,
    ] {
        let mut invalid_shape = arguments.clone();
        invalid_shape[rank] = format!("VONK_RANK={invalid}");
        assert!(validate_docker_run(&invalid_shape, &roots, None).is_err());
    }
    let mut worker = arguments.clone();
    let local = worker
        .iter()
        .position(|arg| arg == "VONK_LOCAL_ADDR=192.168.100.10")
        .unwrap();
    worker[local] = "VONK_LOCAL_ADDR=192.168.100.11".to_owned();
    let endpoint = worker
        .iter()
        .position(|arg| arg == "VONK_LISTEN_PORT=8000")
        .unwrap();
    worker.drain(endpoint - 1..=endpoint);
    assert!(validate_docker_run(&worker, &roots, None).is_ok());
    struct FabricRunner;
    impl CommandRunner for FabricRunner {
        fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(executable, Path::new(super::DOCKER_FIREWALL));
            assert_eq!(
                &arguments[2..],
                [
                    "check-fabric-run",
                    "192.168.100.10",
                    "192.168.100.10",
                    "29500",
                    "8000"
                ]
            );
            Ok(CommandOutput {
                success: true,
                stdout: b"enp1s0f1np1\n".to_vec(),
                exit_code: Some(0),
                stderr: Vec::new(),
            })
        }
    }
    let sysfs = temp.path().join("sysfs");
    crate::runtime_fabric::tests::gid(
        &sysfs,
        "rocep1s0f1",
        "3",
        "::ffff:192.168.100.10",
        "RoCE v2",
    );
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], FabricRunner, None).unwrap();
    let mut started = validate_docker_run(&arguments, &roots, None).unwrap();
    executor.bind_native_fabric(&mut started, &sysfs).unwrap();
    let mut inspected = validate_docker_run(&arguments, &roots, None).unwrap();
    executor.observe_native_fabric(&mut inspected, &sysfs).unwrap();
    assert_eq!(started.arguments, inspected.arguments);
    assert!(
        started
            .arguments
            .iter()
            .any(|arg| arg == "NCCL_IB_HCA==rocep1s0f1:1")
    );
    assert!(
        started
            .arguments
            .iter()
            .any(|arg| arg == "NCCL_SOCKET_IFNAME==enp1s0f1np1")
    );
    // One device: launch and identity agree.
    assert!(started.launch_hca.is_none());
    assert!(
        started
            .docker_arguments()
            .unwrap()
            .iter()
            .any(|arg| arg == "NCCL_IB_HCA==rocep1s0f1:1")
    );
    // A second device of the same cabled port is launched with the first
    // while the identity digest, and so every existing run, stays as is.
    crate::runtime_fabric::tests::gid(
        &sysfs,
        "roceP2p1s0f1",
        "3",
        "::ffff:192.168.101.10",
        "RoCE v2",
    );
    std::fs::write(
        sysfs.join("roceP2p1s0f1/ports/1/gid_attrs/ndevs/3"),
        "enP2p1s0f1np1\n",
    )
    .unwrap();
    crate::runtime_fabric::tests::pci(&sysfs, "rocep1s0f1", "0000:01:00.1", "0x1021");
    crate::runtime_fabric::tests::pci(&sysfs, "roceP2p1s0f1", "0002:01:00.1", "0x1021");
    let mut railed = validate_docker_run(&arguments, &roots, None).unwrap();
    executor.bind_native_fabric(&mut railed, &sysfs).unwrap();
    assert_eq!(railed.arguments, started.arguments);
    let launched = railed.docker_arguments().unwrap();
    assert!(
        launched
            .iter()
            .any(|arg| arg == "NCCL_IB_HCA==rocep1s0f1:1,roceP2p1s0f1:1")
    );
    assert!(
        !launched
            .iter()
            .any(|arg| arg == "NCCL_IB_HCA==rocep1s0f1:1")
    );
    assert!(
        launched
            .iter()
            .any(|arg| arg == "/dev/infiniband:/dev/infiniband")
    );
    std::fs::remove_dir_all(sysfs.join("roceP2p1s0f1")).unwrap();
    let compiled = started.docker_arguments().unwrap();
    assert_eq!(compiled[started.image_index], started.local_image_reference);
    assert_eq!(
        compiled
            .iter()
            .filter(|arg| *arg == &started.entrypoint)
            .count(),
        1
    );
    for required in [
        "/dev/infiniband:/dev/infiniband",
        "memlock=-1:-1",
        "stack=67108864:67108864",
        "VONK_WORLD_SIZE=2",
        "VONK_LOCAL_ADDR=192.168.100.10",
    ] {
        let mut incomplete = arguments.clone();
        let index = incomplete
            .iter()
            .position(|value| value == required)
            .unwrap();
        incomplete.drain(index - 1..=index);
        assert!(
            validate_docker_run(&incomplete, &roots, None).is_err(),
            "missing {required}"
        );
    }
    for forbidden in [
        ["--publish", "192.168.1.211:8000:8000"],
        ["--ipc", "host"],
        ["--env", "NCCL_SOCKET_IFNAME=wlP9s9"],
    ] {
        let mut polluted = arguments.clone();
        let image = polluted
            .iter()
            .position(|value| value.starts_with("localhost/vonk/"))
            .unwrap();
        polluted.splice(image..image, forbidden.map(str::to_owned));
        assert!(validate_docker_run(&polluted, &roots, None).is_err());
    }
}
