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
        assert_verified_base_archive(&path);
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
    assert_verified_base_archive(&path);
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

// Tar ordering and index annotations are packaging, not content identity.
fn assert_verified_base_archive(path: &Path) {
    let fixture = registry_fixture();
    let mut archive = tar::Archive::new(File::open(path).unwrap());
    let mut blobs = BTreeMap::new();
    for entry in archive.entries().unwrap() {
        let mut entry = entry.unwrap();
        let name = entry.path().unwrap().into_owned();
        if let Some(digest) = name.to_str().unwrap().strip_prefix("blobs/sha256/") {
            let mut bytes = Vec::new();
            entry.read_to_end(&mut bytes).unwrap();
            assert_eq!(hex_sha256(&bytes), digest);
            blobs.insert(format!("sha256:{digest}"), bytes);
        }
    }
    for (digest, content) in [
        (fixture.manifest_digest, fixture.manifest),
        (fixture.config_digest, fixture.config),
        (fixture.layer_digest, fixture.layer),
    ] {
        assert_eq!(blobs.get(&digest), Some(&content));
    }
}

struct DatabaseRunner {
    inner: Runner,
    fail_first_build: Cell<bool>,
}

impl ProcessRunner for DatabaseRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        if program == Program::Podman || arguments.iter().any(|a| a == "/usr/bin/podman") {
            let path = |flag: &str| {
                arguments
                    .windows(2)
                    .find(|p| p[0] == flag)
                    .map(|p| PathBuf::from(&p[1]))
            };
            if let (Some(storage), Some(runroot)) = (path("--root"), path("--runroot")) {
                let db = storage.join("fixture-db-runroot");
                if db.exists() && fs::read(&db)? != runroot.as_os_str().as_encoded_bytes() {
                    return Ok(ProcessOutput { exit_code: Some(125), success: false, stdout: vec![], stderr: b"database run root does not match our run root: database configuration mismatch".to_vec() });
                }
                fs::write(db, runroot.as_os_str().as_encoded_bytes())?;
            }
        }
        if program == Program::SystemdRun
            && arguments.iter().any(|arg| arg == "build")
            && self.fail_first_build.replace(false)
        {
            return Ok(ProcessOutput {
                exit_code: Some(1),
                success: false,
                stdout: vec![],
                stderr: b"injected interrupted build".to_vec(),
            });
        }
        self.inner.run(program, arguments, timeout)
    }
}

#[test]
fn retry_reuses_database_paths_and_repairs_incompatible_private_storage() {
    let (archive, digest) = bundle();
    let request = request(archive.len(), digest);
    let operation = Uuid::new_v4();
    for incompatible in [false, true] {
        let root = tempdir().unwrap();
        let runtime = tempdir().unwrap();
        stage_base_archive(root.path());
        let retained = fs::read(base_archive_path(root.path())).unwrap();
        let runner = DatabaseRunner {
            inner: Runner {
                calls: RefCell::new(Vec::new()),
                fail_build: false,
                oversize_base: false,
                registry: None,
                substitute_base: false,
            },
            fail_first_build: Cell::new(true),
        };
        let builder = RecipeBuilder {
            runner: &runner,
            data_root: root.path(),
            runtime_root: runtime.path(),
            egress_binary: Path::new("/bin/true"),
        };
        assert!(builder.build(&request, operation, &archive).is_err());
        if incompatible {
            let storage = fs::read_dir(root.path().join("build-staging"))
                .unwrap()
                .next()
                .unwrap()
                .unwrap()
                .path()
                .join("podman-storage");
            fs::write(storage.join("fixture-db-runroot"), b"").unwrap();
        }
        builder.build(&request, operation, &archive).unwrap();
        assert_eq!(fs::read(base_archive_path(root.path())).unwrap(), retained);
    }
}

struct OpaqueFailureRunner {
    inner: Runner,
    command: &'static str,
}

