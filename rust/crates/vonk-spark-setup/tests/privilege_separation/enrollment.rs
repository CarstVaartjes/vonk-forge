use super::*;

#[test]
fn fresh_preparation_discovers_and_prompts_before_a_stdin_only_sudo_handoff() {
    let temporary = tempdir().unwrap();
    fs::create_dir_all(paths(temporary.path()).config.parent().unwrap()).unwrap();
    let ca = controller_ca();
    let mut prepare_runner = runner_with_bootstrap(&ca);
    let mut prompt = fresh_answers(&ca);

    let prepared = prepare_setup(
        &request(temporary.path()),
        &paths(temporary.path()),
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();

    // Besides read-only host address detection, discovery is exactly two
    // curl requests: the pinned-fingerprint probe, then verified TLS.
    let curls = prepare_runner
        .commands
        .iter()
        .filter(|command| command.program == std::path::Path::new("/usr/bin/curl"))
        .collect::<Vec<_>>();
    assert_eq!(curls.len(), 2);
    assert!(curls[0].args.contains(&"--insecure".to_owned()));
    assert!(curls[1].args.contains(&"--cacert".to_owned()));

    let mut root_runner = RecordingRunner::default();
    handoff_to_root_with_authority(&prepared, &mut root_runner, &ReleaseAuthority::canonical())
        .unwrap();

    assert_eq!(root_runner.commands.len(), 1);
    let sudo = &root_runner.commands[0];
    assert_eq!(sudo.program, std::path::Path::new("/usr/bin/sudo"));
    assert_eq!(sudo.args.first().map(String::as_str), Some("-n"));
    assert!(sudo.args.iter().all(|argument| argument != TOKEN));
    assert!(sudo.env.values().all(|value| value != TOKEN));
    assert!(
        sudo.stdin
            .windows(TOKEN.len())
            .any(|value| value == TOKEN.as_bytes())
    );
    assert!(
        sudo.args
            .iter()
            .any(|argument| argument.contains("__apply"))
    );
    assert!(
        sudo.args
            .iter()
            .all(|argument| !argument.contains("exec \"$setup\"")),
        "the root staging shell must regain control and remove its temporary copies"
    );
    assert!(
        sudo.args
            .iter()
            .all(|argument| !argument.contains("--privileged"))
    );
    assert!(
        sudo.args
            .iter()
            .all(|argument| !argument.contains("/dev/tty"))
    );
    assert!(sudo.args.iter().all(|argument| !argument.contains("curl")));
}

#[test]
fn expired_sudo_ticket_fails_before_the_frame_can_be_applied() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, _) = fresh_prepared(temporary.path(), &install_paths);
    let mut runner = RecordingRunner {
        outputs: [
            CommandOutput {
                success: false,
                stdout: Vec::new(),
                stderr: Vec::new(),
            },
            CommandOutput {
                success: false,
                stdout: Vec::new(),
                stderr: Vec::new(),
            },
        ]
        .into(),
        ..Default::default()
    };
    let result =
        handoff_to_root_with_authority(&prepared, &mut runner, &ReleaseAuthority::canonical());
    assert!(
        matches!(result, Err(SetupError::Command(message)) if message.contains("sudo authorization expired"))
    );
    assert_eq!(runner.commands.len(), 2);
    assert_eq!(runner.commands[1].args, ["-n", "-v"]);
    assert!(runner.commands[1].stdin.is_empty());
}

