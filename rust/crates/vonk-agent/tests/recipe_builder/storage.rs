use super::*;

#[test]
fn base_image_storage_rejects_symlinked_data_and_supply_roots() {
    let (archive, digest) = bundle();
    let runtime = tempdir().unwrap();

    let real_data = tempdir().unwrap();
    stage_base_archive(real_data.path());
    let linked_parent = tempdir().unwrap();
    let linked_data = linked_parent.path().join("agent-data");
    symlink(real_data.path(), &linked_data).unwrap();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let error = RecipeBuilder {
        runner: &runner,
        data_root: &linked_data,
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest.clone()),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000b").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::BaseImageContent));
    assert!(runner.calls.borrow().is_empty());

    let data = tempdir().unwrap();
    let external = tempdir().unwrap();
    stage_base_archive(external.path());
    symlink(
        external.path().join("base-images"),
        data.path().join("base-images"),
    )
    .unwrap();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let error = RecipeBuilder {
        runner: &runner,
        data_root: data.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000c").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::BaseImageContent));
    assert!(runner.calls.borrow().is_empty());
}

#[test]
fn base_image_storage_rejects_symlinked_digest_directory_and_archive() {
    let (archive, digest) = bundle();
    let runtime = tempdir().unwrap();

    let data = tempdir().unwrap();
    let external = tempdir().unwrap();
    stage_base_archive(external.path());
    let digest_name = registry_fixture()
        .manifest_digest
        .strip_prefix("sha256:")
        .unwrap()
        .to_owned();
    fs::create_dir_all(data.path().join("base-images/sha256")).unwrap();
    symlink(
        external
            .path()
            .join("base-images/sha256")
            .join(&digest_name),
        data.path().join("base-images/sha256").join(&digest_name),
    )
    .unwrap();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let error = RecipeBuilder {
        runner: &runner,
        data_root: data.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest.clone()),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000d").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::BaseImageContent));

    let data = tempdir().unwrap();
    let external_archive = data.path().join("external.oci.tar");
    fs::write(&external_archive, oci_archive(&registry_fixture())).unwrap();
    let archive_path = base_archive_path(data.path());
    fs::create_dir_all(archive_path.parent().unwrap()).unwrap();
    symlink(&external_archive, &archive_path).unwrap();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let error = RecipeBuilder {
        runner: &runner,
        data_root: data.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-00000000000e").unwrap(),
        &archive,
    )
    .unwrap_err();
    assert!(matches!(error, RecipeBuildError::BaseImageContent));
}

#[test]
fn base_image_storage_rejects_digest_path_escape_before_registry_or_podman() {
    let (archive, digest) = bundle();
    let mut build_request = request(archive.len(), digest);
    build_request.base_images[0].manifest_digest = "sha256:../../escape".to_owned();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
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
        Uuid::parse_str("00000000-0000-4000-8000-00000000000f").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::BaseImageContent));
    assert!(runner.calls.borrow().is_empty());
}

struct ReplacementRaceRunner {
    archive_path: std::path::PathBuf,
    inner: Runner,
    loaded: RefCell<Vec<u8>>,
}

impl ProcessRunner for ReplacementRaceRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.inner.run(program, arguments, timeout)
    }

    fn run_with_input_disk_reserve_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        input: &File,
        _reserve: ProcessDiskReserve<'_>,
        _cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        if program == Program::Podman && arguments.iter().any(|value| value == "load") {
            let replaced = self.archive_path.with_extension("verified");
            fs::rename(&self.archive_path, &replaced)?;
            fs::write(&self.archive_path, b"substituted after verification")?;
            let mut held = input.try_clone()?;
            held.seek(SeekFrom::Start(0))?;
            held.read_to_end(&mut self.loaded.borrow_mut())?;
        }
        self.inner.run(program, arguments, timeout)
    }
}

#[test]
fn base_image_consumer_holds_verified_descriptor_across_path_replacement() {
    let (archive, digest) = bundle();
    let root = tempdir().unwrap();
    stage_base_archive(root.path());
    let archive_path = base_archive_path(root.path());
    let verified = fs::read(&archive_path).unwrap();
    let runner = ReplacementRaceRunner {
        archive_path: archive_path.clone(),
        inner: Runner {
            calls: RefCell::new(Vec::new()),
            fail_build: false,
            oversize_base: false,
            registry: None,
            substitute_base: false,
        },
        loaded: RefCell::new(Vec::new()),
    };
    let runtime = tempdir().unwrap();

    RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-000000000010").unwrap(),
        &archive,
    )
    .unwrap();

    assert_eq!(*runner.loaded.borrow(), verified);
    assert_eq!(
        fs::read(archive_path).unwrap(),
        b"substituted after verification"
    );
}

#[test]
fn base_image_archive_rejects_a_layer_substituted_under_the_exact_manifest() {
    let (archive, digest) = bundle();
    let root = tempdir().unwrap();
    let mut fixture = registry_fixture();
    fixture.layer.push(b'!');
    let archive_path = base_archive_path(root.path());
    fs::create_dir_all(archive_path.parent().unwrap()).unwrap();
    fs::write(&archive_path, oci_archive(&fixture)).unwrap();
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
        fail_build: false,
        oversize_base: false,
        registry: None,
        substitute_base: false,
    };
    let runtime = tempdir().unwrap();

    let error = RecipeBuilder {
        runner: &runner,
        data_root: root.path(),
        runtime_root: runtime.path(),
        egress_binary: Path::new("/bin/true"),
    }
    .build(
        &request(archive.len(), digest),
        Uuid::parse_str("00000000-0000-4000-8000-000000000011").unwrap(),
        &archive,
    )
    .unwrap_err();

    assert!(matches!(error, RecipeBuildError::BaseImageArchive));
    assert!(
        !runner
            .calls
            .borrow()
            .iter()
            .any(|(_, arguments)| arguments.iter().any(|value| value == "load"))
    );
}
