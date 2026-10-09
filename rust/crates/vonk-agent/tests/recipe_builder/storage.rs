use super::*;

#[test]
fn damaged_generated_cache_entries_repair_without_following_external_targets() {
    for shape in 0..6 {
        let data = tempdir().unwrap();
        let runtime = tempdir().unwrap();
        let external = tempdir().unwrap();
        stage_base_archive(external.path());
        let trusted = fs::read(base_archive_path(external.path())).unwrap();
        let digest = registry_fixture()
            .manifest_digest
            .strip_prefix("sha256:")
            .unwrap()
            .to_owned();
        let path = base_archive_path(data.path());
        match shape {
            0 => {
                symlink(
                    external.path().join("base-images"),
                    data.path().join("base-images"),
                )
                .unwrap();
            }
            1 => {
                fs::create_dir_all(path.parent().unwrap().parent().unwrap()).unwrap();
                symlink(
                    base_archive_path(external.path()).parent().unwrap(),
                    path.parent().unwrap(),
                )
                .unwrap();
            }
            2 => {
                fs::create_dir_all(path.parent().unwrap()).unwrap();
                symlink(base_archive_path(external.path()), &path).unwrap();
            }
            3 => {
                fs::create_dir_all(path.parent().unwrap()).unwrap();
                fs::write(&path, b"truncated").unwrap();
            }
            4 => {
                let locks = path.parent().unwrap().parent().unwrap();
                fs::create_dir_all(locks).unwrap();
                symlink(
                    base_archive_path(external.path()),
                    locks.join(format!("{digest}-lock")),
                )
                .unwrap();
            }
            _ => {
                let locks = path.parent().unwrap().parent().unwrap();
                fs::create_dir_all(locks.join(format!("{digest}-lock"))).unwrap();
            }
        }
        fresh_build(data.path(), runtime.path());
        assert_eq!(fs::read(&path).unwrap(), trusted);
        assert_eq!(
            fs::read(base_archive_path(external.path())).unwrap(),
            trusted
        );
        assert!(path.parent().unwrap().ends_with(digest));
        fresh_build(data.path(), runtime.path());
    }
}

#[test]
fn damaged_local_layer_is_refetched_and_verified_before_import() {
    let root = tempdir().unwrap();
    let runtime = tempdir().unwrap();
    let mut fixture = registry_fixture();
    fixture.layer.push(b'!');
    let path = base_archive_path(root.path());
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(&path, oci_archive(&fixture)).unwrap();
    fresh_build(root.path(), runtime.path());
    assert_eq!(fs::read(path).unwrap(), oci_archive(&registry_fixture()));
    fresh_build(root.path(), runtime.path());
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