#[test]
fn root_apply_installs_pairs_starts_and_verifies_without_tty_or_discovery() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let ca = controller_ca();
    fs::write(&install_paths.hosts, "127.0.0.1 localhost\n").unwrap();
    let mut prepare_runner = runner_with_private_controller_bootstrap(&ca);
    // The Controller's command supplies the NAS address, enrollment URL and
    // CA fingerprint; only this host's own addresses remain to be asked.
    let mut prompt = fresh_answers(&ca);
    prompt.values.drain(0..3);
    let prepared = prepare_setup(
        &request(temporary.path())
            .with_controller_address(Some("192.168.1.231"))
            .unwrap()
            .with_enrollment_values(
                Some("https://enroll.example.test"),
                Some(&ca_fingerprint(&ca)),
            )
            .unwrap(),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    for command in prepare_runner
        .commands
        .iter()
        .filter(|command| command.program == std::path::Path::new("/usr/bin/curl"))
    {
        assert!(command.args.windows(2).any(|arguments| {
            arguments == ["--resolve", "enroll.example.test:443:192.168.1.231"]
        }));
    }
    assert!(
        prepare_runner.commands[0]
            .args
            .contains(&"--insecure".to_owned())
    );
    assert!(
        prepare_runner.commands[1]
            .args
            .contains(&"--cacert".to_owned())
    );
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(
        &prepared,
        &mut handoff_runner,
        &ReleaseAuthority::canonical(),
    )
    .unwrap();
    let frame = handoff_runner.commands[0].stdin.clone();
    let mut apply_runner = RecordingRunner::default();

    apply_setup_from(
        frame.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut apply_runner,
        CallerIdentity::sudo_root(1000),
    )
    .unwrap();

    assert_eq!(
        apply_runner.commands[0].program,
        std::path::Path::new("/usr/bin/apt-get")
    );
    assert!(
        apply_runner.commands[0]
            .args
            .iter()
            .any(|argument| argument == "install")
    );
    assert!(
        apply_runner
            .commands
            .iter()
            .all(|command| command.args != ["update"])
    );
    let pair = apply_runner
        .commands
        .iter()
        .find(|command| command.args.iter().any(|argument| argument == "pair"))
        .unwrap();
    assert_eq!(pair.program, std::path::Path::new("/usr/bin/setpriv"));
    assert_eq!(pair.stdin, format!("{TOKEN}\n").into_bytes());
    assert_eq!(pair.stderr, CommandStderr::Inherit);
    assert!(pair.args.iter().all(|argument| argument != TOKEN));
    assert!(apply_runner.commands.iter().all(|command| {
        command.program != std::path::Path::new("/usr/bin/curl")
            && command.program != std::path::Path::new("/usr/bin/sudo")
            && command.args.iter().all(|argument| argument != TOKEN)
            && command.env.values().all(|value| value != TOKEN)
    }));
    assert!(install_paths.config.is_file());
    assert!(install_paths.ca.is_file());
    assert_eq!(
        fs::read_to_string(&install_paths.firewall_config).unwrap(),
        "VONK_NAS_MANAGEMENT_IP=192.168.1.231\nVONK_NODE_MANAGEMENT_IP=192.168.1.211\nVONK_NODE_FABRIC_IP=192.168.100.10\nVONK_PEER_FABRIC_IP=192.168.100.11\nVONK_ENDPOINT_HOST_PORTS=8000,8101\nVONK_HOST_ENDPOINT_PORTS=8888\nVONK_RENDEZVOUS_PORT=29500\n"
    );
    assert_eq!(
        fs::metadata(&install_paths.firewall_config)
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(
        fs::read_to_string(&install_paths.helper_authority).unwrap(),
        format!("{}\n", "11".repeat(32))
    );
    let agent_config = fs::read_to_string(&install_paths.config).unwrap();
    assert!(agent_config.contains("fabric_address = \"192.168.100.10\"\n"));
    assert!(agent_config.contains("fabric_bandwidth_mbps = 200000\n"));
    assert_eq!(
        fs::read_to_string(&install_paths.hosts).unwrap(),
        "127.0.0.1 localhost\n# BEGIN VONK FORGE MANAGED HOSTS\n192.168.1.231 control.example.test enroll.example.test controller.example.test registry.example.test\n# END VONK FORGE MANAGED HOSTS\n"
    );
    assert_eq!(
        fs::read_to_string(install_paths.config.with_file_name("setup-state")).unwrap(),
        "paired-v1\n"
    );
    let readiness_probe = apply_runner
        .commands
        .iter()
        .find(|command| {
            command.program == install_paths.agent
                && command
                    .args
                    .iter()
                    .any(|argument| argument == "verify-readiness")
        })
        .expect("readiness probe");
    assert_eq!(readiness_probe.stderr, CommandStderr::Suppress);
    assert!(apply_runner.commands.iter().any(|command| {
        command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args
                == [
                    "enable",
                    "--now",
                    "vonk-forge-docker-firewall.service",
                    "vonk-forge-package-helper.socket",
                    "vonk-forge-agent.service",
                    "vonk-forge-monitor.service",
                ]
    }));
}

#[test]
fn pairing_recovery_prompts_once_before_sudo_and_uses_the_same_narrow_apply_path() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "unpaired-v1\n");
    let mut prompt = TokenOnlyPrompt { secrets: 0 };
    let mut prepare_runner = RecordingRunner::default();
    let prepared = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();

    assert_eq!(prompt.secrets, 1);
    assert!(prepare_runner.commands.is_empty());
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(
        &prepared,
        &mut handoff_runner,
        &ReleaseAuthority::canonical(),
    )
    .unwrap();
    let mut apply_runner = RecordingRunner::default();
    apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut apply_runner,
        CallerIdentity::sudo_root(1000),
    )
    .unwrap();

    let pair = apply_runner
        .commands
        .iter()
        .find(|command| command.args.iter().any(|argument| argument == "pair"))
        .unwrap();
    assert_eq!(pair.stdin, format!("{TOKEN}\n").into_bytes());
    assert_eq!(
        fs::read_to_string(install_paths.config.with_file_name("setup-state")).unwrap(),
        "paired-v1\n"
    );

    // A matching dpkg version alone is insufficient: a changed installed
    // binary must make the next run repair the accepted package.
    fs::write(&install_paths.agent, b"changed installed agent").unwrap();
    let repair = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut NoPrompt,
        &mut RecordingRunner::default(),
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    let mut repair_handoff = RecordingRunner::default();
    handoff_to_root_with_authority(&repair, &mut repair_handoff, &ReleaseAuthority::canonical())
        .unwrap();
    let mut repair_runner = RecordingRunner {
        installed_identity: true,
        ..Default::default()
    };
    apply_setup_from(
        repair_handoff.commands[0].stdin.as_slice(),
        repair.package_path(),
        repair.executable_path(),
        &install_paths,
        &mut repair_runner,
        CallerIdentity::sudo_root(1000),
    )
    .unwrap();
    assert!(repair_runner.commands.iter().any(|command| command.program
        == std::path::Path::new("/usr/bin/apt-get")
        && command.args.first().map(String::as_str) == Some("install")));
}

