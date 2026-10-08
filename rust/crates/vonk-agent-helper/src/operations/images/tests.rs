#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn image_pull_tags_the_pinned_image_and_reuses_it_later() {
    let temp = tempfile::tempdir().unwrap();
    let (manifest, config) = ("a".repeat(64), "c".repeat(64));
    let runner = PullRunner {
        pulled_config: format!("sha256:{config}"),
        ..PullRunner::default()
    };
    let executor = OperationExecutor::new(
        ManagedRoots::under(temp.path()),
        &[0; 32],
        runner.clone(),
        None,
    )
    .unwrap();

    executor
        .runtime_image_pull(&pull_arguments(&manifest, &config))
        .unwrap();
    let local = format!("localhost/vonk/compiled-runtime-{manifest}");
    let remote = format!("127.0.0.1:41000/vonk/runtime@sha256:{manifest}");
    {
        let images = runner.images.lock().unwrap();
        assert_eq!(images.get(&local), Some(&format!("sha256:{config}")));
        assert!(
            !images.contains_key(&remote),
            "the loopback reference is dropped"
        );
    }
    let pulls = |runner: &PullRunner| {
        runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|call| call.first().map(String::as_str) == Some("pull"))
            .cloned()
            .collect::<Vec<_>>()
    };
    assert_eq!(
        pulls(&runner),
        vec![vec![
            "pull".to_owned(),
            "--quiet".to_owned(),
            "--platform".to_owned(),
            "linux/arm64".to_owned(),
            remote,
        ]]
    );

    executor
        .runtime_image_pull(&pull_arguments(&manifest, &config))
        .unwrap();
    assert_eq!(
        pulls(&runner).len(),
        1,
        "a pulled image is not pulled again"
    );
}

#[test]
fn image_pull_accepts_the_containerd_store_naming_the_image_by_manifest() {
    let temp = tempfile::tempdir().unwrap();
    let (manifest, config) = ("a".repeat(64), "c".repeat(64));
    let runner = PullRunner {
        pulled_config: format!("sha256:{manifest}"),
        ..PullRunner::default()
    };
    let executor =
        OperationExecutor::new(ManagedRoots::under(temp.path()), &[0; 32], runner, None).unwrap();
    executor
        .runtime_image_pull(&pull_arguments(&manifest, &config))
        .unwrap();
}

#[test]
fn image_pull_refuses_another_image_a_failed_pull_and_foreign_registries() {
    let temp = tempfile::tempdir().unwrap();
    let (manifest, config) = ("a".repeat(64), "c".repeat(64));
    let wrong = PullRunner {
        pulled_config: format!("sha256:{}", "d".repeat(64)),
        ..PullRunner::default()
    };
    let executor = OperationExecutor::new(
        ManagedRoots::under(temp.path()),
        &[0; 32],
        wrong.clone(),
        None,
    )
    .unwrap();
    assert!(matches!(
        executor.runtime_image_pull(&pull_arguments(&manifest, &config)),
        Err(OperationError::RuntimeImageIdentityInvalid)
    ));
    assert!(
        !wrong
            .images
            .lock()
            .unwrap()
            .contains_key(&format!("localhost/vonk/compiled-runtime-{manifest}"))
    );

    let failing = PullRunner {
        pull_fails: true,
        ..PullRunner::default()
    };
    let executor =
        OperationExecutor::new(ManagedRoots::under(temp.path()), &[0; 32], failing, None).unwrap();
    assert!(matches!(
        executor.runtime_image_pull(&pull_arguments(&manifest, &config)),
        Err(OperationError::RuntimeImageLoadFailed)
    ));

    for registry in [
        "ghcr.io",
        "10.0.0.2:5000",
        "127.0.0.1:80",
        "127.0.0.1:041000",
    ] {
        let mut arguments = pull_arguments(&manifest, &config);
        arguments[0] = registry.to_owned();
        assert!(matches!(
            executor.runtime_image_pull(&arguments),
            Err(OperationError::InvalidOperation)
        ));
    }
    let mut other_tag = pull_arguments(&manifest, &config);
    other_tag[3] = format!(
        "localhost/vonk/compiled-runtime-{}@sha256:{manifest}",
        "b".repeat(64)
    );
    assert!(matches!(
        executor.runtime_image_pull(&other_tag),
        Err(OperationError::InvalidOperation)
    ));
}

