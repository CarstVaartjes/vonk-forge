#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn runtime_tmp_refuses_symlink_replacement_targets() {
    let data = tempdir().unwrap();
    let outputs = data.path().join("outputs");
    fs::create_dir(&outputs).unwrap();
    let target = data.path().join("outside");
    fs::create_dir(&target).unwrap();
    symlink(&target, outputs.join("tmp")).unwrap();

    assert!(ensure_runtime_tmp(&outputs).is_err());
    assert!(target.is_dir());
    fs::remove_file(outputs.join("tmp")).unwrap();
    ensure_runtime_tmp(&outputs).unwrap();
    assert!(outputs.join("tmp").is_dir());
    assert!(target.is_dir());
}

#[test]
fn model_materialization_temp_cleanup_is_task_owned() {
    let data = tempdir().unwrap();
    let temporary = data.path().join("model.partial");
    fs::write(&temporary, b"incomplete").unwrap();
    {
        let _guard = super::TemporaryArtifact::new(temporary.clone());
    }
    assert!(!temporary.exists());

    fs::write(&temporary, b"published").unwrap();
    {
        let mut guard = super::TemporaryArtifact::new(temporary.clone());
        guard.retain();
    }
    assert_eq!(fs::read(temporary).unwrap(), b"published");
}

#[test]
fn compiled_models_materialize_selection_scoped_colliding_paths() {
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    plan.validate().unwrap();
    let data = tempdir().unwrap();
    let root = data.path().join("distribution").join("models");
    fs::create_dir_all(&root).unwrap();
    for (artifact, bytes) in [
        (&plan.artifacts[0], b"primary".as_slice()),
        (&plan.artifacts[1], b"secondary".as_slice()),
    ] {
        let path = root.join(&artifact.sha256);
        fs::write(&path, bytes).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    }

    let paths =
        materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
            .unwrap();
    assert_eq!(paths.len(), 2);
    assert_eq!(
        fs::read(
            data.path().join(
                "installations/cb555393-764b-4eb6-8f15-b416d289428f/models/primary/config.json"
            )
        )
        .unwrap(),
        b"primary"
    );
    assert_eq!(
        fs::read(data.path().join(
            "installations/cb555393-764b-4eb6-8f15-b416d289428f/models/secondary/config.json"
        ))
        .unwrap(),
        b"secondary"
    );
}

#[test]
fn compiled_models_materialize_valid_empty_support_files() {
    let mut value = compiled_plan();
    let artifact = &mut value["artifacts"][0];
    artifact["selection_id"] = json!("primary");
    artifact["file_id"] = json!("tokenizer-config");
    artifact["path"] = json!("tokenizer_config.json");
    artifact["sha256"] = json!(crate::workloads::EMPTY_SHA256);
    artifact["size_bytes"] = json!(0);
    artifact["roles"] = json!(["tokenizer"]);
    value["artifacts"] = json!([artifact.clone()]);
    let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let data = tempdir().unwrap();
    let source = data.path().join("distribution").join("models");
    fs::create_dir_all(&source).unwrap();
    let source_file = source.join(&plan.artifacts[0].sha256);
    fs::write(&source_file, []).unwrap();
    fs::set_permissions(&source_file, fs::Permissions::from_mode(0o600)).unwrap();
    materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
        .unwrap();
    assert_eq!(
        fs::metadata(data.path().join("installations/cb555393-764b-4eb6-8f15-b416d289428f/models/primary/tokenizer_config.json")).unwrap().len(),
        0
    );
}

#[test]
fn compiled_models_materialize_one_source_for_two_mount_projections() {
    let mut value = compiled_plan();
    let mut projection = value["artifacts"][0].clone();
    projection["mount"]["target"] = json!("/models/target");
    value["artifacts"] = json!([value["artifacts"][0].clone(), projection]);
    let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let data = tempdir().unwrap();
    let source = data.path().join("distribution").join("models");
    fs::create_dir_all(&source).unwrap();
    let source_file = source.join(&plan.artifacts[0].sha256);
    fs::write(&source_file, b"primary").unwrap();
    fs::set_permissions(&source_file, fs::Permissions::from_mode(0o600)).unwrap();
    let paths =
        materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
            .unwrap();
    assert_eq!(paths.len(), 1);
    assert_eq!(
        paths[0],
        data.path()
            .join("installations/cb555393-764b-4eb6-8f15-b416d289428f/models/primary/config.json")
    );
    let installation = data
        .path()
        .join("installations/cb555393-764b-4eb6-8f15-b416d289428f");
    write_installation_metadata(data.path(), &installation, &plan).unwrap();
    let repeated =
        materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
            .unwrap();
    assert_eq!(repeated.len(), 1);
    assert_eq!(plan.artifacts.len(), 2);
}

