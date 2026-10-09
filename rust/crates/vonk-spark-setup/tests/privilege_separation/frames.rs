use super::*;

#[test]
fn privileged_frame_fails_closed_when_truncated_tampered_oversized_or_extended() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    let valid = handoff_runner.commands[0].stdin.clone();
    let mut truncated = valid.clone();
    truncated.pop();
    let mut tampered = valid.clone();
    let middle = tampered.len() / 2;
    tampered[middle] ^= 1;
    let mut extended = valid.clone();
    extended.push(0);
    let oversized = vec![0_u8; 300 * 1024];

    for malformed in [truncated, tampered, extended, oversized] {
        let mut runner = RecordingRunner::default();
        let result = apply_setup_from(
            malformed.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut runner,
            CallerIdentity::sudo_root(1000),
        );
        assert!(result.is_err());
        assert!(runner.commands.is_empty());
        assert!(!install_paths.config.exists());
    }
}

#[test]
fn apply_frame_is_bound_to_the_authenticated_sudo_caller() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    let frame = &handoff_runner.commands[0].stdin;

    for caller in [
        CallerIdentity::unprivileged(1000),
        CallerIdentity::direct_root(),
        CallerIdentity::sudo_root(1001),
    ] {
        let mut runner = RecordingRunner::default();
        let result = apply_setup_from(
            frame.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut runner,
            caller,
        );
        assert!(result.is_err());
        assert!(runner.commands.is_empty());
    }
}

#[test]
fn public_preparation_rejects_root_before_prompting_or_network_io() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let mut runner = RecordingRunner::default();
    let mut prompt = NoPrompt;

    let result = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::direct_root(),
    );

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
}

#[test]
fn markerless_agent_only_state_prepares_authenticated_bootstrap() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    fs::create_dir_all(install_paths.agent.parent().unwrap()).unwrap();
    fs::write(&install_paths.agent, b"partial agent install").unwrap();
    let (prepared, handoff) = fresh_prepared(temporary.path(), &install_paths);
    let mut runner = RecordingRunner::default();
    assert!(
        apply_setup_from(
            handoff.commands[0].stdin.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut runner,
            CallerIdentity::sudo_root(1000)
        )
        .is_ok()
    );
    assert!(install_paths.config.exists());
    assert!(install_paths.ca.exists());
    let mut next_runner = RecordingRunner::default();
    assert!(
        prepare_setup(
            &request(temporary.path()),
            &install_paths,
            &mut NoPrompt,
            &mut next_runner,
            CallerIdentity::unprivileged(1000)
        )
        .is_ok()
    );
}

#[test]
fn apply_rejects_missing_ingress_bytes_before_effects() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    configured_install(&install_paths, &controller_ca(), "unpaired-v1\n");
    let mut runner = RecordingRunner::default();
    let missing_package = temporary.path().join("missing-package.deb");

    let result = apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        &missing_package,
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
    assert!(
        apply_setup_from(
            handoff_runner.commands[0].stdin.as_slice(),
            prepared.package_path(),
            prepared.executable_path(),
            &install_paths,
            &mut runner,
            CallerIdentity::sudo_root(1000)
        )
        .is_ok()
    );
    assert!(install_paths.config.is_file());
}

#[test]
fn package_mutation_after_preparation_is_rejected_before_root_commands() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    fs::OpenOptions::new()
        .append(true)
        .open(prepared.package_path())
        .unwrap()
        .write_all(b"changed")
        .unwrap();
    let mut runner = RecordingRunner::default();

    let result = apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
}

#[test]
fn root_rejects_a_setup_binary_changed_after_unprivileged_verification() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    fs::write(prepared.executable_path(), b"changed setup executable").unwrap();
    let mut runner = RecordingRunner::default();

    let result = apply_setup_from(
        handoff_runner.commands[0].stdin.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
}

#[test]
fn package_format_identity_and_release_name_are_verified_before_prompt_or_sudo() {
    for case in ["format", "identity", "name"] {
        let temporary = tempdir().unwrap();
        let install_paths = paths(temporary.path());
        fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
        let package_path = if case == "name" {
            temporary.path().join("release.deb")
        } else {
            temporary.path().join(package_filename())
        };
        match case {
            "format" => fs::write(&package_path, vec![b'x'; 80]).unwrap(),
            "identity" => package_with_identity(
                &package_path,
                "vonk-forge-agent",
                "1.0.0",
                native_release_identity().wrong_architecture,
            ),
            "name" => package(&package_path),
            _ => unreachable!(),
        }
        let setup_request = request_for_package(temporary.path(), package_path);
        let mut prompt = NoPrompt;
        let mut runner = RecordingRunner::default();

        let result = prepare_setup(
            &setup_request,
            &install_paths,
            &mut prompt,
            &mut runner,
            CallerIdentity::unprivileged(1000),
        );

        let exact_error = match case {
            "format" => matches!(result, Err(SetupError::PackageFormat)),
            "identity" => matches!(result, Err(SetupError::PackageIdentity)),
            "name" => matches!(result, Err(SetupError::UnsafePackage)),
            _ => unreachable!(),
        };
        assert!(exact_error, "{case} must be rejected by its exact boundary");
        assert!(runner.commands.is_empty());
    }
}

