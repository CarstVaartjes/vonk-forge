use super::*;

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
    let wide_integer = "9".repeat(200);
    build_request.options.environment.push(
        vonk_agent_protocol::parse_strict::<
            vonk_agent_protocol::generated::RecipeBuildEnvironmentArgument,
        >(format!(r#"{{"name":"EXACT_INTEGER","value":{wide_integer}}}"#).as_bytes())
        .unwrap(),
    );
    // Read the retained canonical request again before the real builder path;
    // serde or argument rendering must not narrow an accepted integer.
    let retained_request = root.path().join("accepted-build-request.json");
    fs::write(&retained_request, canonical_json(&build_request).unwrap()).unwrap();
    let build_request: RecipeBuildRequest =
        vonk_agent_protocol::parse_strict(&fs::read(&retained_request).unwrap()).unwrap();
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
    assert!(
        build
            .1
            .iter()
            .any(|value| value == &format!("EXACT_INTEGER={wide_integer}"))
    );
    for required in [
        "--user",
        "--wait",
        "--pipe",
        "--collect",
        "--quiet",
        "--service-type=exec",
        "--unit=vonk-recipe-build-00000000-0000-4000-8000-000000000002",
        "--setenv=HOME=/var/lib/vonk-forge-agent",
        "--setenv=XDG_CONFIG_HOME=/var/lib/vonk-forge-agent/.config",
        "--setenv=XDG_DATA_HOME=/var/lib/vonk-forge-agent",
        "--setenv=CONTAINERS_STORAGE_CONF=/etc/vonk-forge-agent/containers-storage.conf",
        "--property=MemoryMax=8589934592",
        "--property=CPUQuota=800%",
        "--property=TasksMax=4096",
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
        "--root",
        "--runroot",
    ] {
        assert!(build.1.iter().any(|value| value == required), "{required}");
    }
    let runtime_limit: f64 = build
        .1
        .iter()
        .find_map(|value| {
            value
                .strip_prefix("--property=RuntimeMaxSec=")?
                .strip_suffix('s')?
                .parse()
                .ok()
        })
        .unwrap();
    assert!(runtime_limit > 0.0 && runtime_limit <= 3600.0);
    assert!(
        build
            .1
            .iter()
            .any(|value| value.starts_with("--setenv=TMPDIR=")
                && value.ends_with("/podman-image-tmp"))
    );
    let runroot = build
        .1
        .windows(2)
        .find(|pair| pair[0] == "--runroot")
        .unwrap();
    assert!(
        build
            .1
            .contains(&format!("--setenv=XDG_RUNTIME_DIR={}", runroot[1]))
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
    // The recipe image is only the adaptation stage's input.  The exported
    // artifact must be the adapted image, and the adaptation must be the one
    // reviewed stage that installs the interface label, the adapter identity
    // and the runtime user.
    let adapter = adapter_fixture();
    // Image references are keyed by the request's build id; the transient unit
    // names and the private build root are keyed by the operation id.
    let build_id = build_request.build_id;
    let adapter_unit = format!("--unit=vonk-runtime-adapter-{operation}");
    let adapter_build_index = calls
        .iter()
        .position(|call| call.1.iter().any(|value| value == &adapter_unit))
        .expect("the resolved adapter must run as its own ordered build stage");
    let adapter_build = &calls[adapter_build_index];
    assert_eq!(adapter_build.0, Program::SystemdRun);
    assert!(
        calls
            .iter()
            .position(|call| call.1.iter().any(|value| value == "build"))
            .unwrap()
            < adapter_build_index,
        "the recipe image must be built before the adapter is applied"
    );
    assert_eq!(
        calls
            .iter()
            .filter(|call| call.1.iter().any(|value| value == "build"))
            .count(),
        2,
        "exactly one recipe build stage plus one ordered adapter stage"
    );
    assert!(
        adapter_build.1.iter().any(|value| value == "--build-arg")
            && adapter_build
                .1
                .iter()
                .any(|value| value == &format!("VONK_RECIPE_IMAGE={}", recipe_build_tag(build_id))),
        "the adaptation stage must build from the verified recipe image"
    );
    for label in [
        "--label=ai.vonkforge.runtime-interface=v1".to_owned(),
        format!(
            "--label=ai.vonkforge.runtime-adapter={}",
            adapter.definition.adapter_id
        ),
        format!(
            "--label=ai.vonkforge.runtime-adapter-sha256={}",
            adapter.adapter_sha256
        ),
    ] {
        assert!(
            adapter_build.1.iter().any(|value| value == &label),
            "the adapted image must carry {label}"
        );
    }
    assert!(
        adapter_build
            .1
            .iter()
            .any(|value| value == "--cap-drop=all")
            && adapter_build
                .1
                .iter()
                .any(|value| value == "--security-opt=no-new-privileges"),
        "the adapter stage keeps the fail-closed build envelope"
    );
    for reference in [recipe_build_tag(build_id), adapted_build_tag(build_id)] {
        assert!(
            calls.iter().any(|call| {
                call.1.iter().any(|value| value == "inspect")
                    && call.1.iter().any(|value| value == &reference)
            }),
            "each build stage image must be inspected before it is trusted"
        );
        assert!(
            calls.iter().any(|call| {
                call.1.windows(2).any(|pair| pair == ["image", "rm"])
                    && call.1.iter().any(|value| value == &reference)
            }),
            "{reference} must not survive the build"
        );
    }
    let push = calls
        .iter()
        .find(|call| call.1.iter().any(|value| value == "push"))
        .unwrap();
    assert!(
        push.1
            .iter()
            .any(|value| value == &adapted_build_tag(build_id)),
        "only the adapted image may be exported"
    );
    assert!(
        !push
            .1
            .iter()
            .any(|value| value == &recipe_build_tag(build_id)),
        "the un-adapted recipe image must never leave the builder"
    );
    assert!(
        Path::new(
            push.1
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
fn import_build_and_export_share_the_recipe_deadline() {
    const SYNTHETIC_RECIPE_DEADLINE_SECONDS: u32 = 777;
    let (archive, digest) = bundle();
    let runner = DeadlineRunner {
        inner: Runner {
            calls: RefCell::new(Vec::new()),
            fail_build: false,
            oversize_base: false,
            registry: None,
            substitute_base: false,
        },
        timeouts: RefCell::new(Vec::new()),
    };
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let runtime = tempdir().unwrap();
    let mut build_request = request(archive.len(), digest);
    build_request.limits.timeout_seconds = SYNTHETIC_RECIPE_DEADLINE_SECONDS;

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

    let timeouts = runner.timeouts.borrow();
    assert_eq!(
        timeouts
            .iter()
            .map(|(phase, _)| phase.as_str())
            .collect::<Vec<_>>(),
        ["load", "build", "adapt", "push"]
    );
    assert!(timeouts.iter().all(|(_, timeout)| {
        *timeout > Duration::ZERO
            && *timeout <= Duration::from_secs(SYNTHETIC_RECIPE_DEADLINE_SECONDS.into())
    }));
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
    let calls = runner.calls.borrow();
    assert!(
        calls.iter().any(|(_, arguments)| arguments
            .iter()
            .any(|value| value == &format!("--unit=vonk-runtime-adapter-{operation}"))),
        "the limit is checked on the exported adapted image, so the adaptation must have run"
    );
    let push = calls
        .iter()
        .find(|(_, arguments)| arguments.iter().any(|value| value == "push"))
        .unwrap();
    assert!(
        push.1
            .iter()
            .any(|value| value == &adapted_build_tag(build_request.build_id)),
        "the oversize export must be the adapted image, not its recipe input"
    );
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
