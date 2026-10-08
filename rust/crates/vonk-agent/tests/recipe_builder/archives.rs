use super::*;

#[test]
fn faithful_oci_layout_imports_into_a_real_private_content_store() {
    if !local_oci_integration_fixture_available() {
        eprintln!("skipping local OCI integration fixture: containerd/ctr/docker unavailable");
        return;
    }
    let mut fixture = registry_fixture();
    let fixture_id = format!("{}-good", std::process::id());
    fixture.reference = format!(
        "localhost/vonk/round5-{fixture_id}:fixture@{}",
        fixture.manifest_digest
    );
    let image_name = fixture.reference.rsplit_once('@').unwrap().0.to_owned();
    let container_name = format!("vonk-round5-{fixture_id}");
    let _cleanup = DockerCleanup {
        container: container_name.clone(),
        image: image_name.clone(),
    };
    let archive_root = tempdir().unwrap();
    let archive = archive_root.path().join("image.oci.tar");
    fs::write(&archive, oci_archive(&fixture)).unwrap();
    let store = PrivateContainerd::start();

    let imported = store.ctr(&["images", "import", "--no-unpack", archive.to_str().unwrap()]);

    assert!(
        imported.status.success(),
        "real OCI import failed: {}",
        String::from_utf8_lossy(&imported.stderr)
    );
    let images = store.ctr(&["images", "list", "--quiet"]);
    assert!(images.status.success());
    assert!(
        String::from_utf8_lossy(&images.stdout)
            .lines()
            .any(|image| image == fixture.reference.rsplit_once('@').unwrap().0)
    );
    for (digest, expected) in [
        (&fixture.manifest_digest, &fixture.manifest),
        (&fixture.config_digest, &fixture.config),
        (&fixture.layer_digest, &fixture.layer),
    ] {
        let stored = store.ctr(&["content", "get", digest]);
        assert!(stored.status.success(), "missing imported content {digest}");
        assert_eq!(&stored.stdout, expected);
    }
    let config: serde_json::Value = serde_json::from_slice(&fixture.config).unwrap();
    assert_eq!(
        config["rootfs"]["diff_ids"],
        serde_json::json!([fixture.layer_digest])
    );

    let docker_input = archive_root.path().join("image.docker.tar");
    fs::write(&docker_input, docker_archive(&fixture, &image_name)).unwrap();
    let loaded = docker(&["image", "load", "--input", docker_input.to_str().unwrap()]);
    assert!(
        loaded.status.success(),
        "real Docker graph load failed: {}",
        String::from_utf8_lossy(&loaded.stderr)
    );
    let inspected = docker(&[
        "image",
        "inspect",
        "--format",
        "{{json .RootFS.Layers}}",
        &image_name,
    ]);
    assert!(inspected.status.success());
    let diff_ids: serde_json::Value = serde_json::from_slice(&inspected.stdout).unwrap();
    assert_eq!(diff_ids, serde_json::json!([fixture.layer_digest]));
    let created = docker(&[
        "container",
        "create",
        "--platform",
        "linux/arm64",
        "--name",
        &container_name,
        &image_name,
        "/bin/true",
    ]);
    assert!(
        created.status.success(),
        "real Docker container creation failed: {}",
        String::from_utf8_lossy(&created.stderr)
    );
    let rootfs = archive_root.path().join("rootfs.tar");
    let exported = docker(&[
        "container",
        "export",
        "--output",
        rootfs.to_str().unwrap(),
        &container_name,
    ]);
    assert!(exported.status.success());
    let layer_file = tar::Archive::new(File::open(rootfs).unwrap())
        .entries()
        .unwrap()
        .find_map(|entry| {
            let mut entry = entry.unwrap();
            (entry.path().unwrap() == Path::new("fixture.txt")).then(|| {
                let mut content = Vec::new();
                entry.read_to_end(&mut content).unwrap();
                content
            })
        })
        .expect("the real loaded rootfs must contain its declared layer file");
    assert_eq!(layer_file, b"faithful OCI layer\n");
}