#[test]
fn start_converges_when_reset_failed_reports_unit_not_loaded() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "paired-v1\n");
    let mut prompt = TokenOnlyPrompt { secrets: 0 };
    let mut prepare_runner = RecordingRunner::default();
    let prepared = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(
        &prepared,
        &mut handoff_runner,
        &ReleaseAuthority::canonical(),
    )
    .unwrap();
    let mut apply_runner = RecordingRunner {
        fail_reset_failed: true,
        ..Default::default()
    };

    apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut apply_runner,
        CallerIdentity::sudo_root(1000),
    )
    .unwrap();

    assert!(apply_runner.commands.iter().any(|command| {
        command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args == ["reset-failed", "vonk-forge-agent.service"]
    }));
    assert!(apply_runner.commands.iter().any(|command| {
        command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args
                == [
                    "enable",
                    "--now",
                    "vonk-forge-docker-firewall.service",
                    "vonk-forge-package-helper.socket",
                    "vonk-forge-agent.service",
                    "vonk-forge-monitor.service",
                ]
    }));
}

#[test]
fn explicit_reenrollment_replaces_paired_and_recovering_identities() {
    for state in ["paired-v1\n", "recovering-v1\n"] {
        let temporary = tempdir().unwrap();
        let install_paths = paths(temporary.path());
        let ca = controller_ca();
        configured_install(&install_paths, &ca, state);
        let mut prompt = TokenOnlyPrompt { secrets: 0 };
        let mut prepare_runner = runner_with_bootstrap(&ca);
        let prepared = prepare_setup(
            &request(temporary.path()).with_enroll(true),
            &install_paths,
            &mut prompt,
            &mut prepare_runner,
            CallerIdentity::unprivileged(1000),
        )
        .unwrap();

        assert_eq!(prompt.secrets, 1);
        // Planning reads the advertised controller CA once, over the TOFU
        // bootstrap, and records no other command.
        assert_eq!(prepare_runner.commands.len(), 1);
        assert!(
            prepare_runner.commands[0]
                .args
                .iter()
                .any(|argument| argument == "--insecure")
        );
        let mut handoff_runner = RecordingRunner::default();
        handoff_to_root_with_authority(
            &prepared,
            &mut handoff_runner,
            &ReleaseAuthority::canonical(),
        )
        .unwrap();
        let mut apply_runner = RecordingRunner::default();
        apply_setup_from(
            handoff_runner.commands[0].stdin.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut apply_runner,
            CallerIdentity::sudo_root(1000),
        )
        .unwrap();

        let pair = apply_runner
            .commands
            .iter()
            .find(|command| command.args.iter().any(|argument| argument == "pair"))
            .unwrap();
        assert_eq!(pair.stdin, format!("{TOKEN}\n").into_bytes());
        let pair_position = apply_runner
            .commands
            .iter()
            .position(|command| command.args.iter().any(|argument| argument == "pair"))
            .unwrap();
        let stop_position = apply_runner
            .commands
            .iter()
            .position(|command| command.args == ["stop", "vonk-forge-agent.service"])
            .unwrap();
        assert!(
            pair_position < stop_position,
            "a rejected grant must not stop a healthy agent"
        );
        assert!(apply_runner.commands.iter().any(|command| {
            command.program == std::path::Path::new("/usr/bin/systemctl")
                && command.args == ["stop", "vonk-forge-agent.service"]
        }));
        assert!(apply_runner.commands.iter().any(|command| {
            command.program == std::path::Path::new("/usr/bin/systemctl")
                && command.args == ["reset-failed", "vonk-forge-agent.service"]
        }));
        let reload_position = apply_runner
            .commands
            .iter()
            .position(|command| {
                command.program == std::path::Path::new("/usr/bin/systemctl")
                    && command.args == ["daemon-reload"]
            })
            .unwrap();
        let reset_position = apply_runner
            .commands
            .iter()
            .position(|command| {
                command.program == std::path::Path::new("/usr/bin/systemctl")
                    && command.args == ["reset-failed", "vonk-forge-agent.service"]
            })
            .unwrap();
        assert!(reload_position < reset_position);
        assert!(apply_runner.commands.iter().any(|command| {
            command.program == std::path::Path::new("/usr/bin/systemctl")
                && command.args
                    == [
                        "enable",
                        "--now",
                        "vonk-forge-docker-firewall.service",
                        "vonk-forge-package-helper.socket",
                        "vonk-forge-agent.service",
                        "vonk-forge-monitor.service",
                    ]
        }));
        assert_eq!(
            fs::read_to_string(install_paths.config.with_file_name("setup-state")).unwrap(),
            "paired-v1\n"
        );
    }
}

