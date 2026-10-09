#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn damaged_model_projections_rebuild_without_following_foreign_targets() {
    for damage_parent in [false, true] {
        let data = tempdir().unwrap();
        let plan: CompiledExecutionPlan = serde_json::from_value(compiled_plan()).unwrap();
        let objects = data.path().join("distribution/models");
        fs::create_dir_all(&objects).unwrap();
        for (artifact, bytes) in [
            (&plan.artifacts[0], b"primary".as_slice()),
            (&plan.artifacts[1], b"secondary".as_slice()),
        ] {
            let source = objects.join(&artifact.sha256);
            fs::write(&source, bytes).unwrap();
            fs::set_permissions(source, fs::Permissions::from_mode(0o600)).unwrap();
        }
        let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
        let paths = materialize_compiled_models(data.path(), &plan, installation_id).unwrap();
        let foreign = data.path().join("foreign");
        fs::create_dir(&foreign).unwrap();
        fs::write(foreign.join("config.json"), b"foreign bytes").unwrap();
        if damage_parent {
            fs::remove_file(&paths[0]).unwrap();
            fs::remove_dir(paths[0].parent().unwrap()).unwrap();
            symlink(&foreign, paths[0].parent().unwrap()).unwrap();
        } else {
            fs::remove_file(&paths[0]).unwrap();
            symlink(foreign.join("config.json"), &paths[0]).unwrap();
        }
        materialize_compiled_models(data.path(), &plan, installation_id).unwrap();
        assert_eq!(fs::read(&paths[0]).unwrap(), b"primary");
        assert_eq!(
            fs::read(foreign.join("config.json")).unwrap(),
            b"foreign bytes"
        );
        let inode = fs::metadata(&paths[0]).unwrap().ino();
        materialize_compiled_models(data.path(), &plan, installation_id).unwrap();
        assert_eq!(fs::metadata(&paths[0]).unwrap().ino(), inode);
    }
}

#[test]
fn cancelled_copy_resumes_the_same_content_checkpoint_and_admits_a_fresh_install() {
    // Wrong implementation: unwind deletes a multi-hundred-GB prefix and
    // the next worker copies from byte zero into an unrelated temporary inode.
    let data = tempdir().unwrap();
    let mut plan: CompiledExecutionPlan = serde_json::from_value(compiled_plan()).unwrap();
    let bytes = vec![7_u8; 256 * 1024];
    plan.artifacts[0].size_bytes = bytes.len() as u64;
    let objects = data.path().join("distribution/models");
    fs::create_dir_all(&objects).unwrap();
    for (artifact, content) in [
        (&plan.artifacts[0], bytes.as_slice()),
        (&plan.artifacts[1], b"secondary".as_slice()),
    ] {
        let source = objects.join(&artifact.sha256);
        fs::write(&source, content).unwrap();
        fs::set_permissions(&source, fs::Permissions::from_mode(0o600)).unwrap();
    }
    let id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let parent = data
        .path()
        .join("installations")
        .join(id)
        .join("models/primary");
    let partial = parent.join(format!(
        ".{}.{}.partial",
        plan.artifacts[0].file_id, plan.artifacts[0].sha256
    ));
    assert!(
        materialize_compiled_models_with(data.path(), &plan, id, false, &mut |_, _| {}, &|| {
            fs::metadata(&partial).is_ok_and(|metadata| metadata.len() >= 64 * 1024)
        })
        .is_err()
    );
    let prefix = fs::read(&partial).unwrap();
    assert!(bytes.starts_with(&prefix));
    assert!(prefix.len() < bytes.len());
    let inode = fs::metadata(&partial).unwrap().ino();
    let mut observed = Vec::new();
    let paths = materialize_compiled_models_with(
        data.path(),
        &plan,
        id,
        false,
        &mut |done, _| observed.push(done),
        &|| false,
    )
    .unwrap();
    assert!(observed.contains(&(prefix.len() as u64)));
    assert_eq!(fs::metadata(&paths[0]).unwrap().ino(), inode);
    assert_eq!(fs::read(&paths[0]).unwrap(), bytes);
    let runtime = runtime(data.path(), &NoProcess);
    runtime
        .install(&plan, id, &plan.identity.recipe_revision_sha256)
        .unwrap();
    runtime.verify_installation(id).unwrap();
    let completed = fs::metadata(&paths[0]).unwrap().ino();
    runtime
        .install_with_space_check(&plan, id, &plan.identity.recipe_revision_sha256, u64::MAX)
        .unwrap();
    assert_eq!(fs::metadata(&paths[0]).unwrap().ino(), completed);
    // Editorial provenance cannot turn identical completed content into a
    // different installation or demand another copy reservation.
    let accepted_content = plan.identity.recipe_revision_sha256.clone();
    plan.identity.recipe_revision_sha256 = "e".repeat(64);
    runtime
        .install_with_space_check(&plan, id, &accepted_content, u64::MAX)
        .unwrap();
    assert_eq!(fs::metadata(&paths[0]).unwrap().ino(), completed);
}