#[test]
fn runtime_consumes_post_image_entrypoint_marker_before_docker() {
    let (_temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    fs::create_dir_all(&model).unwrap();
    let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
    let image = arguments
        .iter()
        .position(|value| value.starts_with("localhost/vonk/"))
        .unwrap();
    arguments.insert(image + 2, "--once".to_owned());

    let validated = validate_docker_run(&arguments, &roots, None).unwrap();
    assert_eq!(
        validated.arguments[validated.image_index + 1],
        validated.entrypoint
    );
    let docker = validated.docker_arguments().unwrap();
    let image = docker
        .iter()
        .position(|value| value.starts_with("localhost/vonk/"))
        .unwrap();
    assert_eq!(&docker[image + 1..], &["--once"]);
}

#[test]
fn damaged_receipts_rebuild_from_digest_qualified_docker_content() {
    // Wrong implementation: a disposable receipt refused a verified live image.
    for damaged in [None, Some(b"broken".to_vec()), Some(vec![b'x'; 4096])] {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(temp.path());
        let manifest = "a".repeat(64);
        let config = "c".repeat(64);
        let reference = format!("localhost/vonk/compiled-runtime-{manifest}@sha256:{manifest}");
        let runner = PullRunner::default();
        runner
            .images
            .lock()
            .unwrap()
            .insert(reference.clone(), format!("sha256:{config}"));
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
        ensure_private_directory(&roots.runtime_image_receipts, None).unwrap();
        if let Some(bytes) = damaged {
            fs::write(roots.runtime_image_receipts.join(&manifest), bytes).unwrap();
        }
        let arguments = vec![
            manifest.clone(),
            format!("sha256:{manifest}"),
            format!("sha256:{manifest}"),
            reference,
            "10001:10001".to_owned(),
            format!("sha256:{config}"),
        ];
        executor.runtime_image_inspect(&arguments).unwrap();
        executor.runtime_image_inspect(&arguments).unwrap();
        let receipt: RuntimeImageReceipt =
            parse_strict(&fs::read(roots.runtime_image_receipts.join(&manifest)).unwrap()).unwrap();
        assert_eq!(receipt.image_config_id, format!("sha256:{config}"));
        assert!(
            !runner
                .calls
                .lock()
                .unwrap()
                .iter()
                .any(|call| call[0] == "pull")
        );
    }
}

#[test]
fn tag_only_content_is_a_miss_then_exact_signed_pull_repairs_it() {
    // Wrong implementation: labels on an arbitrary tag blessed it as the manifest.
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let manifest = "a".repeat(64);
    let config = "c".repeat(64);
    let local = format!("localhost/vonk/compiled-runtime-{manifest}");
    let reference = format!("{local}@sha256:{manifest}");
    let runner = PullRunner {
        pulled_config: format!("sha256:{config}"),
        ..PullRunner::default()
    };
    runner
        .images
        .lock()
        .unwrap()
        .insert(local, format!("sha256:{}", "d".repeat(64)));
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
    let arguments = vec![
        manifest.clone(),
        format!("sha256:{manifest}"),
        format!("sha256:{manifest}"),
        reference,
        "10001:10001".to_owned(),
        format!("sha256:{config}"),
    ];
    assert!(executor.runtime_image_inspect(&arguments).is_err());
    assert!(!roots.runtime_image_receipts.join(&manifest).exists());
    executor
        .runtime_image_pull(&pull_arguments(&manifest, &config))
        .unwrap();
    executor.runtime_image_inspect(&arguments).unwrap();
}

#[test]
fn runtime_image_receipt_binds_manifest_config_and_local_reference() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    fs::create_dir_all(&roots.data).unwrap();
    let address = "a".repeat(64);
    let manifest = format!("sha256:{address}");
    let config = format!("sha256:{}", "c".repeat(64));
    let local_reference = format!("localhost/vonk/compiled-runtime-{address}@{manifest}");
    let runner = PullRunner::default();
    runner
        .images
        .lock()
        .unwrap()
        .insert(local_reference.clone(), config.clone());
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
    executor
        .write_image_receipt(RuntimeImageReceipt {
            schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
            platform_manifest_digest: manifest.clone(),
            image_config_id: config.clone(),
            local_image_reference: local_reference.clone(),
        })
        .unwrap();
    executor.project_image_receipt(RuntimeImageReceipt {
        schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
        platform_manifest_digest: manifest.clone(),
        image_config_id: config.clone(),
        local_image_reference: local_reference.clone(),
    });
    let receipt = executor.read_image_receipt(&address).unwrap();
    assert_eq!(receipt.image_config_id, config);
    assert!(runner.calls.lock().unwrap().is_empty());
}