#[test]
fn reenrollment_refuses_a_rotated_controller_ca_before_any_write() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let stored_ca = controller_ca();
    let rotated_ca = controller_ca();
    assert_ne!(stored_ca, rotated_ca);
    configured_install(&install_paths, &stored_ca, "paired-v1\n");
    let config_before = fs::read(&install_paths.config).unwrap();
    let ca_before = fs::read(&install_paths.ca).unwrap();
    let mut prompt = TokenOnlyPrompt { secrets: 0 };
    let mut prepare_runner = runner_with_bootstrap(&rotated_ca);

    let result = prepare_setup(
        &request(temporary.path()).with_enroll(true),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    );

    match result {
        Err(SetupError::ControllerCaChanged { stored, advertised }) => {
            assert_eq!(stored, ca_fingerprint(&stored_ca));
            assert_eq!(advertised, ca_fingerprint(&rotated_ca));
        }
        Err(other) => panic!("unexpected error: {other}"),
        Ok(_) => panic!("a rotated controller CA must be refused"),
    }
    assert_eq!(
        prompt.secrets, 0,
        "the CA change must fail closed before a grant is requested"
    );
    assert_eq!(
        fs::read(&install_paths.config).unwrap(),
        config_before,
        "validation must happen before release-controlled state changes"
    );
    assert_eq!(
        fs::read(&install_paths.ca).unwrap(),
        ca_before,
        "planning must not rewrite the stored CA"
    );
}

