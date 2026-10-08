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
fn existing_upgrade_preparation_does_not_read_root_private_firewall_state() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "paired-v1\n");
    fs::set_permissions(
        &install_paths.firewall_config,
        std::fs::Permissions::from_mode(0o000),
    )
    .unwrap();
    let mut prompt = NoPrompt;
    let mut runner = RecordingRunner::default();

    let result = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
    );

    assert!(result.is_ok());
    assert!(runner.commands.is_empty());
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
