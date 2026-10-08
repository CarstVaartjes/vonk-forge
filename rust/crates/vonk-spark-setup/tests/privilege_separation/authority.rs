use super::*;

#[test]
fn root_apply_accepts_only_artifacts_bound_by_the_trusted_signed_release() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let (request, authority) = signed_request(temporary.path());
    let ca = controller_ca();
    let mut prepare_runner = runner_with_bootstrap(&ca);
    let mut prompt = fresh_answers(&ca);
    let prepared = prepare_setup_with_authority(
        &request,
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
        &authority,
    )
    .unwrap();
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(
        &prepared,
        &mut handoff_runner,
        &ReleaseAuthority::canonical(),
    )
    .unwrap();
    let frame = handoff_runner.commands[0].stdin.clone();
    let staged_executable = root_session(temporary.path(), &prepared);

    let malicious = temporary.path().join("malicious.deb");
    package_with_identity(
        &malicious,
        "vonk-forge-agent",
        "1.0.0",
        native_release_identity().architecture,
    );
    fs::copy(
        malicious,
        staged_executable
            .parent()
            .unwrap()
            .join("vonk-forge-agent.deb"),
    )
    .unwrap();
    let mut apply_runner = RecordingRunner::default();
    let result = apply_setup_from_with_authority(
        frame.as_slice(),
        &staged_executable,
        &install_paths,
        &mut apply_runner,
        CallerIdentity::sudo_root(1000),
        &authority,
    );

    assert!(result.is_err());
    assert!(apply_runner.commands.is_empty());
    assert!(!install_paths.config.exists());
}

#[test]
fn root_apply_rejects_an_attacker_signed_release_and_non_session_execution() {
    let temporary = tempdir().unwrap();
    let trusted_root = temporary.path().join("trusted");
    let attacker_root = temporary.path().join("attacker");
    fs::create_dir_all(&trusted_root).unwrap();
    fs::create_dir_all(&attacker_root).unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let (_, trusted_authority) = signed_request(&trusted_root);
    let (attacker_request, attacker_authority) = signed_request(&attacker_root);
    let ca = controller_ca();
    let mut prepare_runner = runner_with_bootstrap(&ca);
    let mut prompt = fresh_answers(&ca);
    let attacker_prepared = prepare_setup_with_authority(
        &attacker_request,
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
        &attacker_authority,
    )
    .unwrap();
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(
        &attacker_prepared,
        &mut handoff_runner,
        &ReleaseAuthority::canonical(),
    )
    .unwrap();
    let frame = handoff_runner.commands[0].stdin.clone();
    let staged_executable = root_session(temporary.path(), &attacker_prepared);

    let mut runner = RecordingRunner::default();
    let untrusted = apply_setup_from_with_authority(
        frame.as_slice(),
        &staged_executable,
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
        &trusted_authority,
    );
    assert!(matches!(untrusted, Err(SetupError::ReleaseSignature)));
    assert!(runner.commands.is_empty());

    let mut runner = RecordingRunner::default();
    let outside_session = apply_setup_from_with_authority(
        frame.as_slice(),
        attacker_prepared.executable_path(),
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
        &attacker_authority,
    );
    assert!(matches!(outside_session, Err(SetupError::PrivilegedInput)));
    assert!(runner.commands.is_empty());
}

#[test]
fn setup_signature_must_match_the_signed_release_before_prompt_or_sudo() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let package_path = temporary.path().join(package_filename());
    package(&package_path);
    let executable = temporary.path().join("vonk-spark-setup");
    fs::write(&executable, b"verified setup executable").unwrap();
    let signed = signed_release(temporary.path(), &package_path, &executable);
    fs::write(&signed.setup_signature, b"not the published signature\n").unwrap();
    let request = SetupRequest::from_signed_release(
        package_path,
        signed.manifest,
        signed.signature,
        signed.setup_signature,
        executable,
    )
    .unwrap();
    let mut runner = RecordingRunner::default();

    let result = prepare_setup_with_authority(
        &request,
        &install_paths,
        &mut NoPrompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
        &signed.authority,
    );

    assert!(matches!(result, Err(SetupError::ReleaseSignature)));
    assert!(runner.commands.is_empty());
}
