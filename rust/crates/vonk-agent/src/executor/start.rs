//! Start.

use super::*;

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_start(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
        request: vonk_agent_protocol::RecipeStartRequest,
    ) -> ExecutionResult {
        let preload_diagnostics = self.runtime.report_preload_memory(
            request.placement().reserved_memory_bytes,
            Path::new("/proc/meminfo"),
        );
        self.report_phase(claim, ProgressPhase::Starting).await;
        let installation_id = request.installation_id.to_string();
        let phase_deadline = match request
            .start_deadline
            .as_deref()
            .map(DateTime::parse_from_rfc3339)
            .transpose()
        {
            Ok(deadline) => deadline,
            Err(_) => return failed("recipe start deadline is invalid"),
        };
        let spec = request.compiled_execution_plan.clone();
        if spec.validate().is_err() {
            return failed("compiled execution plan is invalid");
        }
        let Some(endpoint) = spec.endpoint.as_ref() else {
            return failed("installed recipe is not a persistent service");
        };
        let (Some(endpoint_address), Some(endpoint_port)) =
            (request.endpoint_address(), request.port())
        else {
            return failed("start plan has no serving address");
        };
        if *cancellation.borrow() {
            return cancelled("controller cancelled before installation preparation");
        }
        if let Err(result) = self
            .prepare_installation(
                claim,
                &spec,
                &installation_id,
                &lease_deadline,
                &cancellation,
            )
            .await
        {
            return result;
        }
        if !self
            .runtime
            .load_spec(&installation_id)
            .is_ok_and(|installed| same_installed_workload(&installed, &spec))
        {
            return temporary_runtime_observation_failure();
        }
        let placement = spec.runtime.placement.clone();
        let run_id = request.run_id.to_string();
        let inspection_identity = Some(RecipeRunStartIdentity {
            run_generation: request.run_generation,
        });
        let collective_readiness =
            matches!(request.phase, Some(RecipeStartPhase::CollectiveReadiness));
        let rank_launch = matches!(request.phase, Some(RecipeStartPhase::RankLaunch));
        if request.phase.is_some()
            && !before_phase_deadline(&lease_deadline, phase_deadline.as_ref())
        {
            return failed("distributed start deadline elapsed before execution");
        }
        // A previous agent may have completed the Docker start before
        // its result was acknowledged. Retained lifecycle identity is
        // read without resetting writable state.
        let retained_plan = if collective_readiness {
            None
        } else {
            match self.runtime.prepare_retained_start_if_present(
                &spec,
                &installation_id,
                &run_id,
                &placement,
                inspection_identity.as_ref(),
            ) {
                Ok(plan) => plan,
                Err(crate::oci::OciError::Io(error))
                    if error.kind() != std::io::ErrorKind::PermissionDenied =>
                {
                    return temporary_runtime_observation_failure();
                }
                Err(crate::oci::OciError::ReconciliationBusy) => {
                    return temporary_runtime_observation_failure();
                }
                Err(_) => {
                    // The agent's own record of this run is not this
                    // start's (another generation, another identity,
                    // unreadable). Remove what is proven this order's,
                    // clear the record and start fresh; a container
                    // that is not proven is refused, never touched.
                    if let Err(result) = self
                        .heal_retained_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                        .await
                    {
                        return result;
                    }
                    None
                }
            }
        };
        let retained_existing = retained_plan.is_some();
        let plan = if let Some(plan) = retained_plan {
            plan
        } else if collective_readiness {
            match inspection_identity.as_ref().map_or_else(
                || {
                    self.runtime.prepare_retained_start(
                        &spec,
                        &installation_id,
                        &run_id,
                        &placement,
                    )
                },
                |identity| {
                    self.runtime
                        .prepare_retained_start_with_inspection_identity(
                            &spec,
                            &installation_id,
                            &run_id,
                            &placement,
                            identity,
                        )
                },
            ) {
                Ok(plan) => plan,
                Err(_) => {
                    return temporary_runtime_observation_failure();
                }
            }
        } else {
            // Kit peak is an estimate, never an admission veto. The host
            // guard observes actual wedge precursors independently.
            match inspection_identity.as_ref().map_or_else(
                || {
                    self.runtime
                        .prepare_start(&spec, &installation_id, &run_id, &placement)
                },
                |identity| {
                    self.runtime.prepare_start_with_inspection_identity(
                        &spec,
                        &installation_id,
                        &run_id,
                        &placement,
                        identity,
                    )
                },
            ) {
                Ok(plan) => plan,
                Err(error) => {
                    return runtime_preparation_failure(&error);
                }
            }
        };
        if *cancellation.borrow() {
            return self
                .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                .await;
        }
        let arguments = runtime_arguments_for_plan(&plan, &plan.main);
        let runtime_guard_arguments = arguments.clone();
        let mut acl_transition = if collective_readiness || retained_existing {
            None
        } else {
            match self
                .runtime
                .begin_installation_acl_transition(&installation_id)
            {
                Ok(transition) => Some(transition),
                Err(_) => {
                    return temporary_runtime_observation_failure();
                }
            }
        };
        let mut cancellation_observer = cancellation.clone();
        let runtime_action = if collective_readiness || retained_existing {
            HostRuntimeAction::RunInspect
        } else {
            HostRuntimeAction::Start
        };
        let runtime_arguments = if collective_readiness || retained_existing {
            runtime_guard_arguments.clone()
        } else {
            arguments
        };
        let mut runtime_result = run_until_cancelled(
            async {
                if runtime_action == HostRuntimeAction::Start {
                    self.execute_host_runtime_plan(
                        claim,
                        runtime_arguments,
                        HostRuntimePlan::Start(request.clone()),
                    )
                    .await
                } else {
                    self.execute_host_runtime(claim, runtime_action, runtime_arguments)
                        .await
                }
            },
            &mut cancellation_observer,
        )
        .await;
        if retained_existing
            && matches!(
                &runtime_result,
                Some(Err(crate::host_runtime::HostRuntimeError::HelperRejected { code, .. }))
                    if *code == HelperErrorCode::RuntimeRunMissing
            )
        {
            // The retained plan and an independent Docker listing prove
            // this exact run never reached a running container.
            // Kit peak is an estimate, never an admission veto. The host
            // guard observes actual wedge precursors independently.
            acl_transition = match self
                .runtime
                .begin_installation_acl_transition(&installation_id)
            {
                Ok(transition) => Some(transition),
                Err(_) => {
                    return temporary_runtime_observation_failure();
                }
            };
            runtime_result = run_until_cancelled(
                self.execute_host_runtime_plan(
                    claim,
                    runtime_guard_arguments.clone(),
                    HostRuntimePlan::Start(request.clone()),
                ),
                &mut cancellation_observer,
            )
            .await;
        }
        match runtime_result {
            None => {
                let stopped = self
                    .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                    .await;
                if let Some(transition) = acl_transition.take()
                    && let Err(error) = self
                        .settle_acl_transition(&installation_id, &transition)
                        .await
                {
                    return unconfirmed(
                        WaitReason::ModelCustodyUnconfirmed,
                        "cancelled workload model custody remains unconfirmed",
                        UnknownEvidence::at(FailureStage::ModelCustody)
                            .because(error.safe_category()),
                    );
                }
                return stopped;
            }
            Some(Err(error)) => {
                if *cancellation.borrow() {
                    let stopped = self
                        .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                        .await;
                    if let Some(transition) = acl_transition.take()
                        && let Err(error) = self
                            .settle_acl_transition(&installation_id, &transition)
                            .await
                    {
                        return unconfirmed(
                            WaitReason::ModelCustodyUnconfirmed,
                            "cancelled workload model custody remains unconfirmed",
                            UnknownEvidence::at(FailureStage::ModelCustody)
                                .because(error.safe_category()),
                        );
                    }
                    return stopped;
                }
                if retained_existing {
                    if temporary_observation_error(&error) {
                        return temporary_runtime_observation_failure();
                    }
                    // The helper found the exact-name container is not
                    // this start's. Remove it only when the exact stop
                    // proves it this order's, then re-issue; refuse
                    // (untouched) when it is not. Any other failure is
                    // reported with the helper's evidence.
                    if matches!(
                        &error,
                        crate::host_runtime::HostRuntimeError::HelperRejected { code, .. }
                            if *code == HelperErrorCode::OperationInvalidArtifact
                    ) {
                        return match self
                            .heal_retained_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                            .await
                        {
                            Ok(()) => retained_container_removed(&run_id),
                            Err(result) => result,
                        };
                    }
                    return runtime_observation_failure(&error);
                }
                if !collective_readiness
                    && let Err(uncertain) = self
                        .stop_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds, false)
                        .await
                {
                    return uncertain;
                }
                return runtime_failure(
                    if collective_readiness {
                        "collective workload is not running with exact identity"
                    } else {
                        "container runtime could not start the workload"
                    },
                    &error,
                );
            }
            Some(Ok(())) => {}
        }
        if let Some(transition) = acl_transition.take()
            && self
                .settle_acl_transition(&installation_id, &transition)
                .await
                .is_err()
        {
            // An ACL/receipt observation gap is not evidence that this exact
            // running workload must be stopped. Reobserve the retained run.
            return temporary_runtime_observation_failure();
        }
        if *cancellation.borrow() {
            return self
                .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                .await;
        }
        if rank_launch {
            let first_inspect = self
                .execute_host_runtime(
                    claim,
                    HostRuntimeAction::RunInspect,
                    runtime_guard_arguments.clone(),
                )
                .await;
            let mut launch_failure = first_inspect.err();
            let stable = if launch_failure.is_some()
                || *cancellation.borrow()
                || !before_phase_deadline(&lease_deadline, phase_deadline.as_ref())
            {
                false
            } else if wait_for_launch_stability(
                lease_deadline.clone(),
                cancellation.clone(),
                phase_deadline,
                Duration::from_secs(2),
            )
            .await
            {
                launch_failure = self
                    .execute_host_runtime(
                        claim,
                        HostRuntimeAction::RunInspect,
                        runtime_guard_arguments.clone(),
                    )
                    .await
                    .err();
                launch_failure.is_none()
                    && before_phase_deadline(&lease_deadline, phase_deadline.as_ref())
            } else {
                false
            };
            if !stable {
                if *cancellation.borrow() {
                    return self
                        .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                        .await;
                }
                // Read before the stop removes the container.
                let unstable_evidence = if launch_failure.is_none() {
                    Some(
                        self.running_workload_evidence(
                            &runtime_guard_arguments,
                            "rank_stability_deadline=true",
                        )
                        .await,
                    )
                } else {
                    None
                };
                if let Err(uncertain) = self
                    .stop_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds, false)
                    .await
                {
                    return uncertain;
                }
                return match (launch_failure, unstable_evidence) {
                    (Some(error), _) => {
                        runtime_failure("rank process did not remain stable after launch", &error)
                    }
                    (None, Some(evidence)) => failed_with_evidence(
                        "rank process did not remain stable after launch",
                        evidence,
                    ),
                    (None, None) => failed("rank process did not remain stable after launch"),
                };
            }
            let mut result = recipe_start_result(&request);
            result.preload_diagnostics = Some(preload_diagnostics.clone());
            let success = ExecutionResult::done(result);
            if *cancellation.borrow() {
                return self
                    .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                    .await;
            }
            return success;
        }
        let runtime_guard = async {
            loop {
                tokio::time::sleep(Duration::from_secs(10)).await;
                self.execute_host_runtime(
                    claim,
                    HostRuntimeAction::RunInspect,
                    runtime_guard_arguments.clone(),
                )
                .await?;
            }
        };
        let ready = match if collective_readiness {
            wait_ready_with_runtime_guard_and_cancellation(
                wait_ready_until(
                    endpoint_address,
                    endpoint_port,
                    &endpoint.health_path,
                    lease_deadline,
                    phase_deadline,
                ),
                runtime_guard,
                cancellation.clone(),
            )
            .await
        } else {
            wait_ready_with_runtime_guard_and_cancellation(
                wait_ready(
                    endpoint_address,
                    endpoint_port,
                    &endpoint.health_path,
                    lease_deadline,
                ),
                runtime_guard,
                cancellation.clone(),
            )
            .await
        } {
            ReadinessOutcome::Ready => true,
            ReadinessOutcome::Cancelled | ReadinessOutcome::Deadline => false,
            ReadinessOutcome::GuardFailed(error) => {
                // The runtime could not be inspected, so the effect cannot
                // be bound.  Name that instead of reporting a readiness
                // deadline the workload never reached: a temporary
                // inspection failure is the code the Controller already
                // retries for a start, so it becomes self-healing, and any
                // other rejection carries the captured container output
                // the inspection gate admits.
                if temporary_observation_error(&error) {
                    return temporary_runtime_observation_failure();
                }
                return runtime_observation_failure(&error);
            }
        };
        if !ready {
            if *cancellation.borrow() {
                return self
                    .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                    .await;
            }
            // Read before any stop removes the container: a workload
            // that never became ready leaves its own account of why.
            let evidence = self
                .running_workload_evidence(&runtime_guard_arguments, "readiness_deadline=true")
                .await;
            if !collective_readiness
                && let Err(uncertain) = self
                    .stop_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds, false)
                    .await
            {
                return uncertain;
            }
            return failed_with_evidence(
                "workload did not become ready before its deadline",
                evidence,
            );
        }
        let mut result = recipe_start_result(&request);
        result.preload_diagnostics = Some(preload_diagnostics.clone());
        let success = ExecutionResult::done(result);
        if *cancellation.borrow() {
            return self
                .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                .await;
        }
        success
    }
}