impl ProcessRunner for OpaqueFailureRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        let matches = match self.command {
            "load" | "tag" | "push" => arguments.iter().any(|a| a == self.command),
            "recipe-inspect" => {
                arguments.iter().any(|a| a == "inspect")
                    && arguments
                        .last()
                        .is_some_and(|a| a.starts_with(RECIPE_BUILD_TAG_PREFIX))
            }
            "adapter-inspect" => {
                arguments.iter().any(|a| a == "inspect")
                    && arguments
                        .last()
                        .is_some_and(|a| a.starts_with(ADAPTED_BUILD_TAG_PREFIX))
            }
            "base-inspect" => {
                program == Program::Podman
                    && arguments.iter().any(|a| a == "inspect")
                    && arguments.last().is_some_and(|a| a.contains('@'))
            }
            "recipe-build" => {
                arguments
                    .iter()
                    .any(|a| a.starts_with("--unit=vonk-recipe-build-"))
                    && arguments.iter().any(|a| a == "build")
            }
            "adapter-build" => {
                arguments
                    .iter()
                    .any(|a| a.starts_with("--unit=vonk-runtime-adapter-"))
                    && arguments.iter().any(|a| a == "build")
            }
            _ => unreachable!(),
        };
        if matches {
            return Ok(ProcessOutput {
                exit_code: Some(125),
                success: false,
                stdout: vec![],
                stderr: b"opaque command failure".to_vec(),
            });
        }
        self.inner.run(program, arguments, timeout)
    }
}

#[test]
fn every_failed_podman_build_command_keeps_opaque_stderr_and_exit_status() {
    use vonk_agent::recipe_builder::RecipeBuildError;
    for command in [
        "load",
        "tag",
        "base-inspect",
        "recipe-inspect",
        "adapter-inspect",
        "recipe-build",
        "adapter-build",
        "push",
    ] {
        let (archive, digest) = bundle();
        let root = tempdir().unwrap();
        let runtime = tempdir().unwrap();
        stage_base_archive(root.path());
        let runner = OpaqueFailureRunner {
            command,
            inner: Runner {
                calls: RefCell::new(Vec::new()),
                fail_build: false,
                oversize_base: false,
                registry: None,
                substitute_base: false,
            },
        };
        let error = RecipeBuilder {
            runner: &runner,
            data_root: root.path(),
            runtime_root: runtime.path(),
            egress_binary: Path::new("/bin/true"),
        }
        .build(&request(archive.len(), digest), Uuid::new_v4(), &archive)
        .unwrap_err();
        let logs = match error {
            RecipeBuildError::BaseImageImport { logs, .. }
            | RecipeBuildError::ImageBuild { logs, .. }
            | RecipeBuildError::AdapterBuild { logs, .. }
            | RecipeBuildError::Command { logs, .. } => logs,
            other => panic!("{command} discarded its command evidence: {other:?}"),
        };
        assert_eq!(logs.exit_code, Some(125), "{command}");
        assert_eq!(logs.stderr.text, "opaque command failure", "{command}");
    }
}

#[test]
fn cleanup_repairs_a_private_database_conflict_after_build_services_settle() {
    use vonk_agent::recipe_builder::cleanup_build_storage;
    use vonk_agent_protocol::RecipeBuildCleanupRequest;
    for incompatible in [false, true] {
        let (archive, digest) = bundle();
        let root = tempdir().unwrap();
        let runtime = tempdir().unwrap();
        stage_base_archive(root.path());
        let operation = Uuid::new_v4();
        let runner = DatabaseRunner {
            inner: Runner {
                calls: RefCell::new(Vec::new()),
                fail_build: false,
                oversize_base: false,
                registry: None,
                substitute_base: false,
            },
            fail_first_build: Cell::new(true),
        };
        let builder = RecipeBuilder {
            runner: &runner,
            data_root: root.path(),
            runtime_root: runtime.path(),
            egress_binary: Path::new("/bin/true"),
        };
        assert!(
            builder
                .build(&request(archive.len(), digest), operation, &archive)
                .is_err()
        );
        let staging = fs::read_dir(root.path().join("build-staging"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        if incompatible {
            fs::write(staging.join("podman-storage/fixture-db-runroot"), b"").unwrap();
        }
        cleanup_build_storage(
            &runner,
            root.path(),
            runtime.path(),
            &RecipeBuildCleanupRequest {
                build_id: operation,
                operation_id: operation,
            },
        )
        .unwrap();
        assert!(!staging.exists());
        assert_eq!(fs::read_dir(runtime.path()).unwrap().count(), 0);
        assert_verified_base_archive(&base_archive_path(root.path()));
    }
}