#[test]
fn privileged_plan_rejects_unknown_fields_even_with_a_valid_frame_digest() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    let valid = &handoff_runner.commands[0].stdin;
    let frame = rewrite_frame(valid, |plan| {
        plan.insert("unexpected".to_owned(), serde_json::Value::Bool(true));
    });
    let mut runner = RecordingRunner::default();

    let result = apply_setup_from(
        frame.as_slice(),
        prepared.package_path(),
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(result.is_err());
    assert!(runner.commands.is_empty());
}

#[test]
fn semantic_plan_validation_precedes_any_root_artifact_processing() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    let (prepared, handoff_runner) = fresh_prepared(temporary.path(), &install_paths);
    let frame = rewrite_frame(&handoff_runner.commands[0].stdin, |plan| {
        plan.insert(
            "pairing_token".to_owned(),
            serde_json::Value::String("invalid".to_owned()),
        );
    });
    let missing_package = temporary.path().join("missing-package.deb");
    let mut runner = RecordingRunner::default();

    let result = apply_setup_from(
        frame.as_slice(),
        &missing_package,
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(matches!(result, Err(SetupError::PrivilegedInput)));
    assert!(runner.commands.is_empty());
}

#[test]
fn pairing_plan_must_match_root_owned_configuration_before_package_processing() {
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
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(
        &prepared,
        &mut handoff_runner,
        &ReleaseAuthority::canonical(),
    )
    .unwrap();
    let frame = rewrite_frame(&handoff_runner.commands[0].stdin, |plan| {
        plan.insert(
            "ca_sha256".to_owned(),
            serde_json::Value::String("0".repeat(64)),
        );
    });
    let mut runner = RecordingRunner::default();

    let result = apply_setup_from(
        frame.as_slice(),
        &temporary.path().join("missing-package.deb"),
        prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
    );

    assert!(matches!(result, Err(SetupError::PrivilegedInput)));
    assert!(runner.commands.is_empty());
}

#[test]
fn system_path_operations_cannot_use_a_synthetic_caller_identity() {
    let temporary = tempdir().unwrap();
    let mut install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    install_paths.required_owner = Some(0);
    let ca = controller_ca();
    let mut prompt = fresh_answers(&ca);
    let mut runner = runner_with_bootstrap(&ca);

    let result = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::unprivileged(u32::MAX - 1),
    );

    assert!(matches!(result, Err(SetupError::CallerPhase)));
    assert!(runner.commands.is_empty());
}

#[test]
fn setup_state_below_a_symlinked_configuration_directory_fails_before_prompting() {
    let temporary = tempdir().unwrap();
    let real_directory = temporary.path().join("real-configuration");
    fs::create_dir_all(&real_directory).unwrap();
    let unsafe_directory = temporary.path().join("etc/vonk-forge-agent");
    fs::create_dir_all(unsafe_directory.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink(&real_directory, &unsafe_directory).unwrap();
    let install_paths = InstallPaths {
        config: unsafe_directory.join("agent.toml"),
        ca: unsafe_directory.join("controller-ca.pem"),
        firewall_config: unsafe_directory.join("docker-firewall.conf"),
        helper_authority: unsafe_directory.join("host-helper-authority.pub"),
        hosts: temporary.path().join("etc/hosts"),
        agent: temporary.path().join("usr/lib/vonk-forge/vonk-agent"),
        staging_root: temporary.path().join("var/tmp"),
        sudo: PathBuf::from("/usr/bin/sudo"),
        service: "vonk-forge-agent.service".to_owned(),
        required_owner: None,
    };
    let ca = controller_ca();
    configured_install(&install_paths, &ca, "paired-v1\n");
    let mut prompt = TokenOnlyPrompt { secrets: 0 };
    let mut runner = RecordingRunner::default();

    let result = prepare_setup(
        &request(temporary.path()),
        &install_paths,
        &mut prompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
    );

    assert!(result.is_err());
    assert_eq!(prompt.secrets, 0);
    assert!(runner.commands.is_empty());
}
