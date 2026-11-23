/// The proxy's connection bound, read from its own source: one owner.
fn proxy_max_connections() -> usize {
    include_str!("../../../../vonk-build-egress/src/main.rs")
        .lines()
        .find_map(|line| line.strip_prefix("const MAX_CONNECTIONS: usize = "))
        .and_then(|value| value.trim_end_matches(';').parse().ok())
        .expect("the proxy declares MAX_CONNECTIONS")
}

#[test]
fn the_proxy_task_limit_covers_its_connection_bound() {
    // Wrong implementation: a task limit below what MAX_CONNECTIONS
    // connections need (1 serve + 4 tasks each), so load kills the proxy.
    let needed = 1 + 4 * proxy_max_connections();
    assert!(super::PROXY_PIDS_LIMIT as usize >= needed);
}

#[test]
#[ignore = "requires an isolated Linux systemd host, agent user and installed egress helper"]
fn real_egress_boundary_survives_hardened_agent_and_cleans_up() {
    use super::{BuildEgress, BuildEgressStart};
    use crate::process::{ProcessRunner, Program, SystemProcessRunner};
    use std::{
        fs,
        path::Path,
        time::{Duration, Instant},
    };

    let staging = tempfile::tempdir_in("/var/lib/vonk-forge-agent").unwrap();
    let runtime = tempfile::Builder::new()
        .prefix("e-")
        .tempdir_in("/run/vonk-forge-agent")
        .unwrap();
    let storage = staging.path().join("storage");
    fs::create_dir(&storage).unwrap();
    fs::create_dir(staging.path().join("podman-image-tmp")).unwrap();
    let runner = SystemProcessRunner;
    let hosts = ["pypi.org".to_owned()];
    let boundary = BuildEgress::start(
        &runner,
        BuildEgressStart {
            storage: &storage,
            runroot: runtime.path(),
            staging: staging.path(),
            binary: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
            operation_id: uuid::Uuid::new_v4(),
            hosts: &hosts,
            minimum_free_disk_bytes: 0,
            deadline: Instant::now() + Duration::from_secs(60),
            cancelled: &|| false,
        },
    )
    .unwrap_or_else(|error| panic!("{:?}", error.failure_evidence()));
    let unit = boundary.proxy_unit.clone();
    let active = runner
        .run(
            Program::Systemctl,
            &["--user".to_owned(), "is-active".to_owned(), unit.clone()],
            Duration::from_secs(10),
        )
        .unwrap();
    assert!(active.success, "the proxy must remain owned by its service");
    // Exercise actual Dockerfile RUNs through the production command
    // builder, not just a healthy proxy in its own network namespace.
    let probe = "/usr/lib/vonk-forge/build-network-probe";
    let outbound = format!(
        "{}:18080",
        boundary
            .address(
                &boundary.outbound_network,
                Instant::now() + Duration::from_secs(10),
                &|| false
            )
            .unwrap()
    );
    let reachable = boundary
        .run(
            &[
                "unshare".to_owned(),
                "--rootless-netns".to_owned(),
                probe.to_owned(),
                "--reachable".to_owned(),
                outbound.clone(),
            ],
            Duration::from_secs(10),
        )
        .unwrap();
    assert!(
        reachable.success,
        "the outbound canary must be reachable outside the build: {reachable:?}"
    );
    // The platform proxy's own connection bound, held at once the way a
    // parallel `uv pip install` does: the proxy must neither run out of
    // tasks and die (panic = abort) nor stop admitting afterwards.
    let hold = |count: usize| {
        boundary
            .run(
                &[
                    "unshare".to_owned(),
                    "--rootless-netns".to_owned(),
                    probe.to_owned(),
                    "--hold-tunnels".to_owned(),
                    outbound.clone(),
                    count.to_string(),
                    "pypi.org:443".to_owned(),
                ],
                Duration::from_secs(45),
            )
            .unwrap()
    };
    let held = hold(proxy_max_connections());
    assert!(
        held.success,
        "{} parallel tunnels: {}\n{}",
        proxy_max_connections(),
        String::from_utf8_lossy(&held.stdout),
        String::from_utf8_lossy(&held.stderr)
    );
    let survived = runner
        .run(
            Program::Systemctl,
            &["--user".to_owned(), "is-active".to_owned(), unit.clone()],
            Duration::from_secs(10),
        )
        .unwrap();
    assert!(
        survived.success,
        "parallel tunnels at the connection bound killed the proxy"
    );
    // The closed tunnels release their slots as the proxy sees the
    // disconnects; admission must return within a bound.
    let admission_deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let admitted = hold(1);
        if admitted.success {
            break;
        }
        assert!(
            Instant::now() < admission_deadline,
            "no fresh tunnel after the load: {}\n{}",
            String::from_utf8_lossy(&admitted.stdout),
            String::from_utf8_lossy(&admitted.stderr)
        );
        std::thread::sleep(Duration::from_millis(500));
    }
    let context = staging.path().join("context");
    fs::create_dir(&context).unwrap();
    fs::copy(probe, context.join("probe")).unwrap();
    fs::write(
        context.join("Dockerfile"),
        "FROM scratch\nCOPY probe /probe\nRUN [\"/probe\"]\nRUN [\"/probe\"]\n",
    )
    .unwrap();
    let proxy = format!(
        "http://{}:18080",
        boundary
            .address(
                &boundary.internal_network,
                Instant::now() + Duration::from_secs(10),
                &|| false
            )
            .unwrap()
    );
    for network in [
        Some((boundary.internal_network.as_str(), proxy.as_str())),
        None,
    ] {
        let mut command = super::podman_user_service_arguments(
            &format!("vonk-recipe-build-{}", uuid::Uuid::new_v4()),
            runtime.path(),
            staging.path(),
            Duration::from_secs(30),
            true,
        );
        command.push("/usr/bin/podman".to_owned());
        command.extend(super::podman_build_arguments(
            &storage,
            runtime.path(),
            network,
        ));
        command.extend([
            "--no-cache".to_owned(),
            "--pull=never".to_owned(),
            "--cap-drop=all".to_owned(),
            "--security-opt=no-new-privileges".to_owned(),
            "--env".to_owned(),
            format!("VONK_TEST_OUTBOUND={outbound}"),
            "--env".to_owned(),
            format!(
                "VONK_TEST_NETWORK={}",
                if network.is_some() { "public" } else { "none" }
            ),
            context.display().to_string(),
        ]);
        let built = runner
            .run(Program::SystemdRun, &command, Duration::from_secs(30))
            .unwrap();
        assert!(
            built.success,
            "Dockerfile RUN failed: {}\n{}",
            String::from_utf8_lossy(&built.stdout),
            String::from_utf8_lossy(&built.stderr)
        );
    }
    let mut network_exists = boundary.arguments(Duration::from_secs(10));
    network_exists.extend([
        "network".to_owned(),
        "exists".to_owned(),
        boundary.internal_network.clone(),
    ]);
    drop(boundary);
    assert!(
        !runner
            .run(
                Program::Systemctl,
                &["--user".to_owned(), "is-active".to_owned(), unit],
                Duration::from_secs(10)
            )
            .unwrap()
            .success
    );
    assert!(
        !runner
            .run(
                Program::SystemdRun,
                &network_exists,
                Duration::from_secs(10)
            )
            .unwrap()
            .success
    );
}