#[test]
fn faithful_untagged_oci_layout_loads_with_spark_podman() {
    if !Path::new("/usr/bin/podman").is_file() {
        eprintln!("skipping local Podman OCI integration fixture: podman unavailable");
        return;
    }
    let mut fixture = registry_fixture();
    let fixture_id = format!("{}-podman", std::process::id());
    fixture.reference = format!(
        "localhost/vonk/round5-{fixture_id}@{}",
        fixture.manifest_digest
    );
    let archive_root = tempdir().unwrap();
    let archive = archive_root.path().join("image.oci.tar");
    fs::write(&archive, oci_archive(&fixture)).unwrap();
    let podman_arguments = |storage: &Path, runroot: &Path| {
        vec![
            "--cgroup-manager=systemd".to_owned(),
            "--root".to_owned(),
            storage.display().to_string(),
            "--runroot".to_owned(),
            runroot.display().to_string(),
            "--storage-opt".to_owned(),
            "overlay.ignore_chown_errors=true".to_owned(),
            "--storage-opt".to_owned(),
            "overlay.mount_program=/usr/bin/fuse-overlayfs".to_owned(),
            "--storage-opt".to_owned(),
            "overlay.force_mask=shared".to_owned(),
        ]
    };

    let storage = archive_root.path().join("storage");
    let runroot = archive_root.path().join("run");
    let image_tmp = archive_root.path().join("image-tmp");
    let home = archive_root.path().join("home");
    let user_runtime = archive_root.path().join("user-runtime");
    let storage_config = archive_root.path().join("containers-storage.conf");
    fs::create_dir_all(&storage).unwrap();
    fs::create_dir_all(&runroot).unwrap();
    fs::create_dir_all(&image_tmp).unwrap();
    fs::create_dir_all(&home).unwrap();
    fs::create_dir_all(&user_runtime).unwrap();
    fs::write(
        &storage_config,
        format!(
            "[storage]\ndriver = \"overlay\"\nrunroot = \"{}\"\ngraphroot = \"{}\"\n\n[storage.options.overlay]\nmount_program = \"/usr/bin/fuse-overlayfs\"\n",
            runroot.display(),
            storage.display()
        ),
    )
    .unwrap();
    let configure_podman = |command: &mut Command| {
        command
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", &home)
            .env("XDG_DATA_HOME", &home)
            .env("XDG_RUNTIME_DIR", &user_runtime)
            .env(
                "DBUS_SESSION_BUS_ADDRESS",
                format!("unix:path={}", user_runtime.join("bus").display()),
            )
            .env("CONTAINERS_STORAGE_CONF", &storage_config)
            .env("TMPDIR", &image_tmp);
    };
    let storage_arguments = podman_arguments(&storage, &runroot);
    let mut load_arguments = storage_arguments.clone();
    load_arguments.extend(["load".to_owned(), "--quiet".to_owned()]);
    let mut load = Command::new("/usr/bin/podman");
    configure_podman(&mut load);
    let loaded = load
        .args(&load_arguments)
        .stdin(Stdio::from(File::open(&archive).unwrap()))
        .output()
        .unwrap();
    assert!(
        loaded.status.success(),
        "real rootless Podman OCI load failed: {}",
        String::from_utf8_lossy(&loaded.stderr)
    );

    let mut inspect_arguments = storage_arguments;
    inspect_arguments.extend([
        "image".to_owned(),
        "inspect".to_owned(),
        "--format".to_owned(),
        "{{.Digest}}\t{{.Os}}\t{{.Architecture}}".to_owned(),
        fixture.reference.clone(),
    ]);
    let mut inspect = Command::new("/usr/bin/podman");
    configure_podman(&mut inspect);
    let inspected = inspect
        .args(&inspect_arguments)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(
        inspected.status.success(),
        "real rootless Podman exact-reference inspect failed: {}",
        String::from_utf8_lossy(&inspected.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&inspected.stdout).trim(),
        format!("{}\tlinux\tarm64", fixture.manifest_digest)
    );
}

#[test]
fn real_private_content_store_rejects_absent_and_substituted_oci_content() {
    if !local_oci_integration_fixture_available() {
        eprintln!("skipping local OCI integration fixture: containerd/ctr/docker unavailable");
        return;
    }
    let store = PrivateContainerd::start();
    let archive_root = tempdir().unwrap();
    let absent = archive_root.path().join("absent.oci.tar");
    let missing = store.ctr(&["images", "import", "--no-unpack", absent.to_str().unwrap()]);
    assert!(!missing.status.success());

    let mut substituted = registry_fixture();
    let fixture_id = format!("{}-substituted", std::process::id());
    substituted.reference = format!(
        "localhost/vonk/round5-substituted-{fixture_id}:fixture@{}",
        substituted.manifest_digest
    );
    substituted.layer.push(b'!');
    let archive = archive_root.path().join("substituted.oci.tar");
    fs::write(&archive, oci_archive(&substituted)).unwrap();
    let imported = store.ctr(&["images", "import", "--no-unpack", archive.to_str().unwrap()]);
    let stored = store.ctr(&["content", "get", &substituted.layer_digest]);
    assert!(
        !imported.status.success()
            || !stored.status.success()
            || format!("sha256:{}", hex_sha256(&stored.stdout)) != substituted.layer_digest,
        "private OCI store resolved substituted content under the claimed digest"
    );
}
