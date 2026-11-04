#![cfg(test)]
use super::*;
use crate::operations::test_support::{Host, host_executor, job};
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
                "{}\t{}\t{}\n",
                "a".repeat(64),
                labels["ai.vonkforge.installation-id"].as_str().unwrap(),
                labels["ai.vonkforge.target-id"].as_str().unwrap()
            );
            for mount in container["Mounts"].as_array().unwrap() {
                text.push_str(&serde_json::to_string(&mount["Source"]).unwrap());
                text.push('\n');
            }
            text.push_str("--\n");
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
    let compile_cache = file(data, "compile-cache/unused/data/kernel");
    fs::set_permissions(
        data.join("compile-cache"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    *executor.runner.inspection.lock().unwrap() = None;
    *executor.runner.unavailable.lock().unwrap() = true;
    executor.sweep_unused_runtime_images_at(later());
    assert!(identity.exists() && intent.exists() && generation.exists());
    assert!(
        compile_cache.exists(),
        "unknown container ownership removed a compile cache"
    );
    *executor.runner.unavailable.lock().unwrap() = false;
    executor.sweep_unused_runtime_images_at(later());
    for path in [&identity, &intent, &generation, &history, &lock_path] {
        assert!(!path.exists(), "{} leaked", path.display());
    }
    assert!(checkpoint.exists() && observation.exists());
    assert!(
        !compile_cache.exists(),
        "unused compile key leaked after ownership recovered"
    );
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

fn removal(
    executor: &OperationExecutor<Runner>,
    store: vonk_agent_protocol::generated::AgentRetainedStore,
) -> vonk_agent_protocol::generated::AgentStoreRemovalRequest {
    let name = match store {
        vonk_agent_protocol::generated::AgentRetainedStore::Runs => "runs",
        vonk_agent_protocol::generated::AgentRetainedStore::Installations => "installations",
    };
    let metadata = fs::metadata(executor.roots.agent_data.join(name).join(INSTALLATION)).unwrap();
    vonk_agent_protocol::generated::AgentStoreRemovalRequest {
        request_id: uuid::Uuid::new_v4(),
        store,
        scope_id: INSTALLATION.parse().unwrap(),
        device: metadata.dev(),
        inode: metadata.ino(),
    }
}

#[test]
fn agent_store_removal_refuses_live_owners_replacements_and_symlinked_roots() {
    // Catches broad privileged rm, unknown Docker ownership treated as absent,
    // deleting a replacement after a lost response, and following store links.
    use vonk_agent_protocol::generated::AgentRetainedStore;
    for store in [AgentRetainedStore::Runs, AgentRetainedStore::Installations] {
        let (_root, executor) = executor();
        let name = match store {
            AgentRetainedStore::Runs => "runs",
            AgentRetainedStore::Installations => "installations",
        };
        let scope = executor.roots.agent_data.join(name).join(INSTALLATION);
        file(&scope, "private/owned-by-runtime");
        let request = removal(&executor, store);
        let launch = executor.runtime_stores.read().unwrap();
        assert!(matches!(
            executor.remove_agent_store(&request),
            Err(OperationError::InstallationReconciliationBusy)
        ));
        drop(launch);
        let wrong_owner = OperationExecutor::new(
            executor.roots.clone(),
            &[0; 32],
            executor.runner.clone(),
            None,
        )
        .unwrap()
        .with_runtime_request_owner(rustix::process::geteuid().as_raw() + 1);
        assert!(matches!(
            wrong_owner.remove_agent_store(&request),
            Err(OperationError::UnsafePath)
        ));
        *executor.runner.inspection.lock().unwrap() = Some(serde_json::json!({
            "Config": {"Labels": {"ai.vonkforge.installation-id": INSTALLATION, "ai.vonkforge.target-id": INSTALLATION}},
            "Mounts": [{"Source": scope.join("private")}]
        }));
        assert!(matches!(
            executor.remove_agent_store(&request),
            Err(OperationError::InstallationReconciliationBusy)
        ));
        *executor.runner.inspection.lock().unwrap() = None;
        *executor.runner.unavailable.lock().unwrap() = true;
        assert!(executor.remove_agent_store(&request).is_err());
        *executor.runner.unavailable.lock().unwrap() = false;
        let mut replaced = request.clone();
        replaced.inode += 1;
        assert!(matches!(
            executor.remove_agent_store(&replaced),
            Err(OperationError::UnsafePath)
        ));
        let moved = executor.roots.agent_data.join("outside");
        fs::rename(scope.parent().unwrap(), &moved).unwrap();
        std::os::unix::fs::symlink(&moved, scope.parent().unwrap()).unwrap();
        assert!(matches!(
            executor.remove_agent_store(&request),
            Err(OperationError::UnsafePath)
        ));
        assert!(
            moved
                .join(INSTALLATION)
                .join("private/owned-by-runtime")
                .exists()
        );
        fs::remove_file(scope.parent().unwrap()).unwrap();
        fs::rename(&moved, scope.parent().unwrap()).unwrap();
        let outside = file(&executor.roots.agent_data, "sentinel");
        std::os::unix::fs::symlink(&outside, scope.join("escape")).unwrap();
        executor.remove_agent_store(&request).unwrap();
        executor.remove_agent_store(&request).unwrap();
        assert!(!scope.exists());
        assert!(outside.exists());
    }
}

#[test]
fn interrupted_store_collection_does_not_poison_future_retention() {
    // Catches treating a poisoned launch-exclusion lock as permanently busy.
    use vonk_agent_protocol::generated::AgentRetainedStore;
    let (_root, executor) = executor();
    let scope = executor.roots.agent_data.join("runs").join(INSTALLATION);
    file(&scope, "private/retained");
    let request = removal(&executor, AgentRetainedStore::Runs);
    assert!(
        std::panic::catch_unwind(|| {
            let _collection = executor.runtime_stores.write().unwrap();
            panic!("injected collector death");
        })
        .is_err()
    );
    executor.remove_agent_store(&request).unwrap();
    assert!(!scope.exists());
}

fn managed_containers(count: usize) -> Vec<crate::operations::test_support::Container> {
    (0..count)
        .map(|number| {
            job(
                &format!("run-{number}"),
                &format!("{number:032x}"),
                "running",
            )
        })
        .collect()
}

#[test]
fn owner_collection_completes_however_many_managed_containers_exist() {
    // The id listing and every container's owner read grow with the
    // containers and with their model files (one bind mount per file); a
    // total output bound ended the pass for any big model.
    let host = Host {
        mounts_per_container: 200,
        ..Host::with(&managed_containers(500))
    };
    let (_temp, executor) = host_executor(&host);
    let owners = executor
        .helper_store_owners(Instant::now() + Duration::from_secs(600))
        .unwrap();
    assert_eq!(owners.runtimes.len(), 500);
    assert_eq!(owners.mounts.len(), 500 * 200);
    assert!(
        owners
            .installations
            .contains(crate::operations::test_support::INSTALLATION)
    );
    // One listing and ceil(500 / OWNER_BATCH) inspections; no re-listing.
    assert_eq!(
        host.calls.lock().unwrap().len(),
        1 + 500_usize.div_ceil(OWNER_BATCH)
    );
}

#[test]
fn a_pass_with_no_time_left_is_busy_before_it_asks_docker_anything_more() {
    let host = Host::with(&managed_containers(500));
    let (_temp, executor) = host_executor(&host);
    let result = executor.helper_store_owners(Instant::now());
    assert!(matches!(
        result,
        Err(OperationError::InstallationReconciliationBusy)
    ));
    assert!(host.calls.lock().unwrap().is_empty());
}

#[test]
fn a_container_with_no_provable_owner_keeps_every_store_until_it_is_proven() {
    let containers = managed_containers(3);
    let unproven = containers[1].id();
    let host = Host::with(&containers);
    host.uninspectable.lock().unwrap().push(unproven);
    let (_temp, executor) = host_executor(&host);
    let deadline = Instant::now() + Duration::from_secs(600);
    // It may own any store, so no owner set is offered.
    assert!(matches!(
        executor.helper_store_owners(deadline),
        Err(OperationError::InstallationReconciliationBusy)
    ));
    // Listing, one inspection, one re-listing: no call per unproven container.
    assert_eq!(host.calls.lock().unwrap().len(), 3);
}

#[test]
fn a_container_gone_before_its_inspection_owns_nothing() {
    let host = Host::with(&managed_containers(2));
    host.ghosts.lock().unwrap().push("a".repeat(64));
    host.uninspectable.lock().unwrap().push("a".repeat(64));
    let (_temp, executor) = host_executor(&host);
    let owners = executor
        .helper_store_owners(Instant::now() + Duration::from_secs(600))
        .unwrap();
    assert_eq!(owners.runtimes.len(), 2);
}
