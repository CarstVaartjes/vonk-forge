use super::*;
use std::io::Cursor;

fn payload() -> CanonicalTemplatePayload {
    parse_template_payload(br#"{
        "schema_version":2,"docker_compose_yaml":"services: {}\n",
        "internal_values":[],"required_values":[],"secrets":[],
        "generated_secrets":{"random_text":[{"file":"secret-alpha","bytes":32},{"file":"secret-beta","bytes":32}],"ed25519_pkcs8_pem":[],"postgres_urls":[]}
    }"#).unwrap()
}

fn upgrade(root: &Path) {
    let mut output = Vec::new();
    prepare(
        &payload(),
        SetupRequest::upgrade(root),
        &mut PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output),
        &OsSecretGenerator,
    )
    .unwrap();
}

#[test]
#[ignore = "subprocess fault boundary, invoked by recovery test"]
fn interrupted_publisher() {
    let bundle = PathBuf::from(std::env::var_os("VONK_NAS_PUBLICATION_TEST_BUNDLE").unwrap());
    let _owner = acquire_owner(bundle.parent().unwrap()).unwrap();
    let document = fs::read(bundle.join(VERIFIED).join(".template.json")).unwrap();
    let payload = parse_template_payload(&document).unwrap();
    let stop = std::env::var("VONK_NAS_PUBLICATION_TEST_STOP").unwrap();
    replay_complete(&payload, &bundle, |relative| {
        if relative == stop {
            std::process::exit(73);
        }
    })
    .unwrap();
}

#[test]
fn process_death_during_group_publication_replays_exact_candidate_and_releases_owner() {
    let temporary = tempfile::tempdir().unwrap();
    let bundle = temporary.path().join("vonk-forge");
    create_secure_directory(&bundle).unwrap();
    create_secure_directory(&bundle.join("secrets")).unwrap();
    write_secret_file(&bundle.join("secrets"), "secret-alpha", b"original-first").unwrap();
    write_secret_file(&bundle.join("secrets"), "secret-beta", b"original-second").unwrap();
    write_new_file(&bundle.join(".env"), b"", 0o600).unwrap();
    remember_verified(&payload(), &bundle).unwrap();
    let intent = bundle.join(INTENT);
    create_secure_directory(&intent).unwrap();
    create_secure_directory(&intent.join("secrets")).unwrap();
    write_secret_file(&intent.join("secrets"), "secret-alpha", b"candidate-first").unwrap();
    write_secret_file(&intent.join("secrets"), "secret-beta", b"candidate-second").unwrap();
    write_new_file(&intent.join(".env"), b"", 0o600).unwrap();
    sync_tree(&payload(), &intent.join("secrets")).unwrap();
    sync_directory(&intent).unwrap();
    let mut child = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "publication::tests::interrupted_publisher",
            "--ignored",
        ])
        .env("VONK_NAS_PUBLICATION_TEST_BUNDLE", &bundle)
        .env("VONK_NAS_PUBLICATION_TEST_STOP", "secret-alpha")
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            child.kill().unwrap();
            panic!("publication child exceeded fault-injection deadline");
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    };
    assert_eq!(status.code(), Some(73));
    assert_eq!(
        fs::read(bundle.join("secrets/secret-alpha")).unwrap(),
        b"candidate-first"
    );
    assert_eq!(
        fs::read(bundle.join("secrets/secret-beta")).unwrap(),
        b"original-second"
    );
    upgrade(temporary.path());
    assert_eq!(
        fs::read(bundle.join("secrets/secret-beta")).unwrap(),
        b"candidate-second"
    );
    assert!(!intent.exists());
    upgrade(temporary.path());
    assert_eq!(
        fs::read(bundle.join("secrets/secret-alpha")).unwrap(),
        b"candidate-first"
    );
}

#[test]
fn busy_owner_ends_boundedly_and_next_request_acquires_it() {
    let temporary = tempfile::tempdir().unwrap();
    let owner = acquire_owner(temporary.path()).unwrap();
    let started = std::time::Instant::now();
    assert!(acquire_owner(temporary.path()).is_err());
    assert!(started.elapsed() < std::time::Duration::from_secs(1));
    drop(owner);
    let next = acquire_owner(temporary.path()).unwrap();
    drop(next);
    acquire_owner(temporary.path()).unwrap();
}