#[test]
fn killed_copy_worker_resumes_its_durable_prefix() {
    // Wrong implementation: restart abandons the killed writer's compatible
    // checkpoint and consumes another complete copy's capacity.
    let child_root = std::env::var_os("VONK_WC2_COPY_CHILD");
    let data = tempdir().unwrap();
    let root = child_root
        .as_ref()
        .map(PathBuf::from)
        .unwrap_or_else(|| data.path().to_path_buf());
    let mut plan: CompiledExecutionPlan = serde_json::from_value(compiled_plan()).unwrap();
    let bytes = vec![7_u8; 256 * 1024];
    plan.artifacts[0].size_bytes = bytes.len() as u64;
    let id = "cb555393-764b-4eb6-8f15-b416d289428f";
    let partial = root
        .join("installations")
        .join(id)
        .join("models/primary")
        .join(format!(
            ".{}.{}.partial",
            plan.artifacts[0].file_id, plan.artifacts[0].sha256
        ));
    let marker = root.join("copy-paused");
    if child_root.is_some() {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        let _ = materialize_compiled_models_with(&root, &plan, id, false, &mut |_, _| {}, &|| {
            if fs::metadata(&partial).is_ok_and(|metadata| metadata.len() >= 64 * 1024) {
                fs::write(&marker, b"prefix retained").unwrap();
                while std::time::Instant::now() < deadline {
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                return true;
            }
            false
        });
        return;
    }
    let objects = root.join("distribution/models");
    fs::create_dir_all(&objects).unwrap();
    for (artifact, content) in [
        (&plan.artifacts[0], bytes.as_slice()),
        (&plan.artifacts[1], b"secondary".as_slice()),
    ] {
        let source = objects.join(&artifact.sha256);
        fs::write(&source, content).unwrap();
        fs::set_permissions(source, fs::Permissions::from_mode(0o600)).unwrap();
    }
    let test_name = format!(
        "{}::killed_copy_worker_resumes_its_durable_prefix",
        module_path!().split_once("::").unwrap().1
    );
    let mut child = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", &test_name, "--nocapture"])
        .env("VONK_WC2_COPY_CHILD", &root)
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while !marker.exists() && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let paused = marker.exists();
    if child.try_wait().unwrap().is_none() {
        child.kill().unwrap();
    }
    let reap_deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while child.try_wait().unwrap().is_none() {
        assert!(
            std::time::Instant::now() < reap_deadline,
            "killed copy worker did not exit before the reap deadline"
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    assert!(paused, "child did not reach the production copy boundary");
    let prefix = fs::metadata(&partial).unwrap();
    assert!(prefix.len() > 0 && prefix.len() < bytes.len() as u64);
    let mut progress = Vec::new();
    let paths = materialize_compiled_models_with(
        &root,
        &plan,
        id,
        false,
        &mut |done, _| progress.push(done),
        &|| false,
    )
    .unwrap();
    assert!(progress.contains(&prefix.len()));
    assert_eq!(fs::metadata(&paths[0]).unwrap().ino(), prefix.ino());
    assert_eq!(fs::read(&paths[0]).unwrap(), bytes);
    let completed = fs::metadata(&paths[0]).unwrap().ino();
    write_installation_metadata(&root, &root.join("installations").join(id), &plan).unwrap();
    materialize_compiled_models(&root, &plan, id).unwrap();
    assert_eq!(fs::metadata(&paths[0]).unwrap().ino(), completed);
}