#[test]
fn receipt_publication_faults_do_not_accumulate_staging_or_refuse_verified_images() {
    // Wrong implementation: swallowed rename failures leak a new staging file
    // on every inspection, even though Docker already holds verified content.
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let manifest = "a".repeat(64);
    let config = "c".repeat(64);
    let reference = format!("localhost/vonk/compiled-runtime-{manifest}@sha256:{manifest}");
    let runner = PullRunner::default();
    runner
        .images
        .lock()
        .unwrap()
        .insert(reference.clone(), format!("sha256:{config}"));
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner, None).unwrap();
    ensure_private_directory(&roots.runtime_image_receipts, None).unwrap();
    let receipt = roots.runtime_image_receipts.join(&manifest);
    fs::create_dir(&receipt).unwrap();
    let arguments = vec![
        manifest.clone(),
        format!("sha256:{manifest}"),
        format!("sha256:{manifest}"),
        reference,
        "10001:10001".into(),
        format!("sha256:{config}"),
    ];
    for _ in 0..4 {
        executor.runtime_image_inspect(&arguments).unwrap();
        let files = fs::read_dir(&roots.runtime_image_receipts)
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].path(), receipt);
    }
    fs::remove_dir(&receipt).unwrap();
    executor.runtime_image_inspect(&arguments).unwrap();
    let repaired: RuntimeImageReceipt = parse_strict(&fs::read(receipt).unwrap()).unwrap();
    assert_eq!(repaired.image_config_id, format!("sha256:{config}"));
}

#[test]
fn lost_docker_names_reuse_accepted_content_without_a_receipt_or_pull() {
    // Wrong implementation: a missing logical tag or damaged receipt vetoes
    // content that Docker still holds under its authority-bound digest.
    let temp = tempfile::tempdir().unwrap();
    let (manifest, config) = ("a".repeat(64), "c".repeat(64));
    let manifest_digest = format!("sha256:{manifest}");
    let config_digest = format!("sha256:{config}");
    let reference = format!("localhost/vonk/compiled-runtime-{manifest}@{manifest_digest}");
    let runner = PullRunner::default();
    runner
        .images
        .lock()
        .unwrap()
        .insert(config_digest.clone(), config_digest.clone());
    let roots = ManagedRoots::under(temp.path());
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
    ensure_private_directory(&roots.runtime_image_receipts, None).unwrap();
    fs::write(roots.runtime_image_receipts.join(&manifest), b"damaged").unwrap();
    for _ in 0..2 {
        let (_, operational) = executor
            .inspect_accepted_runtime_image(&reference, &config_digest, &manifest_digest)
            .unwrap();
        assert_eq!(operational, config_digest);
        executor.project_image_receipt(RuntimeImageReceipt {
            schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
            platform_manifest_digest: manifest_digest.clone(),
            image_config_id: config_digest.clone(),
            local_image_reference: reference.clone(),
        });
    }
    assert!(
        !runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|args| args[0] == "pull")
    );
    let receipt: RuntimeImageReceipt =
        parse_strict(&fs::read(roots.runtime_image_receipts.join(&manifest)).unwrap()).unwrap();
    assert_eq!(receipt.image_config_id, config_digest);
}

