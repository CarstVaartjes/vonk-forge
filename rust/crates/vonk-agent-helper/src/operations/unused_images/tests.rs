#![cfg(test)]
//! Which pulled runtime images a sweep removes, and which it keeps.
use super::*;
use crate::operations::test_support::*;

const KEPT_INSTALLATION: &str = "60000000-0000-4000-8000-000000000006";

fn executor() -> (tempfile::TempDir, PullRunner, OperationExecutor<PullRunner>) {
    let temp = tempfile::tempdir().unwrap();
    let runner = PullRunner {
        pulled_config: format!("sha256:{}", "c".repeat(64)),
        ..PullRunner::default()
    };
    let roots = ManagedRoots::under(&temp.path().join("helper"))
        .with_agent_data(&temp.path().join("agent"));
    fs::create_dir_all(&roots.data).unwrap();
    let executor = OperationExecutor::new(roots, &[0; 32], runner.clone(), None).unwrap();
    fs::create_dir_all(&executor.roots.agent_data).unwrap();
    retention(&executor, 0, Vec::new());
    (temp, runner, executor)
}

fn pull(executor: &OperationExecutor<PullRunner>, manifest: &str) {
    executor
        .runtime_image_pull(&pull_arguments(manifest, &"c".repeat(64)))
        .unwrap();
}

fn local(manifest: &str) -> String {
    format!("localhost/vonk/compiled-runtime-{manifest}")
}

fn present(runner: &PullRunner, manifest: &str) -> bool {
    runner.images.lock().unwrap().contains_key(&local(manifest))
}

/// An installation on this host whose plan names `manifest`'s image.
fn install(executor: &OperationExecutor<PullRunner>, manifest: &str) {
    let installation = executor
        .roots
        .agent_data
        .join("installations")
        .join(KEPT_INSTALLATION);
    fs::create_dir_all(&installation).unwrap();
    let mut plan = compiled_plan_for_runtime_authority();
    plan.runtime_image.oci_layout_sha256 = manifest.to_owned();
    plan.runtime_image.image_digest = format!("sha256:{manifest}");
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&plan).unwrap(),
    )
    .unwrap();
}

/// The image was last pulled `hours` ago.
fn pulled_ago(executor: &OperationExecutor<PullRunner>, manifest: &str, hours: u64) {
    File::options()
        .write(true)
        .open(executor.roots.runtime_image_receipts.join(manifest))
        .unwrap()
        .set_modified(std::time::SystemTime::now() - Duration::from_secs(hours * 3600))
        .unwrap();
}

#[test]
fn an_uninstalled_image_goes_after_its_grace_and_a_named_one_stays() {
    let (_temp, runner, executor) = executor();
    let (uninstalled, installed) = ("a".repeat(64), "b".repeat(64));
    pull(&executor, &uninstalled);
    pull(&executor, &installed);
    install(&executor, &installed);

    // Just pulled: an install may not have written its plan yet.
    executor.sweep_unused_runtime_images();
    assert!(present(&runner, &uninstalled));

    pulled_ago(&executor, &uninstalled, 25);
    pulled_ago(&executor, &installed, 25);
    executor.sweep_unused_runtime_images();

    assert!(!present(&runner, &uninstalled), "an unused image was kept");
    assert!(
        !executor
            .roots
            .runtime_image_receipts
            .join(&uninstalled)
            .exists()
    );
    assert!(present(&runner, &installed), "a named image was removed");
    // A later install pulls it again.
    pull(&executor, &uninstalled);
    assert!(present(&runner, &uninstalled));
}

#[test]
fn an_image_a_container_uses_stays_until_that_container_is_gone() {
    let (_temp, runner, executor) = executor();
    let manifest = "a".repeat(64);
    pull(&executor, &manifest);
    pulled_ago(&executor, &manifest, 25);
    runner.in_use.lock().unwrap().insert(local(&manifest));

    executor.sweep_unused_runtime_images();
    assert!(present(&runner, &manifest));
    assert!(
        executor
            .roots
            .runtime_image_receipts
            .join(&manifest)
            .exists()
    );

    runner.in_use.lock().unwrap().clear();
    executor.sweep_unused_runtime_images();
    assert!(!present(&runner, &manifest));
}

#[test]
fn nothing_is_removed_while_a_pull_runs_or_installations_are_unreadable() {
    let (_temp, runner, executor) = executor();
    let manifest = "a".repeat(64);
    pull(&executor, &manifest);
    pulled_ago(&executor, &manifest, 25);

    {
        let _pulling = executor.image_pulls.read().unwrap();
        executor.sweep_unused_runtime_images();
    }
    assert!(present(&runner, &manifest));

    // What the installations name is unknown, not nothing.
    fs::create_dir_all(&executor.roots.agent_data).unwrap();
    fs::write(executor.roots.agent_data.join("installations"), b"").unwrap();
    executor.sweep_unused_runtime_images();
    assert!(present(&runner, &manifest));
}

