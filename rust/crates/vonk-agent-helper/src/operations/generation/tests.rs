#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn job_cancellation_fence_tracks_only_the_exact_runtime_generation() {
    let fence = JobCancellationFence::default();
    let identity = runtime_effect_identity(1);
    let active = fence.begin(identity).unwrap();
    assert!(fence.is_active(identity).unwrap());
    fence.cancel(identity).unwrap();
    assert!(fence.was_cancelled(identity).unwrap());
    assert!(fence.begin(identity).is_err());
    drop(active);
    assert!(!fence.is_active(identity).unwrap());
    assert!(!fence.was_cancelled(identity).unwrap());
}

#[test]
fn start_authority_binds_generation_plan_identity_and_projected_arguments() {
    let temp = tempfile::tempdir().unwrap();
    let executor = OperationExecutor::new(
        ManagedRoots::under(temp.path()),
        &[0; 32],
        MissingContainerRunner,
        None,
    )
    .unwrap();
    let start_plan = recipe_start_plan_for_authority(i64::MAX as u64);
    let arguments = executor
        .projected_runtime_arguments(
            &start_plan.compiled_execution_plan,
            start_plan.installation_id,
            start_plan.run_id,
        )
        .unwrap();
    let fence = uuid::Uuid::new_v4();
    let request = runtime_request_identity(
        &fence,
        RuntimeRequestTestParts {
            action: HostRuntimeAction::Start,
            arguments,
            run_generation: start_plan.run_generation,
            start_plan: Some(start_plan.clone()),
            stop_plan: None,
        },
    );
    let plan_sha256 = hex_sha256(&canonical_json(&start_plan).unwrap());
    let grant = RuntimeRequestGrantBinding {
        fence: &fence,
        installation_intent_nonce: None,
        installation_intent_ordinal: Some(1),
        installation_id: None,
        reconciliation_identity: None,
        start_plan_sha256: Some(&plan_sha256),
        stop_plan_sha256: None,
        run_generation: Some(start_plan.run_generation),
        runtime_run_id: Some(&start_plan.run_id),
        runtime_target_id: Some(&start_plan.run_id),
        runtime_installation_id: Some(&start_plan.installation_id),
    };
    let authorized = executor
        .authorize_runtime_effect(&request, grant)
        .unwrap()
        .unwrap();
    assert!(matches!(
        authorized,
        AuthorizedRuntimeEffect::Start {
            identity: RuntimeEffectIdentity {
                runtime_id,
                installation_id,
                run_generation,
            },
            logical_run_id,
            plan_digest,
            ..
        } if run_generation == i64::MAX as u64 && runtime_id == start_plan.run_id
            && installation_id == start_plan.installation_id
            && logical_run_id == start_plan.run_id
            && plan_digest == start_plan.plan_digest
    ));

    let mut caller_argv = request.clone();
    caller_argv.arguments.push("--privileged".to_owned());
    assert!(matches!(
        executor.authorize_runtime_effect(&caller_argv, grant),
        Err(OperationError::InvalidOperation)
    ));

    let mut stale_grant = request.clone();
    stale_grant.run_generation = Some(2);
    assert!(matches!(
        executor.authorize_runtime_effect(&stale_grant, grant),
        Err(OperationError::InvalidOperation)
    ));

    let mut mutated_plan = request.clone();
    mutated_plan.start_plan.as_mut().unwrap().plan_digest = "d".repeat(64);
    assert!(matches!(
        executor.authorize_runtime_effect(&mutated_plan, grant),
        Err(OperationError::InvalidOperation)
    ));
}

