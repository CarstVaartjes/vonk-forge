#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn installation_cleanup_removes_only_private_runtime_cache_and_is_retryable() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(&temp.path().join("agent-data"));
    let installation_id = "10000000-0000-4000-8000-000000000001";
    let installation = roots.agent_data.join("installations").join(installation_id);
    let cache = installation.join("runtime-cache");
    let private = cache.join("home/private/nested");
    let models = installation.join("models/primary");
    let outside = temp.path().join("outside");
    fs::create_dir_all(&private).unwrap();
    fs::create_dir_all(&models).unwrap();
    fs::create_dir_all(&outside).unwrap();
    fs::write(private.join("engine-owned.bin"), b"private").unwrap();
    fs::write(models.join("model.bin"), b"model").unwrap();
    fs::write(outside.join("sentinel"), b"outside").unwrap();
    std::os::unix::fs::symlink(&outside, cache.join("outside-link")).unwrap();
    fs::set_permissions(cache.join("home"), fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(
        cache.join("home/private"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    fs::set_permissions(&private, fs::Permissions::from_mode(0o700)).unwrap();
    if rustix::process::geteuid().is_root() {
        for path in [cache.join("home"), cache.join("home/private"), private] {
            rustix::fs::chown(
                path,
                Some(rustix::process::Uid::from_raw(10001)),
                Some(rustix::process::Gid::from_raw(10001)),
            )
            .unwrap();
        }
    }
    let executor = OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();

    executor
        .runtime_installation_cleanup(installation_id)
        .unwrap();
    executor
        .runtime_installation_cleanup(installation_id)
        .unwrap();

    assert!(!cache.exists());
    assert_eq!(fs::read(models.join("model.bin")).unwrap(), b"model");
    assert_eq!(fs::read(outside.join("sentinel")).unwrap(), b"outside");
}

#[test]
fn installation_cleanup_rejects_symlinked_managed_roots() {
    let temp = tempfile::tempdir().unwrap();
    let outside = temp.path().join("outside");
    let linked = temp.path().join("agent-data");
    let installation_id = "10000000-0000-4000-8000-000000000001";
    let cache = outside
        .join("installations")
        .join(installation_id)
        .join("runtime-cache");
    fs::create_dir_all(&cache).unwrap();
    fs::write(cache.join("sentinel"), b"outside").unwrap();
    std::os::unix::fs::symlink(&outside, &linked).unwrap();
    let roots = ManagedRoots::under(&linked);
    let executor = OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();

    assert!(
        executor
            .runtime_installation_cleanup(installation_id)
            .is_err()
    );
    assert_eq!(fs::read(cache.join("sentinel")).unwrap(), b"outside");
}

#[test]
fn installation_cleanup_treats_each_missing_private_cache_level_as_complete() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(&temp.path().join("agent-data"));
    let installation_id = "10000000-0000-4000-8000-000000000001";
    let executor = |roots: &ManagedRoots| {
        OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap()
    };

    fs::create_dir_all(&roots.agent_data).unwrap();
    executor(&roots)
        .runtime_installation_cleanup(installation_id)
        .unwrap();
    fs::create_dir(roots.agent_data.join("installations")).unwrap();
    executor(&roots)
        .runtime_installation_cleanup(installation_id)
        .unwrap();
    fs::create_dir(roots.agent_data.join("installations").join(installation_id)).unwrap();
    executor(&roots)
        .runtime_installation_cleanup(installation_id)
        .unwrap();
}

#[test]
fn reconciliation_clears_only_private_cache_and_replays_after_agent_removal() {
    let (_temp, roots, identity, runtime_cache, shared_cache) = helper_reconciliation_fixture();
    let runner = ReconciliationListingRunner::new(CommandOutput {
        success: true,
        stdout: Vec::new(),
        stderr: Vec::new(),
        exit_code: Some(0),
    });
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();

    executor.runtime_reconcile_installation(&identity).unwrap();
    assert!(!runtime_cache.exists());
    assert_eq!(fs::read(&shared_cache).unwrap(), b"shared model cache");
    let receipt_path = roots
        .data
        .join(INSTALLATION_RECONCILIATION_DIRECTORY)
        .join(format!("{}.json", identity.installation_id));
    assert!(receipt_path.is_file());

    let calls = runner.calls.lock().unwrap();
    assert_eq!(calls.len(), 2);
    let arguments = &calls[0];
    assert!(arguments.windows(2).any(|pair| pair
        == [
            "--filter",
            &format!(
                "label=ai.vonkforge.installation-id={}",
                identity.installation_id
            ),
        ]));
    assert!(
        calls[1]
            .iter()
            .any(|argument| argument == &format!("volume={}", runtime_cache.display()))
    );
    drop(calls);

    // The agent may finish deleting its exact installation after the
    // helper's acknowledgement is lost. The helper tombstone must retain
    // enough identity to replay that same authorization without the
    // deleted marker or malformed plan.
    let installation = roots
        .agent_data
        .join("installations")
        .join(identity.installation_id.to_string());
    fs::remove_dir_all(&installation).unwrap();
    executor.runtime_reconcile_installation(&identity).unwrap();
    assert!(receipt_path.is_file());
    assert_eq!(fs::read(&shared_cache).unwrap(), b"shared model cache");
}

#[test]
fn reconciliation_reobserves_an_empty_replacement_without_deleting_it() {
    let (_temp, roots, identity, _runtime_cache, _shared_cache) = helper_reconciliation_fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        &[0; 32],
        ReconciliationListingRunner::new(CommandOutput {
            success: true,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: Some(0),
        }),
        None,
    )
    .unwrap();
    executor.runtime_reconcile_installation(&identity).unwrap();

    let installation = roots
        .agent_data
        .join("installations")
        .join(identity.installation_id.to_string());
    let original = roots
        .agent_data
        .join("installations")
        .join(format!("{}.original", identity.installation_id));
    let spec = fs::read(installation.join("spec.json")).unwrap();
    let recipe = fs::read(installation.join("recipe-content.sha256")).unwrap();
    fs::rename(&installation, &original).unwrap();
    fs::create_dir(&installation).unwrap();
    fs::set_permissions(&installation, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(installation.join("spec.json"), spec).unwrap();
    fs::set_permissions(
        installation.join("spec.json"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    fs::write(installation.join("recipe-content.sha256"), recipe).unwrap();
    fs::set_permissions(
        installation.join("recipe-content.sha256"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();

    executor.runtime_reconcile_installation(&identity).unwrap();
    executor
        .refuse_reconciled_runtime(&identity.installation_id.to_string())
        .unwrap();
    assert!(installation.is_dir());
    assert!(original.is_dir());
}

#[test]
fn reconciliation_of_an_absent_installation_is_complete_without_history() {
    let (_temp, roots, identity, _runtime_cache, _shared_cache) = helper_reconciliation_fixture();
    let installation = roots
        .agent_data
        .join("installations")
        .join(identity.installation_id.to_string());
    fs::remove_dir_all(&installation).unwrap();
    let receipt_path = roots
        .data
        .join(INSTALLATION_RECONCILIATION_DIRECTORY)
        .join(format!("{}.json", identity.installation_id));
    let executor = OperationExecutor::new(
        roots,
        &[0; 32],
        ReconciliationListingRunner::new(CommandOutput {
            success: true,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: Some(0),
        }),
        None,
    )
    .unwrap();

    executor.runtime_reconcile_installation(&identity).unwrap();
    executor.runtime_reconcile_installation(&identity).unwrap();
    executor
        .refuse_reconciled_runtime(&identity.installation_id.to_string())
        .unwrap();
    assert!(!receipt_path.exists());
}

#[test]
fn reconciliation_refuses_active_unknown_failed_or_truncated_runtime_inventory() {
    for case in 0..5 {
        let (_temp, roots, identity, runtime_cache, _shared_cache) =
            helper_reconciliation_fixture();
        let stdout = match case {
            0 => format!(
                "{}\trunning\tvonk-{}\ttrue\t{}\n",
                "1".repeat(64),
                RUN_ID,
                identity.installation_id
            )
            .into_bytes(),
            1 => format!("{}\texited\tvonk-old-{}\ttrue\t\n", "2".repeat(64), RUN_ID).into_bytes(),
            2 => format!("{}\texited\tvonk-legacy-{}\t\t\n", "3".repeat(64), RUN_ID).into_bytes(),
            3 => Vec::new(),
            _ => vec![b'x'; MAX_COMMAND_OUTPUT_BYTES as usize + 1],
        };
        let response = CommandOutput {
            success: case != 3,
            stdout,
            stderr: if case == 3 {
                b"docker inventory unavailable".to_vec()
            } else {
                Vec::new()
            },
            exit_code: Some(if case == 3 { 1 } else { 0 }),
        };
        let receipt_path = roots
            .data
            .join(INSTALLATION_RECONCILIATION_DIRECTORY)
            .join(format!("{}.json", identity.installation_id));
        let executor = OperationExecutor::new(
            roots.clone(),
            &[0; 32],
            ReconciliationListingRunner::new(response),
            None,
        )
        .unwrap();

        assert!(executor.runtime_reconcile_installation(&identity).is_err());
        assert!(runtime_cache.exists());
        assert!(!receipt_path.exists());
        let fresh = OperationExecutor::new(
            roots,
            &[0; 32],
            ReconciliationListingRunner::new(docker_output(true, "", 0)),
            None,
        )
        .unwrap();
        fresh.runtime_reconcile_installation(&identity).unwrap();
        assert!(!runtime_cache.exists());
    }
}

#[test]
fn reconciliation_does_not_delete_unbound_containers() {
    let (_temp, roots, identity, runtime_cache, _shared_cache) = helper_reconciliation_fixture();
    let unbound = format!("{}\texited\tvonk-{}\ttrue\t\n", "a".repeat(64), RUN_ID);
    let runner = ReconciliationListingRunner::new(CommandOutput {
        success: true,
        stdout: unbound.into_bytes(),
        stderr: Vec::new(),
        exit_code: Some(0),
    });
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
    assert!(executor.runtime_reconcile_installation(&identity).is_err());
    assert!(runtime_cache.exists());
    assert_eq!(runner.calls.lock().unwrap().len(), 1);
    let repaired = ReconciliationListingRunner::new(CommandOutput {
        success: true,
        stdout: Vec::new(),
        stderr: Vec::new(),
        exit_code: Some(0),
    });
    let fresh = OperationExecutor::new(roots, &[0; 32], repaired, None).unwrap();
    fresh.runtime_reconcile_installation(&identity).unwrap();
    assert!(!runtime_cache.exists());
}

#[test]
fn reconciliation_lock_defers_cleanup_and_completed_history_admits_fresh_work() {
    let (_temp, roots, identity, runtime_cache, _shared_cache) = helper_reconciliation_fixture();
    let empty_listing = || {
        ReconciliationListingRunner::new(CommandOutput {
            success: true,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: Some(0),
        })
    };
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], empty_listing(), None).unwrap();
    let installation_id = identity.installation_id.to_string();

    // START holds this exact per-installation runtime fence through its
    // Docker call. A cleanup request arriving during that critical section
    // must be deferred without removing the cache or recording a receipt.
    let start_guard = executor
        .lock_installation_runtime(&installation_id)
        .unwrap();
    assert!(executor.runtime_reconcile_installation(&identity).is_err());
    assert!(runtime_cache.exists());
    drop(start_guard);

    executor.runtime_reconcile_installation(&identity).unwrap();
    assert!(!runtime_cache.exists());

    // Completed history never vetoes a fresh signed START. Actual launch
    // contracts and generation fences remain its admission boundaries.
    executor
        .refuse_reconciled_runtime(&installation_id)
        .unwrap();
    let cache = roots
        .agent_data
        .join("installations")
        .join(&installation_id)
        .join("runtime-cache");
    fs::create_dir(&cache).unwrap();
    executor.runtime_reconcile_installation(&identity).unwrap();
    assert!(!cache.exists());
}

#[test]
fn installation_cleanup_rejects_symlinked_installation_path_components() {
    let installation_id = "10000000-0000-4000-8000-000000000001";
    for linked_component in ["installations", "installation", "runtime-cache"] {
        let temp = tempfile::tempdir().unwrap();
        let roots = ManagedRoots::under(&temp.path().join("agent-data"));
        let outside = temp.path().join("outside");
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("sentinel"), b"outside").unwrap();
        fs::create_dir_all(&roots.agent_data).unwrap();

        let installations = roots.agent_data.join("installations");
        if linked_component == "installations" {
            std::os::unix::fs::symlink(&outside, &installations).unwrap();
        } else {
            fs::create_dir(&installations).unwrap();
            let installation = installations.join(installation_id);
            if linked_component == "installation" {
                std::os::unix::fs::symlink(&outside, &installation).unwrap();
            } else {
                fs::create_dir(&installation).unwrap();
                std::os::unix::fs::symlink(&outside, installation.join("runtime-cache")).unwrap();
            }
        }

        let executor =
            OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
        assert!(
            executor
                .runtime_installation_cleanup(installation_id)
                .is_err()
        );
        assert_eq!(fs::read(outside.join("sentinel")).unwrap(), b"outside");
    }
}

#[test]
fn damaged_cleanup_receipt_is_a_miss_and_new_cleanup_repairs_it() {
    let (_temp, roots, identity, runtime_cache, shared_cache) = helper_reconciliation_fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        &[0; 32],
        ReconciliationListingRunner::new(CommandOutput {
            success: true,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: Some(0),
        }),
        None,
    )
    .unwrap();
    executor.runtime_reconcile_installation(&identity).unwrap();
    let receipt = roots
        .data
        .join(INSTALLATION_RECONCILIATION_DIRECTORY)
        .join(format!("{}.json", identity.installation_id));
    fs::write(&receipt, b"damaged").unwrap();
    fs::create_dir(&runtime_cache).unwrap();
    executor.runtime_reconcile_installation(&identity).unwrap();
    assert!(!runtime_cache.exists());
    assert_eq!(fs::read(shared_cache).unwrap(), b"shared model cache");
    executor
        .refuse_reconciled_runtime(&identity.installation_id.to_string())
        .unwrap();
    assert!(!receipt.exists());
}

#[test]
fn uncertain_replacement_is_preserved_and_does_not_gate_fresh_work() {
    // Wrong implementation: either delete a replacement under old custody or
    // retain the old receipt as a veto on every later signed START.
    let (_temp, roots, identity, _runtime_cache, _shared_cache) = helper_reconciliation_fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        &[0; 32],
        ReconciliationListingRunner::new(CommandOutput {
            success: true,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: Some(0),
        }),
        None,
    )
    .unwrap();
    executor.runtime_reconcile_installation(&identity).unwrap();
    let installation = roots
        .agent_data
        .join("installations")
        .join(identity.installation_id.to_string());
    let original = installation.with_extension("original");
    fs::rename(&installation, &original).unwrap();
    fs::create_dir(&installation).unwrap();
    fs::set_permissions(&installation, fs::Permissions::from_mode(0o700)).unwrap();
    fs::create_dir(installation.join("runtime-cache")).unwrap();
    let sentinel = installation.join("runtime-cache/replacement");
    fs::write(&sentinel, b"unproven replacement").unwrap();
    assert!(executor.runtime_reconcile_installation(&identity).is_err());
    assert_eq!(fs::read(&sentinel).unwrap(), b"unproven replacement");
    executor
        .refuse_reconciled_runtime(&identity.installation_id.to_string())
        .unwrap();
    assert_eq!(fs::read(&sentinel).unwrap(), b"unproven replacement");
    assert!(original.is_dir());
}

mod independent_history;
