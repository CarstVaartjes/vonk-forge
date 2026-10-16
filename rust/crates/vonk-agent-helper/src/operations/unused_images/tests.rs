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
    let plan = serde_json::json!({
        "runtime_image": {
            "oci_layout_sha256": manifest,
            "image_digest": format!("sha256:{manifest}"),
        },
    });
    fs::write(installation.join("spec.json"), plan.to_string()).unwrap();
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