#[test]
fn reenrollment_proceeds_when_the_advertised_ca_matches() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "paired-v1\n");
    let config_before = fs::read(&install_paths.config).unwrap();
    let mut prompt = TokenOnlyPrompt { secrets: 0 };
    let mut prepare_runner = runner_with_bootstrap(&ca);

    prepare_setup(
        &request(temporary.path()).with_enroll(true),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .expect("a matching advertised CA re-enrolls as before");

    assert_eq!(prompt.secrets, 1, "a matching CA still asks for one grant");
    assert_eq!(prepare_runner.commands.len(), 1);
    let bootstrap = &prepare_runner.commands[0];
    assert_eq!(bootstrap.program, std::path::Path::new("/usr/bin/curl"));
    assert!(
        bootstrap
            .args
            .iter()
            .any(|argument| argument == "https://enroll.example.test/agent/bootstrap"),
        "the comparison reads the controller bootstrap endpoint"
    );
    assert_eq!(
        fs::read(&install_paths.config).unwrap(),
        config_before,
        "planning must not rewrite the stored configuration"
    );
}

#[test]
fn failed_post_pair_readiness_is_resumed_without_another_token() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "paired-v1\n");
    let mut prompt = TokenOnlyPrompt { secrets: 0 };
    let mut prepare_runner = runner_with_bootstrap(&ca);
    let prepared = prepare_setup(
        &request(temporary.path()).with_enroll(true),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(
        &prepared,
        &mut handoff_runner,
        &ReleaseAuthority::canonical(),
    )
    .unwrap();
    let mut failing_runner = FailingReadinessRunner::default();

    let result = apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut failing_runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(result.is_err());
    let readiness_probes = failing_runner
        .commands
        .iter()
        .filter(|command| {
            command
                .args
                .iter()
                .any(|argument| argument == "verify-readiness")
        })
        .collect::<Vec<_>>();
    // The bounded readiness window is 180 seconds with a two-second sample
    // interval, followed by one inherited-stderr diagnostic probe.
    assert_eq!(readiness_probes.len(), 91);
    assert!(
        readiness_probes[..90]
            .iter()
            .all(|command| command.stderr == CommandStderr::Suppress)
    );
    assert_eq!(readiness_probes[90].stderr, CommandStderr::Inherit);
    assert_eq!(
        fs::read_to_string(install_paths.config.with_file_name("setup-state")).unwrap(),
        "recovering-v1\n"
    );

    let mut retry_prompt = NoPrompt;
    let mut retry_prepare_runner = RecordingRunner::default();
    let retry = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut retry_prompt,
        &mut retry_prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    let mut retry_handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(
        &retry,
        &mut retry_handoff_runner,
        &ReleaseAuthority::canonical(),
    )
    .unwrap();
    assert!(
        !retry_handoff_runner.commands[0]
            .stdin
            .windows(TOKEN.len())
            .any(|value| value == TOKEN.as_bytes())
    );
    let mut retry_apply_runner = RecordingRunner {
        installed_identity: true,
        ..Default::default()
    };
    apply_setup_from(
        retry_handoff_runner.commands[0].stdin.as_slice(),
        retry.package_path(),
        retry.executable_path(),
        &install_paths,
        &mut retry_apply_runner,
        CallerIdentity::sudo_root(1000),
    )
    .unwrap();

    assert!(
        retry_apply_runner
            .commands
            .iter()
            .all(|command| !command.args.iter().any(|argument| argument == "pair"))
    );
    assert!(
        retry_apply_runner
            .commands
            .iter()
            .all(|command| command.program != std::path::Path::new("/usr/bin/apt-get"))
    );
    assert_eq!(
        fs::read_to_string(install_paths.config.with_file_name("setup-state")).unwrap(),
        "paired-v1\n"
    );
}

#[test]
fn stale_bootstrap_observation_is_refetched_before_effects() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let ca = controller_ca();
    let mut runner = runner_with_bootstrap(&ca);
    let mut bootstrap: vonk_agent_protocol::generated::EnrollmentBootstrapResponse =
        serde_json::from_slice(&runner.outputs.front().unwrap().stdout).unwrap();
    bootstrap.ca_fingerprint = "0".repeat(64);
    runner.outputs.push_front(CommandOutput::success(
        serde_json::to_vec(&bootstrap).unwrap(),
    ));
    let mut prompt = fresh_answers(&ca);

    let result = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
    );

    let prepared = result.unwrap();
    assert!(!install_paths.config.exists());
    assert!(!install_paths.helper_authority.exists());
    assert!(runner.commands.iter().all(|command| matches!(
        command.program.to_str(),
        Some("/usr/bin/curl" | "/usr/sbin/ip")
    )));
    assert_eq!(
        runner
            .commands
            .iter()
            .filter(|command| command.program == std::path::Path::new("/usr/bin/curl"))
            .count(),
        3
    );
    let mut handoff = RecordingRunner::default();
    handoff_to_root_with_authority(&prepared, &mut handoff, &ReleaseAuthority::canonical())
        .unwrap();
    assert!(
        apply_setup_from(
            handoff.commands[0].stdin.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut RecordingRunner::default(),
            CallerIdentity::sudo_root(1000)
        )
        .is_ok()
    );
    assert_eq!(fs::read(&install_paths.ca).unwrap(), ca);
    assert!(
        prepare_setup(
            &request(temporary.path()),
            &install_paths,
            &mut NoPrompt,
            &mut RecordingRunner::default(),
            CallerIdentity::unprivileged(1000)
        )
        .is_ok()
    );
}

