use super::*;

#[test]
fn existing_upgrade_never_prompts_or_discovers_and_restarts_through_apply() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "paired-v1\n");
    let mut prompt = NoPrompt;
    let mut prepare_runner = RecordingRunner::default();
    let prepared = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();

    assert!(prepare_runner.commands.is_empty());
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(
        &prepared,
        &mut handoff_runner,
        &ReleaseAuthority::canonical(),
    )
    .unwrap();
    assert!(
        !handoff_runner.commands[0]
            .stdin
            .windows(TOKEN.len())
            .any(|value| value == TOKEN.as_bytes())
    );
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

    assert!(apply_runner.commands.iter().any(|command| {
        command.program == std::path::Path::new("/usr/bin/systemctl")
            && command.args == ["restart", "vonk-forge-agent.service"]
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
    assert!(
        apply_runner
            .commands
            .iter()
            .all(|command| !command.args.iter().any(|argument| argument == "pair"))
    );
}

#[test]
fn damaged_generated_files_are_repaired_without_replacing_enrollment() {
    for fault in 0..6 {
        let temporary = tempdir().unwrap();
        let install_paths = paths(temporary.path());
        let ca = controller_ca();
        configured_install(&install_paths, &ca, "paired-v1\n");
        let identity = fs::read(&install_paths.config).unwrap();
        match fault {
            0 => fs::remove_file(&install_paths.agent).unwrap(),
            1 => fs::write(
                install_paths.config.parent().unwrap().join("setup-state"),
                b"damaged",
            )
            .unwrap(),
            2 => fs::write(&install_paths.firewall_config, b"damaged").unwrap(),
            _ => {
                let path = if fault == 3 {
                    install_paths.config.parent().unwrap().join("setup-state")
                } else if fault == 4 {
                    install_paths.firewall_config.clone()
                } else {
                    install_paths.agent.clone()
                };
                fs::remove_file(&path).unwrap();
                fs::create_dir(&path).unwrap();
                fs::write(path.join("preserved"), b"old local state").unwrap();
            }
        }
        let mut prompt = FreshAnswers {
            values: [
                "192.168.1.231",
                "192.168.1.211",
                "192.168.100.10",
                "192.168.100.11",
            ]
            .map(str::to_owned)
            .into(),
        };
        let mut runner = RecordingRunner::default();
        let prepared = prepare_setup(
            &request(temporary.path()),
            &install_paths,
            &mut prompt,
            &mut runner,
            CallerIdentity::unprivileged(1000),
        )
        .unwrap();
        let mut handoff = RecordingRunner::default();
        handoff_to_root_with_authority(&prepared, &mut handoff, &ReleaseAuthority::canonical())
            .unwrap();
        let mut apply = RecordingRunner::default();
        assert!(
            apply_setup_from(
                handoff.commands[0].stdin.as_slice(),
                prepared.package_path(),
                prepared.executable_path(),
                &install_paths,
                &mut apply,
                CallerIdentity::sudo_root(1000)
            )
            .is_ok()
        );
        assert_eq!(fs::read(&install_paths.config).unwrap(), identity);
        if fault >= 3 {
            assert!(
                [
                    install_paths.config.parent().unwrap(),
                    install_paths.agent.parent().unwrap()
                ]
                .into_iter()
                .flat_map(|directory| fs::read_dir(directory).unwrap().flatten())
                .any(|entry| fs::read(entry.path().join("preserved"))
                    .is_ok_and(|bytes| bytes == b"old local state"))
            );
        }
        assert!(
            apply
                .commands
                .iter()
                .all(|command| !command.args.iter().any(|argument| argument == "pair"))
        );
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
}

#[test]
fn damaged_security_material_is_refetched_through_pinned_authority() {
    for missing_ca in [true, false] {
        let temporary = tempdir().unwrap();
        let paths = paths(temporary.path());
        let ca = controller_ca();
        configured_install(&paths, &ca, "paired-v1\n");
        if missing_ca {
            fs::remove_file(&paths.ca).unwrap();
        } else {
            fs::write(&paths.helper_authority, b"damaged").unwrap();
        }
        let mut runner = runner_with_bootstrap(&ca);
        // A lost or malformed peer reply is observed again within this request.
        runner
            .outputs
            .push_front(CommandOutput::success(b"unparseable peer reply".to_vec()));
        let prepared = prepare_setup(
            &request(temporary.path()),
            &paths,
            &mut NoPrompt,
            &mut runner,
            CallerIdentity::unprivileged(1000),
        )
        .unwrap();
        let mut handoff = RecordingRunner::default();
        handoff_to_root_with_authority(&prepared, &mut handoff, &ReleaseAuthority::canonical())
            .unwrap();
        let mut apply = runner_with_bootstrap(&ca);
        assert!(
            apply_setup_from(
                handoff.commands[0].stdin.as_slice(),
                prepared.package_path(),
                prepared.executable_path(),
                &paths,
                &mut apply,
                CallerIdentity::sudo_root(1000)
            )
            .is_ok()
        );
        assert_eq!(fs::read(&paths.ca).unwrap(), ca);
        assert_eq!(
            fs::read_to_string(&paths.helper_authority).unwrap(),
            format!("{}\n", "11".repeat(32))
        );
        assert!(
            prepare_setup(
                &request(temporary.path()),
                &paths,
                &mut NoPrompt,
                &mut RecordingRunner::default(),
                CallerIdentity::unprivileged(1000)
            )
            .is_ok()
        );
    }
}

#[test]
fn peer_loss_ends_boundedly_then_a_fresh_request_repairs() {
    let temporary = tempdir().unwrap();
    let paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&paths, &ca, "paired-v1\n");
    fs::remove_file(&paths.helper_authority).unwrap();
    let mut lost = RecordingRunner::default();
    assert!(
        prepare_setup(
            &request(temporary.path()),
            &paths,
            &mut NoPrompt,
            &mut lost,
            CallerIdentity::unprivileged(1000)
        )
        .is_err()
    );
    assert_eq!(lost.commands.len(), 3);
    assert!(!paths.helper_authority.exists());
    assert!(
        prepare_setup(
            &request(temporary.path()),
            &paths,
            &mut NoPrompt,
            &mut runner_with_bootstrap(&ca),
            CallerIdentity::unprivileged(1000)
        )
        .is_ok()
    );
}

#[test]
fn tty_prompt_construction_is_lazy_for_headless_upgrades() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "paired-v1\n");
    let setup_request = request(temporary.path());
    let mut prompt = TtyPrompt::new();
    let mut runner = RecordingRunner::default();

    let result = prepare_setup(
        &setup_request,
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
    );

    assert!(result.is_ok());
    assert!(runner.commands.is_empty());
}