#[test]
fn stop_authority_binds_exact_target_node_plan_and_cancellation_semantics() {
    let temp = tempfile::tempdir().unwrap();
    let executor = OperationExecutor::new(
        ManagedRoots::under(temp.path()),
        &[0; 32],
        MissingContainerRunner,
        None,
    )
    .unwrap();
    let stop_plan = recipe_stop_plan_for_authority(1, true);
    let fence = uuid::Uuid::new_v4();
    let request = runtime_request_identity(
        &fence,
        RuntimeRequestTestParts {
            action: HostRuntimeAction::Stop,
            arguments: Vec::new(),
            run_generation: stop_plan.run_generation,
            start_plan: None,
            stop_plan: Some(stop_plan.clone()),
        },
    );
    let plan_sha256 = hex_sha256(&canonical_json(&stop_plan).unwrap());
    let grant = RuntimeRequestGrantBinding {
        fence: &fence,
        installation_intent_nonce: None,
        installation_intent_ordinal: Some(1),
        installation_id: None,
        reconciliation_identity: None,
        start_plan_sha256: None,
        stop_plan_sha256: Some(&plan_sha256),
        run_generation: Some(stop_plan.run_generation),
        runtime_run_id: Some(&stop_plan.run_id),
        runtime_target_id: Some(&stop_plan.target_runtime_id),
        runtime_installation_id: Some(&stop_plan.installation_id),
    };
    let authorized = executor
        .authorize_runtime_effect(&request, grant)
        .unwrap()
        .unwrap();
    assert!(matches!(
        authorized,
        AuthorizedRuntimeEffect::Stop {
            identity: RuntimeEffectIdentity {
                runtime_id,
                installation_id,
                run_generation: 1,
            },
            logical_run_id,
            cancel_pending_start: true,
            ..
        } if runtime_id == stop_plan.target_runtime_id
            && installation_id == stop_plan.installation_id
            && logical_run_id == stop_plan.run_id
    ));

    // Every identity and the cleanup timeout remain covered by the signed
    // Stop hash even though cleanup does not require launch history.
    let mutations: [fn(&mut RecipeStopPayload); 4] = [
        |plan: &mut RecipeStopPayload| plan.rank = 1_u64.into(),
        |plan: &mut RecipeStopPayload| plan.role = "other".to_owned(),
        |plan: &mut RecipeStopPayload| plan.recipe_content_sha256 = "d".repeat(64),
        |plan: &mut RecipeStopPayload| plan.stop_timeout_seconds += 1,
    ];
    for mutate in mutations {
        let mut changed = request.clone();
        mutate(changed.stop_plan.as_mut().unwrap());
        assert!(matches!(
            executor.authorize_runtime_effect(&changed, grant),
            Err(OperationError::InvalidOperation)
        ));
    }

    let mut empty_argv = request.clone();
    empty_argv.arguments.push("ignored-argv".to_owned());
    assert!(matches!(
        executor.authorize_runtime_effect(&empty_argv, grant),
        Err(OperationError::InvalidOperation)
    ));

    let mut different_generation = request.clone();
    different_generation.run_generation = Some(2);
    assert!(matches!(
        executor.authorize_runtime_effect(&different_generation, grant),
        Err(OperationError::InvalidOperation)
    ));

    let mut different_target = request.clone();
    different_target
        .stop_plan
        .as_mut()
        .unwrap()
        .target_runtime_id = uuid::Uuid::new_v4();
    assert!(matches!(
        executor.authorize_runtime_effect(&different_target, grant),
        Err(OperationError::InvalidOperation)
    ));
}

#[test]
fn cancelled_generation_fence_survives_helper_restart_and_allows_newer_start() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let first_helper =
        OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
    let old = runtime_effect_identity(1);

    first_helper
        .update_runtime_generation_fence(
            &old,
            RuntimeGenerationFenceUse::Stop {
                cancel_pending_start: true,
            },
        )
        .expect("Stop(true) durably cancels its generation before checking effects");
    assert!(
        temp.path()
            .join(RUNTIME_GENERATION_FENCE_DIRECTORY)
            .is_dir()
    );
    let stored = first_helper
        .read_runtime_generation_fence(old.installation_id, old.runtime_id)
        .unwrap()
        .unwrap();
    assert_eq!(stored.highest_generation, old.run_generation);
    assert!(stored.cancelled);

    let restarted_helper =
        OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
    assert!(matches!(
        restarted_helper.update_runtime_generation_fence(&old, RuntimeGenerationFenceUse::Start),
        Err(OperationError::InvalidOperation)
    ));
    let current = RuntimeEffectIdentity {
        run_generation: 2,
        ..old
    };
    restarted_helper
        .update_runtime_generation_fence(&current, RuntimeGenerationFenceUse::Start)
        .expect("a newer authorized generation replaces the cancellation fence");
    let stored = restarted_helper
        .read_runtime_generation_fence(current.installation_id, current.runtime_id)
        .unwrap()
        .unwrap();
    assert_eq!(stored.highest_generation, 2);
    assert!(!stored.cancelled);
}

