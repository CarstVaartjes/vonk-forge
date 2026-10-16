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
    handoff_to_root_with_authority(&prepared, &mut handoff_runner, &repository_root_authority())
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
        &repository_root_authority(),
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

#[test]
fn release_root_file_refuses_anything_but_one_private_regular_pem() {
    let temporary = tempdir().unwrap();
    let valid = fs::read(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../install/installer-release-public.pem"),
    )
    .unwrap();
    let write = |name: &str, content: &[u8], mode: u32| {
        let path = temporary.path().join(name);
        fs::write(&path, content).unwrap();
        fs::set_permissions(&path, PermissionsExt::from_mode(mode)).unwrap();
        path
    };
    let accepted = write("root.pem", &valid, 0o600);
    assert!(ReleaseAuthority::from_file(&accepted).is_ok());

    let missing = temporary.path().join("missing.pem");
    let symlink = temporary.path().join("symlink.pem");
    std::os::unix::fs::symlink(&accepted, &symlink).unwrap();
    let hardlink = temporary.path().join("hardlink.pem");
    let shared = write("shared.pem", &valid, 0o600);
    fs::hard_link(&shared, &hardlink).unwrap();
    let mut oversized = valid.clone();
    oversized.extend(std::iter::repeat_n(b'A', 16 * 1024));
    let refused = [
        missing,
        symlink,
        hardlink,
        temporary.path().to_path_buf(),
        write("empty.pem", b"", 0o600),
        write("oversized.pem", &oversized, 0o600),
        write(
            "certificate.pem",
            b"-----BEGIN CERTIFICATE-----\nAA==\n",
            0o600,
        ),
        write("writable.pem", &valid, 0o622),
    ];
    for path in refused {
        assert!(
            matches!(
                ReleaseAuthority::from_file(&path),
                Err(SetupError::ReleaseSignature)
            ),
            "{} must not become a release root",
            path.display()
        );
    }
    if rustix::process::geteuid().as_raw() != 0 {
        let unreadable = write("unreadable.pem", &valid, 0o000);
        assert!(ReleaseAuthority::from_file(&unreadable).is_err());
    }
}

#[test]
fn a_root_that_did_not_sign_the_release_refuses_before_prompt_or_sudo() {
    let temporary = tempdir().unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    let (request, _) = signed_request(temporary.path());
    let mut runner = RecordingRunner::default();

    let result = prepare_setup_with_authority(
        &request,
        &install_paths,
        &mut NoPrompt,
        &mut runner,
        CallerIdentity::unprivileged(1000),
        &repository_root_authority(),
    );

    assert!(matches!(result, Err(SetupError::ReleaseSignature)));
    assert!(runner.commands.is_empty());
}

#[test]
fn root_apply_trusts_only_the_root_owned_staged_root_and_refuses_another_signer() {
    let temporary = tempdir().unwrap();
    let trusted_root = temporary.path().join("trusted");
    let attacker_root = temporary.path().join("attacker");
    fs::create_dir_all(&trusted_root).unwrap();
    fs::create_dir_all(&attacker_root).unwrap();
    let install_paths = paths(temporary.path());
    fs::create_dir_all(install_paths.config.parent().unwrap()).unwrap();
    signed_request(&trusted_root);
    let (attacker_request, attacker_authority) = signed_request(&attacker_root);
    let ca = controller_ca();
    let mut prepare_runner = runner_with_bootstrap(&ca);
    let mut prompt = fresh_answers(&ca);
    let prepared = prepare_setup_with_authority(
        &attacker_request,
        &install_paths,
        &mut prompt,
        &mut prepare_runner,
        CallerIdentity::unprivileged(1000),
        &attacker_authority,
    )
    .unwrap();
    let mut handoff_runner = RecordingRunner::default();
    handoff_to_root_with_authority(&prepared, &mut handoff_runner, &attacker_authority).unwrap();
    let frame = handoff_runner.commands[0].stdin.clone();
    let executable = root_session(temporary.path(), &prepared);
    let staged = executable.with_file_name("installer-release-public.pem");

    // No staged root: the root phase has nothing to trust and refuses.
    assert!(matches!(
        ReleaseAuthority::from_staged_session(&executable, &install_paths),
        Err(SetupError::PrivilegedInput)
    ));
    // A root that is not private to the session, or a link, is refused.
    fs::copy(trusted_root.join("installer-release-public.pem"), &staged).unwrap();
    fs::set_permissions(&staged, PermissionsExt::from_mode(0o644)).unwrap();
    assert!(ReleaseAuthority::from_staged_session(&executable, &install_paths).is_err());
    fs::remove_file(&staged).unwrap();
    std::os::unix::fs::symlink(trusted_root.join("installer-release-public.pem"), &staged).unwrap();
    assert!(ReleaseAuthority::from_staged_session(&executable, &install_paths).is_err());
    fs::remove_file(&staged).unwrap();

    // The staged root is the trusted one; the stdin frame was signed by another key.
    fs::copy(trusted_root.join("installer-release-public.pem"), &staged).unwrap();
    fs::set_permissions(&staged, PermissionsExt::from_mode(0o600)).unwrap();
    let authority = ReleaseAuthority::from_staged_session(&executable, &install_paths).unwrap();
    let mut runner = RecordingRunner::default();
    let result = apply_setup_from_with_authority(
        frame.as_slice(),
        &executable,
        &install_paths,
        &mut runner,
        CallerIdentity::sudo_root(1000),
        &authority,
    );
    assert!(matches!(result, Err(SetupError::ReleaseSignature)));
    assert!(runner.commands.is_empty());
    assert!(!install_paths.config.exists());
}