#[test]
fn leftover_owned_staging_is_collected_after_clearance_without_touching_unsafe_siblings() {
    // Wrong implementation: a failed staging unlink has no future owner, or
    // collection races a current publisher / removes ambiguous sibling bytes.
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let executor =
        OperationExecutor::new(roots.clone(), &[0; 32], PullRunner::default(), None).unwrap();
    ensure_private_directory(&roots.runtime_image_receipts, None).unwrap();
    let stage = roots
        .runtime_image_receipts
        .join(format!(".receipt-{}.tmp", uuid::Uuid::new_v4()));
    fs::write(&stage, b"incomplete unpublished receipt").unwrap();
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o600)).unwrap();
    let sibling = roots
        .runtime_image_receipts
        .join(format!(".receipt-{}.tmp", uuid::Uuid::new_v4()));
    fs::write(&sibling, b"ambiguous sibling").unwrap();
    fs::set_permissions(&sibling, fs::Permissions::from_mode(0o644)).unwrap();
    let guard = executor.lock_image_publication().unwrap();
    let receipt = RuntimeImageReceipt {
        schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
        platform_manifest_digest: format!("sha256:{}", "a".repeat(64)),
        image_config_id: format!("sha256:{}", "c".repeat(64)),
        local_image_reference: format!(
            "localhost/vonk/compiled-runtime-{}@sha256:{}",
            "a".repeat(64),
            "a".repeat(64)
        ),
    };
    assert!(executor.write_image_receipt(receipt.clone()).is_err());
    assert!(stage.exists());
    drop(guard);
    executor.write_image_receipt(receipt.clone()).unwrap();
    assert!(!stage.exists());
    assert_eq!(fs::read(&sibling).unwrap(), b"ambiguous sibling");
    executor.write_image_receipt(receipt).unwrap();
    assert_eq!(
        fs::read_dir(&roots.runtime_image_receipts).unwrap().count(),
        2
    );
}

#[test]
fn containerd_alias_loss_uses_the_observed_managed_launch_name() {
    // Wrong implementation: containerd's observed manifest ID is passed to
    // Docker as a launch name after the digest-qualified alias disappears.
    let temp = tempfile::tempdir().unwrap();
    let (manifest, config) = ("a".repeat(64), "c".repeat(64));
    let local = format!("localhost/vonk/compiled-runtime-{manifest}");
    let reference = format!("{local}@sha256:{manifest}");
    let runner = PullRunner::default();
    runner
        .images
        .lock()
        .unwrap()
        .insert(local.clone(), format!("sha256:{manifest}"));
    let executor =
        OperationExecutor::new(ManagedRoots::under(temp.path()), &[0; 32], runner, None).unwrap();
    let (observed, launch) = executor
        .inspect_accepted_runtime_image(
            &reference,
            &format!("sha256:{config}"),
            &format!("sha256:{manifest}"),
        )
        .unwrap();
    assert_eq!(observed.0, format!("sha256:{manifest}"));
    assert_eq!(launch, local);
}

#[test]
fn receipt_projection_reuses_verified_observation_after_daemon_loss() {
    // A second daemon query during bookkeeping would veto accepted content.
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let runner = PullRunner::default();
    let address = "a".repeat(64);
    let manifest = format!("sha256:{address}");
    let config = format!("sha256:{}", "c".repeat(64));
    let reference = format!("localhost/vonk/compiled-runtime-{address}@{manifest}");
    runner
        .images
        .lock()
        .unwrap()
        .insert(reference.clone(), config.clone());
    let executor = OperationExecutor::new(roots, &[0; 32], runner.clone(), None).unwrap();
    let (observed, _) = executor
        .inspect_accepted_runtime_image(&reference, &config, &manifest)
        .unwrap();
    let calls = runner.calls.lock().unwrap().len();
    runner.images.lock().unwrap().clear();
    executor.project_image_receipt(RuntimeImageReceipt {
        schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
        platform_manifest_digest: manifest,
        image_config_id: observed.0,
        local_image_reference: reference,
    });
    assert_eq!(runner.calls.lock().unwrap().len(), calls);
    assert_eq!(
        executor
            .read_image_receipt(&address)
            .unwrap()
            .image_config_id,
        config
    );
}