#[test]
fn an_uninstall_starts_its_images_grace() {
    let (_temp, runner, executor) = executor();
    let manifest = "a".repeat(64);
    pull(&executor, &manifest);
    install(&executor, &manifest);
    pulled_ago(&executor, &manifest, 240);

    // The uninstall's cleanup, then the installation is gone.
    executor.start_image_grace(KEPT_INSTALLATION);
    fs::remove_dir_all(executor.roots.agent_data.join("installations")).unwrap();
    executor.sweep_unused_runtime_images();
    assert!(present(&runner, &manifest), "removed within its grace");

    pulled_ago(&executor, &manifest, 25);
    executor.sweep_unused_runtime_images();
    assert!(!present(&runner, &manifest));
}

fn retention(executor: &OperationExecutor<PullRunner>, required: u64, protected: Vec<String>) {
    let request = vonk_agent_protocol::generated::StoreRetentionRequest {
        issued_at: std::time::SystemTime::now()
            .duration_since(std::time::SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs(),
        expires_at: std::time::SystemTime::now()
            .duration_since(std::time::SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs()
            + 300,
        required_free_bytes: required,
        remove_model_objects: Vec::new(),
        protected_model_objects: Vec::new(),
        protected_image_addresses: protected,
    };
    fs::write(
        executor.roots.agent_data.join("store-retention.json"),
        serde_json::to_vec(&request).unwrap(),
    )
    .unwrap();
}

#[test]
fn disk_demand_removes_young_unused_images_but_keeps_controller_and_container_references() {
    let (_temp, runner, executor) = executor();
    let (unused, pinned, running) = ("a".repeat(64), "b".repeat(64), "d".repeat(64));
    for manifest in [&unused, &pinned, &running] {
        pull(&executor, manifest);
    }
    runner.in_use.lock().unwrap().insert(local(&running));
    for manifest in [&unused, &pinned, &running] {
        pulled_ago(&executor, manifest, 1);
    }
    retention(&executor, 100, vec![pinned.clone()]);
    executor.sweep_runtime_images_unused_for(UNUSED_IMAGE_GRACE, &|| Some(0));
    assert!(!present(&runner, &unused));
    assert!(present(&runner, &pinned));
    assert!(present(&runner, &running));
}

#[test]
fn unavailable_projection_keeps_young_images_but_reclaims_unreferenced_old_ones() {
    let (_temp, runner, executor) = executor();
    let unused = "a".repeat(64);
    pull(&executor, &unused);
    retention(&executor, 100, Vec::new());
    executor.sweep_runtime_images_unused_for(UNUSED_IMAGE_GRACE, &|| Some(100));
    assert!(present(&runner, &unused));
    pulled_ago(&executor, &unused, 25);
    fs::remove_file(executor.roots.agent_data.join("store-retention.json")).unwrap();
    executor.sweep_runtime_images_unused_for(UNUSED_IMAGE_GRACE, &|| Some(0));
    assert!(!present(&runner, &unused));
}

#[test]
fn pressure_never_removes_an_image_pulled_after_the_controllers_reference_scan() {
    let (_temp, runner, executor) = executor();
    retention(&executor, 100, Vec::new());
    let image = "a".repeat(64);
    pull(&executor, &image);
    executor.sweep_runtime_images_unused_for(UNUSED_IMAGE_GRACE, &|| Some(0));
    assert!(present(&runner, &image));
}

#[test]
fn stale_or_unreadable_projection_cannot_veto_safe_image_reclamation() {
    // Catches a stale scan globally vetoing cleanup, while retaining every
    // readable protection and the installation/container/pull safety boundaries.
    for corrupt in [false, true] {
        let (_temp, runner, executor) = executor();
        let (unused, installed, running, protected) = (
            "a".repeat(64),
            "b".repeat(64),
            "d".repeat(64),
            "e".repeat(64),
        );
        for manifest in [&unused, &installed, &running, &protected] {
            pull(&executor, manifest);
            pulled_ago(&executor, manifest, 25);
        }
        install(&executor, &installed);
        runner.in_use.lock().unwrap().insert(local(&running));
        retention(&executor, 100, vec![protected.clone()]);
        let path = executor.roots.agent_data.join("store-retention.json");
        if corrupt {
            fs::write(&path, b"{").unwrap();
        } else {
            let mut request: vonk_agent_protocol::generated::StoreRetentionRequest =
                serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            request.expires_at = 0;
            fs::write(&path, serde_json::to_vec(&request).unwrap()).unwrap();
        }
        executor.sweep_runtime_images_unused_for(UNUSED_IMAGE_GRACE, &|| Some(0));
        assert!(!present(&runner, &unused));
        assert!(present(&runner, &installed) && present(&runner, &running));
        if !corrupt {
            assert!(present(&runner, &protected));
        }
    }
}
