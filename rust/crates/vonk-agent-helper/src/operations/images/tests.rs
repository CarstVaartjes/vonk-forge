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
    let executor =
        OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
    let address = "a".repeat(64);
    let manifest = format!("sha256:{address}");
    let config = format!("sha256:{}", "c".repeat(64));
    let local_reference = format!("localhost/vonk/compiled-runtime-{address}@{manifest}");
    executor
        .write_image_receipt(RuntimeImageReceipt {
            schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
            platform_manifest_digest: manifest.clone(),
            image_config_id: config.clone(),
            local_image_reference: local_reference.clone(),
        })
        .unwrap();
    executor
        .require_image_receipt(&address, &manifest, &manifest, &local_reference, &config)
        .unwrap();
    let other = format!("sha256:{}", "d".repeat(64));
    for (registry, platform, local, image) in [
        (&other, &manifest, &local_reference, &config),
        (&manifest, &other, &local_reference, &config),
        (&manifest, &manifest, &local_reference, &other),
    ] {
        assert!(
            executor
                .require_image_receipt(&address, registry, platform, local, image)
                .is_err()
        );
    }
    // Another address has no receipt at all.
    assert!(
        executor
            .require_image_receipt(&"b".repeat(64), &other, &other, &local_reference, &config)
            .is_err()
    );
}
