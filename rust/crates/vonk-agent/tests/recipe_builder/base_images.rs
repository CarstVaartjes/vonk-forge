use super::*;

#[test]
fn fresh_node_produces_verified_exact_digest_oci_archive_before_offline_build() {
    let mut fixture = registry_fixture();
    fixture.reference = format!("1.1.1.1/vonkforge/base@{}", fixture.manifest_digest);
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_images = vec![RecipeBuildBaseImage {
        manifest_digest: fixture.manifest_digest.clone(),
        reference: fixture.reference.clone(),
    }];
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: Some(fixture.clone()),
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000000a").unwrap(),
        &archive,
    )
    .unwrap();

    let stored = root
        .path()
        .join("base-images/sha256")
        .join(fixture.manifest_digest.strip_prefix("sha256:").unwrap())
        .join("image.oci.tar");
    let mut entries = BTreeMap::new();
    for entry in tar::Archive::new(fs::File::open(stored).unwrap())
        .entries()
        .unwrap()
    {
        let mut entry = entry.unwrap();
        let path = entry.path().unwrap().into_owned();
        let mut payload = Vec::new();
        entry.read_to_end(&mut payload).unwrap();
        entries.insert(path, payload);
    }
    assert_eq!(
        entries[Path::new("oci-layout")],
        br#"{"imageLayoutVersion":"1.0.0"}"#
    );
    assert_eq!(
        entries[Path::new(&format!(
            "blobs/sha256/{}",
            fixture.manifest_digest.strip_prefix("sha256:").unwrap()
        ))],
        fixture.manifest
    );
    assert_eq!(
        entries[Path::new(&format!(
            "blobs/sha256/{}",
            fixture.config_digest.strip_prefix("sha256:").unwrap()
        ))],
        fixture.config
    );
    assert_eq!(
        entries[Path::new(&format!(
            "blobs/sha256/{}",
            fixture.layer_digest.strip_prefix("sha256:").unwrap()
        ))],
        fixture.layer
    );
    let calls = runner.calls.borrow();
    let oras = calls
        .iter()
        .filter(|(program, _)| *program == Program::Oras)
        .collect::<Vec<_>>();
    assert_eq!(oras.len(), 3);
    let exact_remote = format!("1.1.1.1/vonkforge/base@{}", fixture.manifest_digest);
    assert!(oras[0].1.iter().any(|value| value == &exact_remote));
    assert!(oras.iter().all(|(_, arguments)| {
        arguments.iter().any(|value| value == "--resolve")
            && arguments.iter().all(|value| value != "--no-tty")
            && arguments.iter().all(|value| !value.contains("ignored-tag"))
    }));
    let load = calls
        .iter()
        .position(|(_, arguments)| arguments.iter().any(|value| value == "load"))
        .unwrap();
    let build = calls
        .iter()
        .position(|(_, arguments)| arguments.iter().any(|value| value == "build"))
        .unwrap();
    assert!(load < build);
    // The offline build still ends in the reviewed adaptation stage, and the
    // verified artifact is the adapted image.
    let operation = Uuid::parse_str("00000000-0000-4000-8000-00000000000a").unwrap();
    let adapter_build = calls
        .iter()
        .position(|(_, arguments)| {
            arguments
                .iter()
                .any(|value| value == &format!("--unit=vonk-runtime-adapter-{operation}"))
        })
        .unwrap();
    assert!(build < adapter_build);
    assert!(
        calls[adapter_build]
            .1
            .iter()
            .any(|value| value == "--build-arg")
            && calls[adapter_build].1.iter().any(|value| value
                == &format!(
                    "VONK_RECIPE_IMAGE={}",
                    recipe_build_tag(build_request.build_id)
                )),
        "the adaptation stage must build from the recipe image"
    );
    assert!(
        calls.iter().any(|(_, arguments)| arguments
            .iter()
            .any(|value| value == &adapted_build_tag(build_request.build_id))),
        "the exported image must be the adapted one"
    );
    assert!(calls.iter().all(|(_, arguments)| {
        !arguments.iter().any(|value| value == "pull")
            && !arguments.iter().any(|value| value == "--pull")
    }));
}

#[test]
fn failed_base_image_import_uses_accounted_tmpdir_and_safe_diagnostics() {
    let cases = [
        (
            b"write /private/secret/cache: no space left on device".as_slice(),
            "temporary-storage-exhausted",
        ),
        (
            b"potentially insufficient UIDs or GIDs; inspect /etc/subuid".as_slice(),
            "subordinate-id-mapping-unavailable",
        ),
        (
            b"payload does not match any of the supported image formats: oci-archive".as_slice(),
            "archive-format-rejected",
        ),
        (
            b"open /private/secret/archive: permission denied".as_slice(),
            "permission-denied",
        ),
        (
            b"opaque failure containing /private/secret".as_slice(),
            "unclassified-podman-load-failure",
        ),
    ];
    for (stderr, diagnostic) in cases {
        let (archive, digest) = bundle();
        let request = request(archive.len(), digest);
        let root = tempdir().unwrap();
        stage_base_archive(root.path());
        let runtime = tempdir().unwrap();
        let runner = FailedImportRunner {
            inner: Runner {
                calls: RefCell::new(Vec::new()),
                fail_build: false,
                oversize_base: false,
                registry: None,
                substitute_base: false,
            },
            stderr: stderr.to_vec(),
            temporary_directory: RefCell::new(None),
            monitored_directory: RefCell::new(None),
            minimum_free_bytes: Cell::new(0),
        };

        let error = RecipeBuilder {
            runner: &runner,
            data_root: root.path(),
            runtime_root: runtime.path(),
            egress_binary: Path::new("/bin/true"),
        }
        .build(
            &request,
            Uuid::parse_str("00000000-0000-4000-8000-00000000003a").unwrap(),
            &archive,
        )
        .unwrap_err();

        assert_eq!(
            error.to_string(),
            format!("Podman could not import the verified base image ({diagnostic})")
        );
        assert!(!error.to_string().contains("private"));
        assert!(!error.to_string().contains("secret"));
        let temporary_directory = runner.temporary_directory.borrow();
        let temporary_directory = temporary_directory.as_ref().unwrap();
        let monitored_directory = runner.monitored_directory.borrow();
        let monitored_directory = monitored_directory.as_ref().unwrap();
        assert!(temporary_directory.starts_with(root.path().join("build-staging")));
        assert!(monitored_directory.starts_with(root.path().join("build-staging")));
        // Disposable filesystems may scale the reserve below 4 GiB. The
        // import must still monitor a positive, bounded reserve on its own
        // temporary filesystem.
        assert!((1..=64 * 1024 * 1024 * 1024).contains(&runner.minimum_free_bytes.get()));
    }
}