#[test]
fn death_between_certificate_and_key_replays_verified_pair_before_validation() {
    let payload = parse_template_payload(br#"{
      "schema_version":2,"docker_compose_yaml":"services: {}\n",
      "internal_values":[],"required_values":[],"secrets":[],
      "step_ca_controller":{
        "hostname_env":"VONK_CONTROL_HOSTNAME","provisioner_name":"vonk-forge-agent","password_bytes":32,
        "files":{
          "root_certificate":"step-ca/root-certificate","intermediate_certificate":"step-ca/intermediate-certificate",
          "intermediate_private_key":"step-ca/intermediate-key","controller_server_certificate":"controller-server-certificate",
          "controller_server_private_key":"controller-server-key","provisioner_private_jwk":"agent-ca-credential",
          "provisioner_public_jwk":"agent-ca-provisioner-public-jwk","ca_config":"step-ca/ca.json","password":"step-ca-password"
        }
      }
    }"#).unwrap();
    let temporary = tempfile::tempdir().unwrap();
    let bundle = temporary.path().join("vonk-forge");
    create_secure_directory(&bundle).unwrap();
    let root = bundle.join("secrets");
    create_secure_directory(&root).unwrap();
    let environment = vec![(
        "VONK_CONTROL_HOSTNAME".to_owned(),
        "control.example.test".to_owned(),
    )];
    let request = payload.step_ca_controller.as_ref().unwrap();
    let files = generate_pki(request, &environment, &OsSecretGenerator).unwrap();
    for (path, value) in &files {
        write_secret_file(&root, path, value.as_bytes()).unwrap();
    }
    write_new_file(
        &bundle.join(".env"),
        render_owned_environment(&environment).unwrap().as_bytes(),
        0o600,
    )
    .unwrap();
    remember_verified(&payload, &bundle).unwrap();
    let old_key = fs::read(root.join(&request.files.controller_server_private_key)).unwrap();
    let replacement = upgrade::renew_controller_leaf(request, &environment, &files).unwrap();
    let expected_certificate = secret_file_content(replacement.certificate);
    let expected_key = secret_file_content(replacement.private_key);
    let intent = bundle.join(INTENT);
    create_secure_directory(&intent).unwrap();
    copy_candidate(&payload, &bundle, &intent.join("secrets")).unwrap();
    atomic_replace(
        &intent.join("secrets").join(&replacement.certificate_path),
        &expected_certificate,
        0o600,
    )
    .unwrap();
    atomic_replace(
        &intent.join("secrets").join(&replacement.private_key_path),
        &expected_key,
        0o600,
    )
    .unwrap();
    write_new_file(
        &intent.join(".env"),
        render_owned_environment(&environment).unwrap().as_bytes(),
        0o600,
    )
    .unwrap();
    sync_tree(&payload, &intent.join("secrets")).unwrap();
    sync_directory(&intent).unwrap();
    let mut child = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "publication::tests::interrupted_publisher",
            "--ignored",
        ])
        .env("VONK_NAS_PUBLICATION_TEST_BUNDLE", &bundle)
        .env(
            "VONK_NAS_PUBLICATION_TEST_STOP",
            &replacement.certificate_path,
        )
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            child.kill().unwrap();
            panic!("publisher exceeded fault deadline");
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    };
    assert_eq!(status.code(), Some(73));
    assert_eq!(
        fs::read(root.join(&replacement.certificate_path)).unwrap(),
        expected_certificate
    );
    assert_eq!(
        fs::read(root.join(&replacement.private_key_path)).unwrap(),
        old_key
    );
    for _ in 0..2 {
        let mut output = Vec::new();
        prepare(
            &payload,
            SetupRequest::upgrade(temporary.path()),
            &mut PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output),
            &OsSecretGenerator,
        )
        .unwrap();
        assert_eq!(
            fs::read(root.join(&replacement.certificate_path)).unwrap(),
            expected_certificate
        );
        assert_eq!(
            fs::read(root.join(&replacement.private_key_path)).unwrap(),
            expected_key
        );
        let repaired = step_ca_files(&request.files)
            .into_iter()
            .map(|path| (path.to_owned(), read_member(&root, path).unwrap()))
            .collect::<Vec<_>>();
        validate_pki_material(request, &environment, &repaired).unwrap();
        assert_eq!(
            fs::read(root.join(&request.files.root_certificate)).unwrap(),
            files
                .iter()
                .find(|(path, _)| path == &request.files.root_certificate)
                .unwrap()
                .1
                .as_bytes()
        );
    }
}