#[test]
fn apply_repairs_late_projection_damage_from_retained_enrollment() {
    for fault in 0..5 {
        let temporary = tempdir().unwrap();
        let install_paths = paths(temporary.path());
        let ca = controller_ca();
        configured_install(
            &install_paths,
            &ca,
            &format!(
                "{}\n",
                vonk_agent_protocol::generated::SparkInstallationState::PairedV1
            ),
        );
        // First normal apply publishes current retained enrollment and firewall.
        let setup_request = request(temporary.path());
        let prepared = prepare_setup(
            &setup_request,
            &install_paths,
            &mut NoPrompt,
            &mut RecordingRunner::default(),
            CallerIdentity::unprivileged(1000),
        )
        .unwrap();
        let mut handoff = RecordingRunner::default();
        handoff_to_root_with_authority(&prepared, &mut handoff, &ReleaseAuthority::canonical())
            .unwrap();
        apply_setup_from(
            handoff.commands[0].stdin.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut RecordingRunner::default(),
            CallerIdentity::sudo_root(1000),
        )
        .unwrap();
        let expected_config = fs::read(&install_paths.config).unwrap();
        let expected_firewall = fs::read(&install_paths.firewall_config).unwrap();
        let prepared = prepare_setup(
            &setup_request,
            &install_paths,
            &mut NoPrompt,
            &mut RecordingRunner::default(),
            CallerIdentity::unprivileged(1000),
        )
        .unwrap();
        let damaged = match fault {
            0 | 1 => &install_paths.config,
            2 => &install_paths.firewall_config,
            3 => &install_paths.ca,
            _ => &install_paths.helper_authority,
        };
        if fault == 1 {
            fs::remove_file(damaged).unwrap();
            fs::create_dir(damaged).unwrap();
            fs::write(damaged.join("preserved"), b"uncertain bytes").unwrap();
        } else {
            fs::write(damaged, b"damaged after preparation").unwrap();
        }
        let mut handoff = RecordingRunner::default();
        handoff_to_root_with_authority(&prepared, &mut handoff, &ReleaseAuthority::canonical())
            .unwrap();
        let mut apply = if fault >= 3 {
            runner_with_bootstrap(&ca)
        } else {
            RecordingRunner::default()
        };
        apply_setup_from(
            handoff.commands[0].stdin.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut apply,
            CallerIdentity::sudo_root(1000),
        )
        .unwrap();
        assert_eq!(fs::read(&install_paths.config).unwrap(), expected_config);
        assert_eq!(
            fs::read(&install_paths.firewall_config).unwrap(),
            expected_firewall
        );
        assert_eq!(fs::read(&install_paths.ca).unwrap(), ca);
        if fault == 1 {
            assert!(
                fs::read_dir(install_paths.config.parent().unwrap())
                    .unwrap()
                    .flatten()
                    .any(|entry| fs::read(entry.path().join("preserved"))
                        .is_ok_and(|bytes| bytes == b"uncertain bytes"))
            );
        }
        assert!(
            apply
                .commands
                .iter()
                .all(|command| !command.args.iter().any(|arg| arg == "pair"))
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
}

#[test]
fn relocated_signed_package_is_accepted_without_filename_identity() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(
        &install_paths,
        &ca,
        &format!(
            "{}\n",
            vonk_agent_protocol::generated::SparkInstallationState::PairedV1
        ),
    );
    let relocated = temporary.path().join("content.deb");
    package(&relocated);
    let setup_request = request_for_package(temporary.path(), relocated);
    let prepared = prepare_setup(
        &setup_request,
        &install_paths,
        &mut NoPrompt,
        &mut RecordingRunner::default(),
        CallerIdentity::unprivileged(1000),
    )
    .unwrap();
    let mut handoff = RecordingRunner::default();
    handoff_to_root_with_authority(&prepared, &mut handoff, &ReleaseAuthority::canonical())
        .unwrap();
    apply_setup_from(
        handoff.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut RecordingRunner::default(),
        CallerIdentity::sudo_root(1000),
    )
    .unwrap();
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