#[test]
fn fresh_node_retries_a_failed_manifest_transfer() {
    let fixture = registry_fixture();
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_images = vec![RecipeBuildBaseImage {
        manifest_digest: fixture.manifest_digest.clone(),
        reference: fixture.reference.clone(),
    }];
    let runner = RetryManifestRunner {
        inner: Runner {
            calls: RefCell::new(Vec::new()),
            fail_build: false,
            oversize_base: false,
            registry: Some(fixture),
            substitute_base: false,
        },
        remaining_failures: Cell::new(1),
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000002c").unwrap(),
        &archive,
    )
    .unwrap();

    assert_eq!(runner.remaining_failures.get(), 0);
}

#[test]
fn fresh_node_reports_manifest_stage_after_bounded_retries() {
    let fixture = registry_fixture();
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_images = vec![RecipeBuildBaseImage {
        manifest_digest: fixture.manifest_digest.clone(),
        reference: fixture.reference.clone(),
    }];
    let runner = RetryManifestRunner {
        inner: Runner {
            calls: RefCell::new(Vec::new()),
            fail_build: false,
            oversize_base: false,
            registry: Some(fixture),
            substitute_base: false,
        },
        remaining_failures: Cell::new(3),
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000002d").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::BaseImageManifest));
    assert_eq!(runner.remaining_failures.get(), 0);
}

#[test]
fn repeated_identical_base_image_layer_is_materialized_once() {
    let fixture = duplicate_layer_registry_fixture();
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_images = vec![RecipeBuildBaseImage {
        manifest_digest: fixture.manifest_digest.clone(),
        reference: fixture.reference.clone(),
    }];
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: Some(fixture.clone()),
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000002a").unwrap(),
        &archive,
    )
    .unwrap();

    let stored = root
        .path()
        .join("base-images/sha256")
        .join(fixture.manifest_digest.strip_prefix("sha256:").unwrap())
        .join("image.oci.tar");
    let layer_path = format!(
        "blobs/sha256/{}",
        fixture.layer_digest.strip_prefix("sha256:").unwrap()
    );
    let layer_entries = tar::Archive::new(fs::File::open(stored).unwrap())
        .entries()
        .unwrap()
        .map(|entry| entry.unwrap().path().unwrap().into_owned())
        .filter(|path| path == Path::new(&layer_path))
        .count();
    assert_eq!(layer_entries, 1);
    let oras = runner
        .calls
        .borrow()
        .iter()
        .filter(|(program, _)| *program == Program::Oras)
        .count();
    assert_eq!(
        oras, 3,
        "manifest, config, and repeated layer are each fetched once"
    );
}

#[test]
fn conflicting_repeated_base_image_layer_is_rejected_before_blob_fetch() {
    let fixture = conflicting_duplicate_layer_registry_fixture();
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_images = vec![RecipeBuildBaseImage {
        manifest_digest: fixture.manifest_digest.clone(),
        reference: fixture.reference.clone(),
    }];
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: Some(fixture),
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000002b").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::BaseImageManifest));
    let oras = runner
        .calls
        .borrow()
        .iter()
        .filter(|(program, _)| *program == Program::Oras)
        .count();
    assert_eq!(oras, 1, "only the conflicting manifest may be fetched");
}

#[test]
fn base_image_producer_rejects_declared_archive_above_bound_before_blob_fetch() {
    let fixture = registry_fixture();
    let (archive, digest) = bundle_for(&fixture.reference);
    let mut build_request = request(archive.len(), digest);
    build_request.base_image_storage_bytes = oci_archive(&fixture).len() as u64 - 1;
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: Some(fixture),
        substitute_base: false,
    };
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &build_request,
        Uuid::parse_str("00000000-0000-4000-8000-00000000001a").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::OutputLimit));
    let calls = runner.calls.borrow();
    let oras = calls
        .iter()
        .filter(|(program, _)| *program == Program::Oras)
        .collect::<Vec<_>>();
    assert_eq!(oras.len(), 1, "only the bounded manifest may be fetched");
    assert!(oras[0].1.iter().any(|value| value == "manifest"));
    assert!(!base_archive_path(root.path()).exists());
}
