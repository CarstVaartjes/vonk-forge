#![cfg(test)]
use super::*;
use std::sync::{Arc, Mutex};

const INSTALLATION: &str = "0b7d2c9e-6a51-4c43-9d07-3a3c3c3c3c3c";
const RUNTIME: &str = "5f0c1e2d-3b4a-4c5d-8e6f-7a8b9c0d1e2f";

#[derive(Clone, Default)]
struct Runner {
    inspection: Arc<Mutex<Option<serde_json::Value>>>,
    unavailable: Arc<Mutex<bool>>,
}
impl CommandRunner for Runner {
    fn run(&self, _: &Path, args: &[String]) -> Result<CommandOutput, String> {
        if *self.unavailable.lock().unwrap() {
            return Err("injected inventory outage".into());
        }
        let inspection = self.inspection.lock().unwrap();
        let stdout = if args.starts_with(&["container".into(), "ls".into()]) {
            if inspection.is_some() {
                "a".repeat(64).into_bytes()
            } else {
                vec![]
            }
        } else if args.starts_with(&["container".into(), "inspect".into()]) {
            let container = inspection.as_ref().unwrap();
            let labels = &container["Config"]["Labels"];
            let mut text = format!(
                "{}\t{}\n",
                labels["ai.vonkforge.installation-id"].as_str().unwrap(),
                labels["ai.vonkforge.target-id"].as_str().unwrap()
            );
            for mount in container["Mounts"].as_array().unwrap() {
                text.push_str(&serde_json::to_string(&mount["Source"]).unwrap());
                text.push('\n');
            }
            text.into_bytes()
        } else {
            return Err("unexpected command".into());
        };
        Ok(CommandOutput {
            success: true,
            stdout,
            stderr: vec![],
            exit_code: Some(0),
        })
    }
}
fn executor() -> (tempfile::TempDir, OperationExecutor<Runner>) {
    let root = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(&root.path().join("helper"))
        .with_agent_data(&root.path().join("agent"));
    fs::create_dir_all(&roots.data).unwrap();
    fs::create_dir_all(&roots.agent_data).unwrap();
    let executor = OperationExecutor::new(roots, &[0; 32], Runner::default(), None).unwrap();
    (root, executor)
}
fn file(root: &Path, name: &str) -> PathBuf {
    let path = root.join(name);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(&path, b"retained").unwrap();
    path
}
fn later() -> SystemTime {
    SystemTime::now() + GRACE + Duration::from_secs(60)
}

#[test]
fn unused_helper_records_leave_without_another_request_but_live_owners_stay() {
    // Catches the old image-only sweep: none of these growing stores left.
    let (_root, executor) = executor();
    let data = &executor.roots.data;
    let identity = file(data, "runtime-identities/10001-10002/passwd");
    let intent = file(
        data,
        &format!("installation-intent-fences/{INSTALLATION}.json"),
    );
    let generation = file(
        data,
        &format!("runtime-generation-fences/{INSTALLATION}-{RUNTIME}.json"),
    );
    let history = file(
        data,
        &format!("installation-reconciliation/{INSTALLATION}.json"),
    );
    let damaged = file(
        data,
        &format!("installation-intent-fences/{RUNTIME}.json/corrupt"),
    );
    let exit = file(data, "job-exits/completed.json");
    let replay = file(data, "helper/requests/expired-request");
    let scratch = file(data, "gpu-clock/.interrupted.tmp");
    let checkpoint = file(
        data,
        &format!(
            "gpu-clock/{}",
            vonk_agent_protocol::gpu_clock::CHECKPOINT_FILE
        ),
    );
    file(data, "gpu-clock/lock");
    let observation = file(data, "gpu-clock-observation.json");
    let lock = executor.lock_installation_runtime(INSTALLATION).unwrap();
    let lock_path = data
        .join("runtime-ownership")
        .join(format!("{INSTALLATION}.lock"));
    drop(lock);
    *executor.runner.inspection.lock().unwrap() = Some(serde_json::json!({
        "Config": {"Labels": {"ai.vonkforge.installation-id": INSTALLATION, "ai.vonkforge.target-id": RUNTIME}},
        "Mounts": [{"Source": identity.to_str().unwrap()}]
    }));
    executor.sweep_unused_runtime_images_at(later());
    for path in [
        &identity,
        &intent,
        &generation,
        &history,
        &lock_path,
        &checkpoint,
        &observation,
    ] {
        assert!(path.exists(), "{} was in use", path.display());
    }
    assert!(!exit.exists() && !replay.exists() && !scratch.exists() && !damaged.exists());
    // A failed inventory is unknown. Once it recovers, the same pass collects.
    *executor.runner.inspection.lock().unwrap() = None;
    *executor.runner.unavailable.lock().unwrap() = true;
    executor.sweep_unused_runtime_images_at(later());
    assert!(identity.exists() && intent.exists() && generation.exists());
    *executor.runner.unavailable.lock().unwrap() = false;
    executor.sweep_unused_runtime_images_at(later());
    for path in [&identity, &intent, &generation, &history, &lock_path] {
        assert!(!path.exists(), "{} leaked", path.display());
    }
    assert!(checkpoint.exists() && observation.exists());
}

