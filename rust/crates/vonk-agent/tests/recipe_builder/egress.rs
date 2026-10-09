use super::*;

#[test]
fn build_failure_preserves_partial_storage_and_admits_fresh_work() {
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

    assert!(!error.to_string().contains("private"));
    // Failed build storage is retained for reconciliation/reuse, without
    // reserving a queue head or blocking a new build identity.
    fresh_build(root.path(), runtime.path());
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
    // Starting the boundary from the hardened agent inherits read-only procfs
    // mounts. Every helper step must use the same clean user namespace as the
    // build; detaching Podman would also kill conmon when its service exits.
    assert_eq!(proxy.0, Program::SystemdRun);
    assert!(!proxy.1.iter().any(|value| value == "--detach"));
    assert!(!proxy.1.iter().any(|value| value == "--wait"));
    let runtime = build
        .1
        .iter()
        .find(|value| value.starts_with("--setenv=XDG_RUNTIME_DIR="))
        .unwrap();
    for (program, arguments) in calls.iter().filter(|(_, arguments)| {
        arguments
            .iter()
            .any(|value| value == "import" || value == "network" || value == "exec")
    }) {
        assert_eq!(*program, Program::SystemdRun);
        assert!(arguments.contains(runtime));
    }
    let unit = proxy
        .1
        .iter()
        .find_map(|value| value.strip_prefix("--unit="))
        .unwrap();
    assert!(calls.iter().any(|(program, arguments)| {
        *program == Program::Systemctl
            && arguments.iter().any(|value| value == "list-units")
            && arguments.last().is_some_and(|value| value == unit)
    }));
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
            .any(|value| value == "HTTP_PROXY=http://10.89.0.2:18080")
    );
    let adapter_build = calls
        .iter()
        .find(|(_, arguments)| {
            arguments
                .iter()
                .any(|value| value == &format!("--unit=vonk-runtime-adapter-{operation}"))
        })
        .expect("the adapter is applied as its own ordered stage");
    assert!(
        adapter_build
            .1
            .iter()
            .any(|value| value == "--network=none"),
        "the adapter stage installs from the local recipe image and needs no egress"
    );
    assert!(
        adapter_build
            .1
            .iter()
            .all(|value| !value.contains("HTTP_PROXY") && !value.contains("HTTPS_PROXY")),
        "the declared public hosts must not leak into the adapter stage"
    );
    assert!(
        calls
            .iter()
            .any(|(_, arguments)| arguments.windows(2).any(|pair| pair == ["network", "rm"]))
    );
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
        hosts: vec!["pypi.org".to_owned()],
    };

    let _error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build_cancellable(&build_request, operation, &archive, &|| {
        runner.cancelled.get()
    })
    .unwrap_err();

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
    drop(calls);
    fresh_build(root.path(), runtime.path());
}