#[test]
fn full_controller_generation_fence_survives_restart_without_truncation() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let helper =
        OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
    let identity = runtime_effect_identity(i64::MAX as u64);
    helper
        .update_runtime_generation_fence(
            &identity,
            RuntimeGenerationFenceUse::Stop {
                cancel_pending_start: true,
            },
        )
        .unwrap();
    let restarted = OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
    let stored = restarted
        .read_runtime_generation_fence(identity.installation_id, identity.runtime_id)
        .unwrap()
        .unwrap();
    assert_eq!(stored.highest_generation, i64::MAX as u64);
    assert!(stored.cancelled);
    assert!(matches!(
        restarted.update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start),
        Err(OperationError::InvalidOperation)
    ));
    let stale = RuntimeEffectIdentity {
        run_generation: u64::from(u32::MAX) + 1,
        ..identity
    };
    assert!(matches!(
        restarted.update_runtime_generation_fence(&stale, RuntimeGenerationFenceUse::Start),
        Err(OperationError::InvalidOperation)
    ));
    let invalid = RuntimeEffectIdentity {
        run_generation: i64::MAX as u64 + 1,
        ..identity
    };
    assert!(matches!(
        restarted.update_runtime_generation_fence(&invalid, RuntimeGenerationFenceUse::Start),
        Err(OperationError::InvalidOperation)
    ));
}

#[test]
fn ordinary_stop_keeps_same_generation_retry_available_after_restart() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let first_helper =
        OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
    let identity = runtime_effect_identity(1);

    first_helper
        .update_runtime_generation_fence(
            &identity,
            RuntimeGenerationFenceUse::Stop {
                cancel_pending_start: false,
            },
        )
        .unwrap();
    let restarted_helper =
        OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
    restarted_helper
        .update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start)
        .expect("ordinary Stop must not cancel a retry in its authorized generation");
    let stored = restarted_helper
        .read_runtime_generation_fence(identity.installation_id, identity.runtime_id)
        .unwrap()
        .unwrap();
    assert_eq!(stored.highest_generation, identity.run_generation);
    assert!(!stored.cancelled);
}