#[test]
fn a_new_installation_links_the_shared_model_files_instead_of_copying_them() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let store = stock_store(data.path(), &plan);

    let first = linked_installation(data.path(), &plan, FIRST);
    let second = linked_installation(data.path(), &plan, SECOND);

    for (artifact, object) in plan.artifacts.iter().zip(&store) {
        let object = fs::metadata(object).unwrap();
        for installation in [&first, &second] {
            let file = fs::metadata(
                installation
                    .join("models")
                    .join(&artifact.selection_id)
                    .join(&artifact.path),
            )
            .unwrap();
            assert_eq!((file.dev(), file.ino()), (object.dev(), object.ino()));
        }
        // The store and both installations: one inode, three names, one
        // set of bytes on disk.
        assert_eq!(object.nlink(), 3);
    }
    assert_eq!(
        fs::read(first.join("models/primary/config.json")).unwrap(),
        b"primary"
    );

    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime.verify_installation(FIRST).unwrap();
    runtime.verify_installation(SECOND).unwrap();
    // The tree still measures at its full logical size.
    assert!(runtime.installed_bytes(FIRST).unwrap() > 16);
}

#[test]
fn a_sibling_installation_changing_the_link_count_does_not_stale_the_others_custody() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    stock_store(data.path(), &plan);
    linked_installation(data.path(), &plan, FIRST);
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime.verify_installation(FIRST).unwrap();

    // Linking a second installation and removing it again both change the
    // shared inode's change time.
    std::thread::sleep(Duration::from_millis(2));
    let second = linked_installation(data.path(), &plan, SECOND);
    fs::remove_dir_all(second).unwrap();

    let transition = runtime.begin_installation_acl_transition(FIRST).unwrap();
    runtime
        .finish_installation_acl_transition(FIRST, &transition)
        .unwrap();
    runtime.verify_installation(FIRST).unwrap();
}

#[test]
fn a_model_file_linked_to_anything_but_the_store_object_is_refused() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    stock_store(data.path(), &plan);
    let installation = linked_installation(data.path(), &plan, FIRST);
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime.verify_installation(FIRST).unwrap();

    // A second, foreign name for the same inode does not change that it is
    // the store object, but a different inode with two names is not one.
    let foreign = installation.join("models/primary/config.json");
    fs::remove_file(&foreign).unwrap();
    let other = data.path().join("other-secret");
    fs::write(&other, b"primary").unwrap();
    fs::set_permissions(&other, fs::Permissions::from_mode(0o600)).unwrap();
    fs::hard_link(&other, &foreign).unwrap();
    assert!(runtime.verify_installation(FIRST).is_err());
    runtime
        .install(&plan, FIRST, &plan.identity.recipe_revision_sha256)
        .unwrap();
    runtime.verify_installation(FIRST).unwrap();
    assert_eq!(fs::read(&other).unwrap(), b"primary");
    runtime
        .install_with_space_check(
            &plan,
            FIRST,
            &plan.identity.recipe_revision_sha256,
            u64::MAX,
        )
        .unwrap();
}

#[test]
#[cfg(target_os = "linux")]
fn an_object_a_workload_already_runs_from_still_links_into_a_new_installation() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let store = stock_store(data.path(), &plan);
    linked_installation(data.path(), &plan, FIRST);
    // The helper grants the runtime user read access when the first
    // installation starts; the shared inode carries it for every name.
    apply_acl(
        &store[0],
        &[
            (0x0001, 0o6, u32::MAX),
            (0x0002, 0o4, 10_001),
            (0x0004, 0, u32::MAX),
            (0x0010, 0o4, u32::MAX),
            (0x0020, 0, u32::MAX),
        ],
    );
    assert_eq!(fs::metadata(&store[0]).unwrap().mode() & 0o777, 0o640);

    linked_installation(data.path(), &plan, SECOND);
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime.verify_installation(FIRST).unwrap();
    runtime.verify_installation(SECOND).unwrap();
}

#[test]
fn an_installation_replaces_a_private_copy_with_the_shared_object() {
    let data = tempdir().unwrap();
    let (installation_id, installation, plan) = persisted_installation(data.path());
    stock_store(data.path(), &plan);
    let before = fs::metadata(installation.join("models/primary/config.json")).unwrap();

    materialize_compiled_models(data.path(), &plan, &installation_id).unwrap();

    let after = fs::metadata(installation.join("models/primary/config.json")).unwrap();
    assert_ne!((after.dev(), after.ino()), (before.dev(), before.ino()));
    let shared = fs::metadata(
        data.path()
            .join("distribution/models")
            .join(&plan.artifacts[0].sha256),
    )
    .unwrap();
    assert_eq!((after.dev(), after.ino()), (shared.dev(), shared.ino()));
    write_installation_metadata(data.path(), &installation, &plan).unwrap();
    let runner = NoProcess;
    runtime(data.path(), &runner)
        .verify_installation(&installation_id)
        .unwrap();
}