#[test]
fn installation_and_run_claims_and_held_locks_prevent_collection_then_release() {
    // Catches deleting security/lock records by age without their live owner.
    let (_root, executor) = executor();
    let intent = file(
        &executor.roots.data,
        &format!("installation-intent-fences/{INSTALLATION}.json"),
    );
    let generation = file(
        &executor.roots.data,
        &format!("runtime-generation-fences/{INSTALLATION}-{RUNTIME}.json"),
    );
    let installation = executor
        .roots
        .agent_data
        .join("installations")
        .join(INSTALLATION);
    let run = executor.roots.agent_data.join("run-metadata").join(RUNTIME);
    fs::create_dir_all(&installation).unwrap();
    fs::create_dir_all(&run).unwrap();
    executor.sweep_unused_runtime_images_at(later());
    assert!(intent.exists() && generation.exists());
    fs::remove_dir_all(installation).unwrap();
    fs::remove_dir_all(run).unwrap();
    let held = executor.lock_installation_runtime(INSTALLATION).unwrap();
    executor.sweep_unused_runtime_images_at(later());
    assert!(intent.exists() && generation.exists());
    drop(held);
    let launching = executor.runtime_stores.read().unwrap();
    executor.sweep_unused_runtime_images_at(later());
    assert!(intent.exists() && generation.exists());
    drop(launching);
    executor.sweep_unused_runtime_images_at(later());
    assert!(!intent.exists() && !generation.exists());
}

#[test]
fn grace_and_symlinks_are_respected_and_unused_job_fences_do_not_need_an_uninstall() {
    // Catches retaining one generation fence per completed job forever while
    // its installation stays, and following a helper-store symlink outside it.
    let (root, executor) = executor();
    let generation = file(
        &executor.roots.data,
        &format!("runtime-generation-fences/{INSTALLATION}-{RUNTIME}.json"),
    );
    fs::create_dir_all(
        executor
            .roots
            .agent_data
            .join("installations")
            .join(INSTALLATION),
    )
    .unwrap();
    let outside = file(root.path(), "outside/keep");
    std::os::unix::fs::symlink(
        outside.parent().unwrap(),
        executor.roots.data.join("job-exits"),
    )
    .unwrap();
    executor.sweep_unused_runtime_images_at(SystemTime::now());
    assert!(generation.exists());
    executor.sweep_unused_runtime_images_at(later());
    assert!(!generation.exists() && outside.exists());
}

#[test]
fn collected_security_fences_require_a_new_controller_challenge() {
    // Catches a collector that lets a previously signed START replay after GC.
    let (_root, executor) = executor();
    let installation = uuid::Uuid::parse_str(INSTALLATION).unwrap();
    let identity = RuntimeEffectIdentity {
        installation_id: installation,
        runtime_id: uuid::Uuid::parse_str(RUNTIME).unwrap(),
        run_generation: 1,
    };
    let challenge = executor
        .prepare_runtime_generation_fence(&identity)
        .unwrap_err();
    let OperationError::InstallationIntentObservationRequired { nonce } = challenge else {
        panic!("a fresh fence must challenge")
    };
    executor
        .accept_installation_intent(installation, Some(&nonce), Some(1), false)
        .unwrap();
    executor
        .prepare_runtime_generation_fence(&identity)
        .unwrap();
    executor.sweep_unused_runtime_images_at(later());
    assert!(matches!(
        executor.accept_installation_intent(installation, Some(&nonce), Some(1), false),
        Err(OperationError::InstallationIntentObservationRequired { .. })
    ));
    assert!(matches!(
        executor.prepare_runtime_generation_fence(&identity),
        Err(OperationError::InstallationIntentObservationRequired { .. })
    ));
}