#[test]
fn successful_pairing_enters_recovery_before_later_service_startup() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    let mut runner = RecordingRunner {
        commands: Vec::new(),
        outputs: [
            CommandOutput::success_empty(),
            CommandOutput::success_empty(),
            CommandOutput::success_empty(),
            CommandOutput::success_empty(),
            CommandOutput {
                success: false,
                stdout: Vec::new(),
                stderr: Vec::new(),
            },
        ]
        .into(),
        ..Default::default()
    };

    let result = apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(result.is_err());
    assert_eq!(
        fs::read_to_string(install_paths.config.with_file_name("setup-state")).unwrap(),
        "recovering-v1\n",
        "a retry must resume readiness without asking for an already consumed token"
    );
}

#[test]
fn lost_pair_response_resumes_observation_without_another_grant() {
    struct LostPairResponse {
        inner: RecordingRunner,
        accepted_pairs: usize,
    }
    impl CommandRunner for LostPairResponse {
        fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
            let pairing = command.args.iter().any(|argument| argument == "pair");
            let output = self.inner.run(command)?;
            if pairing {
                self.accepted_pairs += 1;
                return Err("response unavailable".to_owned());
            }
            Ok(output)
        }
        fn authenticate_sudo(&mut self, _sudo: &std::path::Path) -> Result<(), SetupError> {
            Ok(())
        }
        fn sleep(&mut self, _duration: std::time::Duration) {}
    }
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(
        &install_paths,
        &ca,
        &format!(
            "{}\n",
            vonk_agent_protocol::generated::SparkInstallationState::UnpairedV1
        ),
    );
    let setup_request = request(temporary.path());
    let mut prompt = TokenOnlyPrompt { secrets: 0 };
    let prepared = prepare_setup(
        &setup_request,
        &install_paths,
        &mut prompt,
        &mut RecordingRunner::default(),
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    let mut handoff = RecordingRunner::default();
    handoff_to_root_with_authority(&prepared, &mut handoff, &ReleaseAuthority::canonical())
        .unwrap();
    let mut lost = LostPairResponse {
        inner: RecordingRunner::default(),
        accepted_pairs: 0,
    };
    assert!(
        apply_setup_from(
            handoff.commands[0].stdin.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut lost,
            CallerIdentity::sudo_root(1000)
        )
        .is_err()
    );
    assert_eq!(lost.accepted_pairs, 1);
    assert_eq!(prompt.secrets, 1);
    let resumed = prepare_setup(
        &setup_request,
        &install_paths,
        &mut NoPrompt,
        &mut RecordingRunner::default(),
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    let mut handoff = RecordingRunner::default();
    handoff_to_root_with_authority(&resumed, &mut handoff, &ReleaseAuthority::canonical()).unwrap();
    let mut observed = RecordingRunner::default();
    apply_setup_from(
        handoff.commands[0].stdin.as_slice(),
        resumed.package_path(),
        resumed.executable_path(),
        &install_paths,
        &mut observed,
        CallerIdentity::sudo_root(1000),
    )
    .unwrap();
    assert!(
        observed
            .commands
            .iter()
            .all(|command| !command.args.iter().any(|argument| argument == "pair"))
    );
    assert!(
        prepare_setup(
            &setup_request,
            &install_paths,
            &mut NoPrompt,
            &mut RecordingRunner::default(),
            CallerIdentity::unprivileged(1000)
        )
        .is_ok()
    );
}