#[test]
fn a_store_object_that_is_not_private_owner_only_is_never_linked() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let store = stock_store(data.path(), &plan);
    fs::set_permissions(&store[0], fs::Permissions::from_mode(0o644)).unwrap();

    assert!(materialize_compiled_models(data.path(), &plan, FIRST).is_err());
    assert!(
        !data
            .path()
            .join("installations")
            .join(FIRST)
            .join("models/primary/config.json")
            .exists()
    );
    // Unsafe local custody is retired by the reader; request preparation
    // rehydrates the exact content rather than trusting a chmod repair.
    stock_store(data.path(), &plan);
    materialize_compiled_models(data.path(), &plan, FIRST).unwrap();
    let installation = data.path().join("installations").join(FIRST);
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&plan).unwrap(),
    )
    .unwrap();
    write_installation_metadata(data.path(), &installation, &plan).unwrap();
    runtime(data.path(), &NoProcess)
        .verify_installation(FIRST)
        .unwrap();
}

#[test]
fn hardlink_failure_reports_layout_without_copy_and_recovers() {
    // Wrong implementation: a link error silently allocates a private copy.
    let data = tempdir().unwrap();
    let plan: CompiledExecutionPlan = serde_json::from_value(compiled_plan()).unwrap();
    let store = stock_store(data.path(), &plan);
    let error =
        materialize_compiled_models_with(data.path(), &plan, FIRST, false, &mut |_, _| {}, &|| {
            false
        })
        .unwrap_err();
    assert!(matches!(
        error,
        OciError::ModelStoreLayout {
            errno: Some(18),
            ..
        }
    ));
    let diagnostic = error.to_string();
    assert!(diagnostic.contains("errno=Some(18)"));
    assert!(diagnostic.contains("models/primary/config.json"));
    assert!(
        !data
            .path()
            .join("installations")
            .join(FIRST)
            .join("models/primary/config.json")
            .exists()
    );
    assert!(store.iter().all(|path| path.exists()));
    linked_installation(data.path(), &plan, FIRST);
    linked_installation(data.path(), &plan, SECOND);
    let object = fs::metadata(&store[0]).unwrap();
    for id in [FIRST, SECOND] {
        let linked = fs::metadata(
            data.path()
                .join("installations")
                .join(id)
                .join("models/primary/config.json"),
        )
        .unwrap();
        assert_eq!((linked.dev(), linked.ino()), (object.dev(), object.ino()));
    }
}

// This uses a hosted, owned bind mount: distribution/models is real tmpfs,
// installations is the runner filesystem. No fake link function or link=false.
#[cfg(target_os = "linux")]
#[test]
#[ignore = "requires the hosted owned cross-device model-store fixture"]
fn real_cross_device_link_failure_reports_layout_without_copy() {
    let data =
        PathBuf::from(std::env::var_os("VONK_OCI_CROSS_DEVICE_ROOT").expect("hosted fixture root"));
    let plan: CompiledExecutionPlan = serde_json::from_value(compiled_plan()).unwrap();
    let model_root = data.join("distribution/models");
    assert_ne!(
        fs::metadata(&model_root).unwrap().dev(),
        fs::metadata(&data).unwrap().dev()
    );
    stock_store(&data, &plan);
    let error = materialize_compiled_models(&data, &plan, FIRST).unwrap_err();
    assert!(matches!(
        error,
        OciError::ModelStoreLayout {
            errno: Some(18),
            ..
        }
    ));
    eprintln!("{error}");
    assert!(
        !data
            .join("installations")
            .join(FIRST)
            .join("models/primary/config.json")
            .exists()
    );
    assert!(model_root.join(&plan.artifacts[0].sha256).exists());
}

#[test]
fn linkable_bytes_count_only_complete_store_objects() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    assert_eq!(super::linkable_model_bytes(data.path(), &plan), 0);
    let store = stock_store(data.path(), &plan);
    assert_eq!(super::linkable_model_bytes(data.path(), &plan), 7 + 9);
    fs::write(&store[1], b"short").unwrap();
    assert_eq!(super::linkable_model_bytes(data.path(), &plan), 7);
}

#[test]
fn cancelled_copy_yields_without_publishing_and_fresh_preparation_converges() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let store = stock_store(data.path(), &plan);
    let checks = std::cell::Cell::new(0);
    let cancelled = || {
        let count = checks.get() + 1;
        checks.set(count);
        count > 2
    };
    assert!(
        materialize_compiled_models_controlled(
            data.path(),
            &plan,
            FIRST,
            true,
            &mut |_, _| {},
            &cancelled,
        )
        .is_err()
    );
    assert!(
        !data
            .path()
            .join("installations")
            .join(FIRST)
            .join("models/primary/config.json")
            .exists()
    );
    assert!(store.iter().all(|path| path.exists()));
    materialize_compiled_models_controlled(
        data.path(),
        &plan,
        FIRST,
        true,
        &mut |_, _| {},
        &|| false,
    )
    .unwrap();
    assert_eq!(
        fs::read(
            data.path()
                .join("installations")
                .join(FIRST)
                .join("models/primary/config.json")
        )
        .unwrap(),
        b"primary"
    );
}
