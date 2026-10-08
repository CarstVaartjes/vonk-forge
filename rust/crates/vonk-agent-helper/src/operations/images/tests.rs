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
fn an_image_the_previous_agent_loaded_keeps_its_installation_startable() {
    // Wrong implementation: after the layered image store landed, the
    // schema-2 receipt of an image loaded from an archive was refused,
    // so an installation the previous agent started could not be
    // restarted or recovered without a reinstall.
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    fs::create_dir_all(&roots.runtime_image_receipts).unwrap();
    let executor =
        OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
    let archive = "a".repeat(64);
    let index = format!("sha256:{}", "b".repeat(64));
    let platform = format!("sha256:{}", "c".repeat(64));
    let config = format!("sha256:{}", "d".repeat(64));
    let local = format!("localhost/vonk/compiled-runtime-{archive}@{platform}");
    fs::write(
        roots.runtime_image_receipts.join(&archive),
        serde_json::to_vec(&serde_json::json!({
            "archive_bytes": 4096,
            "archive_config_id": config,
            "archive_identity": {
                "bytes": 4096, "changed_nanoseconds": 0, "changed_seconds": 1,
                "device": 2, "inode": 3, "modified_nanoseconds": 0,
                "modified_seconds": 1,
            },
            "archive_sha256": archive,
            "image_config_id": config,
            "local_image_reference": local,
            "platform_manifest_digest": platform,
            "registry_index_digest": index,
            "schema_version": 2,
        }))
        .unwrap(),
    )
    .unwrap();

    executor
        .require_image_receipt(&archive, &index, &platform, &local, &config)
        .unwrap();
    let other = format!("sha256:{}", "e".repeat(64));
    assert!(
        executor
            .require_image_receipt(&archive, &index, &platform, &local, &other)
            .is_err()
    );
    assert!(
        executor
            .require_image_receipt(&archive, &other, &platform, &local, &config)
            .is_err()
    );
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

#[test]
fn damaged_image_receipt_reconciles_exact_manifest_then_admits_fresh_request() {
    #[derive(Clone)]
    struct ManifestRunner {
        reply: std::sync::Arc<Mutex<Vec<u8>>>,
        calls: std::sync::Arc<std::sync::atomic::AtomicUsize>,
    }
    impl CommandRunner for ManifestRunner {
        fn run(&self, _: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            assert_eq!(arguments[3], "{{json .RepoDigests}}");
            self.calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            Ok(CommandOutput {
                success: true,
                exit_code: Some(0),
                stdout: self.reply.lock().unwrap().clone(),
                stderr: Vec::new(),
            })
        }
    }
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let address = "a".repeat(64);
    let manifest = format!("sha256:{address}");
    let config = format!("sha256:{}", "c".repeat(64));
    let local = format!("localhost/vonk/compiled-runtime-{address}@{manifest}");
    let runner = ManifestRunner {
        reply: std::sync::Arc::new(Mutex::new(b"[]".to_vec())),
        calls: std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0)),
    };
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
    // An inspected config alone never blesses a missing manifest association.
    assert!(
        executor
            .require_image_receipt(&address, &manifest, &manifest, &local, &config)
            .is_err()
    );
    assert_eq!(runner.calls.load(std::sync::atomic::Ordering::SeqCst), 3);
    assert!(!roots.runtime_image_receipts.join(&address).exists());
    *runner.reply.lock().unwrap() =
        serde_json::to_vec(&vec![format!("registry/runtime@{manifest}")]).unwrap();
    executor
        .require_image_receipt(&address, &manifest, &manifest, &local, &config)
        .unwrap();
    for damaged in [b"broken JSON".as_slice(), b"{}".as_slice()] {
        fs::write(roots.runtime_image_receipts.join(&address), damaged).unwrap();
        executor
            .require_image_receipt(&address, &manifest, &manifest, &local, &config)
            .unwrap();
        let receipt: RuntimeImageReceipt =
            serde_json::from_slice(&fs::read(roots.runtime_image_receipts.join(&address)).unwrap())
                .unwrap();
        assert_eq!(receipt.platform_manifest_digest, manifest);
        assert_eq!(receipt.image_config_id, config);
        let before = runner.calls.load(std::sync::atomic::Ordering::SeqCst);
        executor
            .require_image_receipt(&address, &manifest, &manifest, &local, &config)
            .unwrap();
        assert_eq!(
            runner.calls.load(std::sync::atomic::Ordering::SeqCst),
            before
        );
    }
}

#[test]
fn signed_plan_receipt_repair_has_a_fresh_observation_budget_and_reuses_exact_image() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let plan = compiled_plan_for_runtime_authority();
    let image = &plan.runtime_image;
    let local = format!(
        "localhost/vonk/compiled-runtime-{}",
        image.oci_layout_sha256
    );
    let runner = PullRunner::default();
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
    assert!(executor.repair_signed_image_receipt(&plan).is_err());
    assert!(
        !roots
            .runtime_image_receipts
            .join(&image.oci_layout_sha256)
            .exists()
    );
    assert_eq!(runner.calls.lock().unwrap().len(), 6);
    runner
        .images
        .lock()
        .unwrap()
        .insert(local.clone(), image.local_image_config_id.clone());
    executor.repair_signed_image_receipt(&plan).unwrap();
    let receipt_path = roots.runtime_image_receipts.join(&image.oci_layout_sha256);
    fs::write(&receipt_path, b"damaged receipt").unwrap();
    executor.repair_signed_image_receipt(&plan).unwrap();
    let receipt: RuntimeImageReceipt =
        serde_json::from_slice(&fs::read(&receipt_path).unwrap()).unwrap();
    assert_eq!(receipt.platform_manifest_digest, image.image_digest);
    assert_eq!(receipt.image_config_id, image.local_image_config_id);
    executor
        .require_image_receipt(
            &image.oci_layout_sha256,
            &image.image_digest,
            &image.image_digest,
            &receipt.local_image_reference,
            &image.local_image_config_id,
        )
        .unwrap();
    executor.repair_signed_image_receipt(&plan).unwrap();
    assert_eq!(
        runner.images.lock().unwrap().get(&local),
        Some(&image.local_image_config_id)
    );
    assert!(
        runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .all(|call| call[0] == "image")
    );
}