#[test]
fn newer_generation_survives_failed_start_and_old_exact_stop_without_rewinding_fence() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let old = runtime_effect_identity(1);
    let current = runtime_effect_identity(2);
    let old_plan_digest = "a".repeat(64);
    let current_plan_digest = "b".repeat(64);
    let runner = ExistingRuntimeGenerationRunner {
        logical_run_id: old.runtime_id,
        target_id: old.runtime_id,
        installation_id: old.installation_id,
        run_generation: old.run_generation,
        plan_digest: old_plan_digest.clone(),
        calls: Arc::default(),
    };
    let calls = runner.calls.clone();
    let executor = OperationExecutor::new(roots, &[0; 32], runner, None).unwrap();
    let nonce =
        match executor.accept_installation_intent(current.installation_id, None, Some(1), false) {
            Err(OperationError::InstallationIntentObservationRequired { nonce }) => nonce,
            _ => panic!("current intent challenge was not issued"),
        };
    // A new authorized Start reserves its generation before validating or
    // invoking Docker. Force it to fail at the malformed launch boundary,
    // then prove the old named container is still rejected as generation
    // 1 and can only be removed by its own exact Stop.
    assert!(matches!(
        executor.runtime_start_authorized(
            &[],
            current,
            current.runtime_id,
            &current_plan_digest,
            &format!("sha256:{}", "c".repeat(64)),
            &RuntimeRequestGrantBinding {
                fence: &uuid::Uuid::new_v4(),
                installation_intent_nonce: Some(&nonce),
                installation_intent_ordinal: Some(1),
                installation_id: None,
                reconciliation_identity: None,
                start_plan_sha256: None,
                stop_plan_sha256: None,
                run_generation: None,
                runtime_run_id: None,
                runtime_target_id: None,
                runtime_installation_id: None,
            },
        ),
        Err(OperationError::InvalidOperation)
    ));

    // The old container cannot be mistaken for generation 2 or removed
    // by a Stop for that generation.
    assert!(matches!(
        executor.runtime_stop_once(current, current.runtime_id, &current_plan_digest, 1,),
        Err(OperationError::InvalidArtifact)
    ));
    executor
        .runtime_stop_authorized(old, old.runtime_id, &old_plan_digest, 1, true)
        .expect("a stale exact Stop may clean its own generation");
    let recorded_calls = calls.lock().unwrap();
    assert!(
        recorded_calls
            .iter()
            .any(|call| call.first().map(String::as_str) == Some("stop"))
    );
    assert!(
        recorded_calls
            .iter()
            .any(|call| call.first().map(String::as_str) == Some("rm"))
    );
    drop(recorded_calls);
    let stored = executor
        .read_runtime_generation_fence(current.installation_id, current.runtime_id)
        .unwrap()
        .unwrap();
    assert_eq!(stored.highest_generation, 2);
    assert!(!stored.cancelled);
    assert!(matches!(
        executor.update_runtime_generation_fence(&old, RuntimeGenerationFenceUse::Start),
        Err(OperationError::InvalidOperation)
    ));
    executor
        .update_runtime_generation_fence(&current, RuntimeGenerationFenceUse::Start)
        .expect("the newer generation remains eligible for retry");
}

#[test]
fn stop_deadline_never_reports_success_while_a_slow_start_remains_active() {
    let fence = JobCancellationFence::default();
    let identity = runtime_effect_identity(1);
    let _active = fence.begin(identity).unwrap();
    fence.cancel(identity).unwrap();

    assert!(matches!(
        fence.wait_for_active_start(identity, Instant::now()),
        Err(OperationError::StopUncertain)
    ));
}

#[test]
fn exact_cancel_stop_waits_for_active_start_then_blocks_late_start() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    let executor =
        OperationExecutor::new(roots.clone(), &[0; 32], MissingContainerRunner, None).unwrap();
    let identity = runtime_effect_identity(1);
    executor
        .update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start)
        .unwrap();
    let active = executor.job_cancellation.begin(identity).unwrap();
    std::thread::scope(|scope| {
        let stop = scope.spawn(|| {
            executor.runtime_stop_authorized(
                identity,
                identity.runtime_id,
                &"a".repeat(64),
                1,
                true,
            )
        });
        while !executor.job_cancellation.was_cancelled(identity).unwrap() {
            std::thread::yield_now();
        }
        assert!(!stop.is_finished(), "stop acknowledged an active START");
        drop(active);
        stop.join().unwrap().unwrap();
    });
    let restarted_helper =
        OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
    assert!(matches!(
        restarted_helper
            .update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start),
        Err(OperationError::InvalidOperation)
    ));
}

#[test]
fn ordinary_recovery_stop_does_not_fence_a_same_run_restart() {
    let temp = tempfile::tempdir().unwrap();
    let executor = OperationExecutor::new(
        ManagedRoots::under(temp.path()),
        &[0; 32],
        MissingContainerRunner,
        None,
    )
    .unwrap();

    let identity = runtime_effect_identity(1);
    executor
        .runtime_stop_authorized(identity, identity.runtime_id, &"a".repeat(64), 1, false)
        .unwrap();
    executor
        .update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start)
        .expect("ordinary Stop does not cancel a same-generation retry");
}
