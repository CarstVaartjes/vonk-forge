//! Recipe requests.

use super::*;

#[derive(Debug, Clone, PartialEq)]
pub enum RecipeOperationRequest {
    RuntimePreflight(runtime_preflight::RuntimePreflightRequest),
    Build(Box<RecipeBuildRequest>),
    BuildCleanup(RecipeBuildCleanupRequest),
    JobRun(RecipeJobRunRequest),
    Install(RecipeInstallRequest),
    Start(RecipeStartRequest),
    Stop(RecipeStopRequest),
    Uninstall(RecipeUninstallRequest),
    Reconcile(RecipeReconcileRequest),
}

impl RecipeJobRunResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        let manifest = generated::RecipeJobOutputManifestContent {
            schema_version: self.output_manifest.schema_version,
            total_bytes: self.output_manifest.total_bytes,
            files: self.output_manifest.files.clone(),
        };
        let valid = self
            .diagnostics
            .as_ref()
            .is_none_or(|value| value.validate().is_ok())
            && (0..=255).contains(&self.exit_code)
            && self.output_manifest.schema_version == 1
            && self.output_manifest.files.len() <= 32
            && self
                .output_manifest
                .files
                .windows(2)
                .all(|pair| pair[0].name < pair[1].name)
            && self.output_manifest.files.iter().all(|file| {
                valid_job_file_name(&file.name)
                    && valid_media_type(&file.media_type)
                    && file.size_bytes <= 1024 * 1024 * 1024
                    && lower_hex(&file.sha256, 64)
            })
            && self
                .output_manifest
                .files
                .iter()
                .try_fold(0_u64, |total, file| {
                    total.checked_add(u64::from(file.size_bytes))
                })
                == Some(u64::from(self.output_manifest.total_bytes))
            && self.output_manifest.total_bytes <= 2 * 1024 * 1024 * 1024
            && canonical_json(&manifest)
                .ok()
                .is_some_and(|bytes| hex_sha256(&bytes) == self.output_manifest.manifest_sha256)
            && self.evidence.elapsed_milliseconds <= 7 * 24 * 60 * 60 * 1000
            && self
                .evidence
                .peak_memory_bytes
                .is_none_or(|value| value <= 16 * 1024_u64.pow(4))
            && self.reason.as_ref().is_none_or(|reason| {
                !reason.is_empty() && reason.len() <= 512 && !reason.contains('\0')
            });
        if valid {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe job result"))
        }
    }
}

impl RecipeReconciliationIdentity {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if valid_reconciliation_identity(self) {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe reconciliation identity"))
        }
    }
}

impl RecipeOperationRequest {
    pub fn parse(claim: &AgentClaim) -> Result<Self, ProtocolError> {
        claim.validate()?;
        let request = match (&claim.operation, &claim.payload) {
            (
                generated::AgentOperation::RuntimePreflightV1,
                generated::AgentClaimPayload::RuntimePreflightRequest(value),
            ) => Self::RuntimePreflight(value.clone()),
            (
                generated::AgentOperation::RecipeBuildCleanupV1,
                generated::AgentClaimPayload::RecipeBuildCleanupRequest(value),
            ) => Self::BuildCleanup(value.clone()),
            (
                generated::AgentOperation::RecipeBuildV1,
                generated::AgentClaimPayload::RecipeBuildRequest(value),
            ) => Self::Build(Box::new(value.clone())),
            (
                generated::AgentOperation::RecipeJobRunV1,
                generated::AgentClaimPayload::RecipeJobRunRequest(value),
            ) => Self::JobRun(value.clone()),
            (
                generated::AgentOperation::RecipeInstall,
                generated::AgentClaimPayload::RecipeInstallPayload(value),
            ) => Self::Install(value.clone()),
            (
                generated::AgentOperation::RecipeStart,
                generated::AgentClaimPayload::RecipeStartPayload(value),
            ) => Self::Start(value.clone()),
            (
                generated::AgentOperation::RecipeStop,
                generated::AgentClaimPayload::RecipeStopPayload(value),
            ) => Self::Stop(value.clone()),
            (
                generated::AgentOperation::RecipeUninstall,
                generated::AgentClaimPayload::RecipeUninstallPayload(value),
            ) => Self::Uninstall(value.clone()),
            (
                generated::AgentOperation::RecipeReconcile,
                generated::AgentClaimPayload::RecipeReconcilePayload(value),
            ) => Self::Reconcile(value.clone()),
            _ => return Err(ProtocolError::Identity("recipe operation payload")),
        };
        request.validate()?;
        Ok(request)
    }

    pub(super) fn validate(&self) -> Result<(), ProtocolError> {
        let valid = match self {
            Self::RuntimePreflight(_) => true,
            Self::Build(value) => validate_build(value),
            Self::BuildCleanup(_) => true,
            Self::JobRun(value) => validate_recipe_job(value),
            Self::Install(value) => {
                lower_hex(&value.plan_digest, 64)
                    && value.expected_bytes <= 16 * 1024_u64.pow(4)
                    && valid_role(&value.compiled_execution_plan.runtime.placement.role)
            }
            Self::Start(value) => validate_recipe_start(value),
            Self::Stop(value) => validate_recipe_stop(value),
            Self::Uninstall(value) => {
                lower_hex(&value.plan_digest, 64)
                    && lower_hex(&value.recipe_content_sha256, 64)
                    && value
                        .cleanup_model_content_sha256
                        .as_ref()
                        .is_none_or(|digest| lower_hex(digest, 64))
            }
            Self::Reconcile(value) => {
                valid_reconciliation_identity(&RecipeReconciliationIdentity::from(value))
            }
        };
        if valid {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe payload"))
        }
    }
}

#[cfg(test)]
pub(super) mod recipe_start_tests {
    use super::*;

    fn start_payload(
        world_size: u32,
        rank: u32,
        local_address: Option<&str>,
        master_address: Option<&str>,
        phase: Option<&str>,
    ) -> Value {
        let mut plan: Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
        ))
        .unwrap();
        plan["runtime"]["placement"] = serde_json::json!({
            "endpoint_address": if world_size == 1 { Some("100.100.20.30") } else { None },
            "local_address": local_address,
            "master_address": master_address,
            "master_port": if world_size > 1 { Some(29500) } else { None },
            "memory_floor_bytes": 2 * 1024_u64.pow(3),
            "port": 8000,
            "rank": rank,
            "reserved_memory_bytes": 1024,
            "role": if rank == 0 { "entrypoint" } else { "worker" },
            "world_size": world_size,
        });
        let mut payload = serde_json::json!({
            "compiled_execution_plan": plan,
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "mapping_id": "00000000-0000-4000-8000-000000000002",
            "plan_digest": "b".repeat(64),
            "recipe_revision_id": "00000000-0000-4000-8000-000000000003",
            "run_generation": 1,
            "run_id": "00000000-0000-4000-8000-000000000004",
        });
        if let Some(phase) = phase {
            let document = payload.as_object_mut().unwrap();
            document.insert("phase".to_owned(), Value::String(phase.to_owned()));
            document.insert("run_generation".to_owned(), Value::from(1));
            document.insert(
                "start_deadline".to_owned(),
                Value::String("2026-09-01T12:00:00+00:00".to_owned()),
            );
        }
        payload
    }

    fn claim(payload: Value) -> Result<AgentClaim, ProtocolError> {
        claim_for(generated::AgentOperation::RecipeStart, payload)
    }

    fn claim_for(
        operation: generated::AgentOperation,
        payload: Value,
    ) -> Result<AgentClaim, ProtocolError> {
        let payload: generated::AgentClaimPayload = serde_json::from_value(payload)?;
        Ok(AgentClaim {
            observation_budget_seconds: 3600,
            deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
            fence: Uuid::parse_str("00000000-0000-4000-8000-000000000005").unwrap(),
            operation: operation.parse().unwrap(),
            payload,
        })
    }

    fn parsed_start(payload: Value) -> Result<RecipeStartRequest, ProtocolError> {
        match RecipeOperationRequest::parse(&claim(payload)?)? {
            RecipeOperationRequest::Start(request) => Ok(request),
            _ => unreachable!(),
        }
    }

    pub(crate) fn valid_start_plan() -> RecipeStartRequest {
        parsed_start(start_payload(1, 0, None, None, None)).unwrap()
    }

    fn stop_payload() -> Value {
        let start = start_payload(1, 0, None, None, None);
        serde_json::json!({
            "run_id": start["run_id"],
            "target_runtime_id": start["run_id"],
            "run_generation": 1,
            "installation_id": start["installation_id"],
            "recipe_revision_id": start["recipe_revision_id"],
            "mapping_id": start["mapping_id"],
            "plan_digest": start["plan_digest"],
            "rank": start["compiled_execution_plan"]["runtime"]["placement"]["rank"],
            "role": start["compiled_execution_plan"]["runtime"]["placement"]["role"],
            "recipe_content_sha256": start["compiled_execution_plan"]["identity"]["recipe_revision_sha256"],
            "stop_timeout_seconds": start["compiled_execution_plan"]["lifecycle"]["stop_timeout_seconds"],
            "cancel_pending_start": false,
        })
    }

    fn parsed_stop(payload: Value) -> Result<RecipeStopRequest, ProtocolError> {
        let claim = claim_for(generated::AgentOperation::RecipeStop, payload)?;
        match RecipeOperationRequest::parse(&claim)? {
            RecipeOperationRequest::Stop(request) => Ok(request),
            _ => unreachable!(),
        }
    }

    #[test]
    fn schema_two_start_payload_allows_unphased_distributed_role_ordering() {
        let single = parsed_start(start_payload(1, 0, None, None, None)).unwrap();
        assert_eq!(single.phase, None);
        assert_eq!(single.start_deadline, None);
        assert_eq!(single.run_generation, 1);
        let unphased_wire = serde_json::to_value(single).unwrap();
        assert!(unphased_wire.get("phase").is_none());
        assert!(unphased_wire.get("start_deadline").is_none());

        let mut with_rendezvous = start_payload(1, 0, None, None, None);
        with_rendezvous["compiled_execution_plan"]["runtime"]["placement"]["master_port"] =
            Value::from(29500);
        assert!(parsed_start(with_rendezvous).is_err());

        let distributed = parsed_start(start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            None,
        ))
        .unwrap();
        assert_eq!(distributed.phase, None);
        assert_eq!(distributed.start_deadline, None);
        assert_eq!(distributed.run_generation, 1);
    }

    #[test]
    fn typed_runtime_start_and_exact_stop_require_matching_plan_and_generation() {
        let start = valid_start_plan();
        let request = HostRuntimeRequest {
            action: HostRuntimeAction::Start,
            fence: Uuid::new_v4(),
            arguments: vec!["sha256:image".to_owned(), "run".to_owned()],
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: Some(start.run_generation),
            start_plan: Some(start.clone()),
            stop_plan: None,
        };
        assert!(request.validate().is_ok());

        let mut missing_start = request.clone();
        missing_start.start_plan = None;
        assert_eq!(
            missing_start.validate(),
            Err(HostRuntimeRequestRule::PlanBinding)
        );
        let mut mismatched_start_generation = request.clone();
        mismatched_start_generation.run_generation = Some(start.run_generation + 1);
        assert_eq!(
            mismatched_start_generation.validate(),
            Err(HostRuntimeRequestRule::PlanBinding)
        );

        let stop = parsed_stop(stop_payload()).unwrap();
        let stop_request = HostRuntimeRequest {
            action: HostRuntimeAction::Stop,
            fence: Uuid::new_v4(),
            arguments: Vec::new(),
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: Some(stop.run_generation),
            start_plan: None,
            stop_plan: Some(stop),
        };
        assert!(stop_request.validate().is_ok());
        let mut wrong_generation = stop_request.clone();
        wrong_generation.run_generation = Some(2);
        assert_eq!(
            wrong_generation.validate(),
            Err(HostRuntimeRequestRule::PlanBinding)
        );
        let mut argv_stop = stop_request;
        argv_stop.arguments.push("run-id".to_owned());
        assert_eq!(
            argv_stop.validate(),
            Err(HostRuntimeRequestRule::ArgumentsPresence)
        );
    }

    #[test]
    fn typed_job_run_start_requires_the_exact_job_plan_and_generation() {
        let claim: AgentClaim = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
        ))
        .unwrap();
        let generated::AgentClaimPayload::RecipeJobRunRequest(job) = &claim.payload else {
            panic!("expected canonical JobRun claim");
        };
        let request = HostRuntimeRequest {
            action: HostRuntimeAction::Start,
            fence: claim.fence,
            arguments: vec!["sha256:image".to_owned(), "job".to_owned()],
            job_plan: Some(job.clone()),
            installation_id: None,
            reconciliation_identity: None,
            run_generation: Some(job.run_generation),
            start_plan: None,
            stop_plan: None,
        };
        assert!(request.validate().is_ok());
        assert_ne!(job.run_id, job.job_id);

        let mut mismatched_generation = request.clone();
        mismatched_generation.run_generation = Some(job.run_generation + 1);
        assert_eq!(
            mismatched_generation.validate(),
            Err(HostRuntimeRequestRule::PlanBinding)
        );

        let mut duplicate_plan = request;
        duplicate_plan.start_plan = Some(valid_start_plan());
        assert_eq!(
            duplicate_plan.validate(),
            Err(HostRuntimeRequestRule::PlanBinding)
        );
    }

    #[test]
    fn distributed_start_accepts_rank_launch_and_exact_owner_collective_readiness() {
        let launch = parsed_start(start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            Some("rank-launch"),
        ))
        .unwrap();
        assert_eq!(launch.phase, Some(RecipeStartPhase::RankLaunch));

        // Endpoint ownership is identified by the rank's exact local fabric
        // address matching the signed rendezvous address; it need not be rank zero.
        let collective = parsed_start(start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.3"),
            Some("collective-readiness"),
        ))
        .unwrap();
        assert_eq!(
            collective.phase,
            Some(RecipeStartPhase::CollectiveReadiness)
        );

        for field in ["local_address", "master_address", "master_port"] {
            let mut omitted = start_payload(
                2,
                1,
                Some("192.168.100.3"),
                Some("192.168.100.2"),
                Some("rank-launch"),
            );
            omitted["compiled_execution_plan"]["runtime"]["placement"][field] = Value::Null;
            assert!(
                parsed_start(omitted).is_err(),
                "omitted distributed field {field} must be rejected"
            );
        }
    }

    #[test]
    fn phased_start_rejects_unknown_single_node_and_non_owner_phases() {
        for payload in [
            start_payload(1, 0, None, None, Some("rank-launch")),
            start_payload(1, 0, None, None, Some("collective-readiness")),
            start_payload(
                2,
                1,
                Some("192.168.100.3"),
                Some("192.168.100.2"),
                Some("collective-readiness"),
            ),
            start_payload(
                2,
                1,
                Some("192.168.100.3"),
                Some("192.168.100.2"),
                Some("launch"),
            ),
        ] {
            assert!(parsed_start(payload).is_err());
        }

        let mut missing_deadline = start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            Some("rank-launch"),
        );
        missing_deadline
            .as_object_mut()
            .unwrap()
            .remove("start_deadline");
        assert!(parsed_start(missing_deadline).is_err());

        let mut missing_generation = start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            Some("rank-launch"),
        );
        missing_generation
            .as_object_mut()
            .unwrap()
            .remove("run_generation");
        assert!(parsed_start(missing_generation).is_err());

        let mut missing_phase = start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            Some("rank-launch"),
        );
        missing_phase.as_object_mut().unwrap().remove("phase");
        assert!(parsed_start(missing_phase).is_err());

        let mut unphased_with_deadline =
            start_payload(2, 1, Some("192.168.100.3"), Some("192.168.100.2"), None);
        unphased_with_deadline.as_object_mut().unwrap().insert(
            "start_deadline".to_owned(),
            Value::String("2026-09-01T12:00:00+00:00".to_owned()),
        );
        assert!(parsed_start(unphased_with_deadline).is_err());

        let mut non_utc_deadline = start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            Some("rank-launch"),
        );
        non_utc_deadline.as_object_mut().unwrap().insert(
            "start_deadline".to_owned(),
            Value::String("2026-09-01T14:00:00+02:00".to_owned()),
        );
        assert!(parsed_start(non_utc_deadline).is_err());
    }

    #[test]
    fn formatted_start_deadlines_preserve_lexical_bytes() {
        for deadline in [
            "2026-09-01T12:00:00.123000+00:00",
            "2026-09-01T12:00:00.123Z",
        ] {
            let mut payload = start_payload(
                2,
                1,
                Some("192.168.100.3"),
                Some("192.168.100.2"),
                Some("rank-launch"),
            );
            payload["start_deadline"] = Value::String(deadline.into());
            let request = parsed_start(payload).unwrap();
            assert_eq!(request.start_deadline.as_deref(), Some(deadline));
            assert_eq!(
                serde_json::to_value(request).unwrap()["start_deadline"],
                deadline
            );
        }
    }

    #[test]
    fn authenticated_launch_claims_enforce_the_claim_ceiling_and_admit_fresh_work() {
        let mut payload = start_payload(1, 0, None, None, None);
        payload["compiled_execution_plan"]["runtime"]["argv"] =
            serde_json::json!(["x".repeat(516 * 1024)]);
        assert!(claim(payload.clone()).unwrap().validate().is_ok());

        // Claim admission owns the whole claim envelope; the helper separately
        // checks the compiled document's smaller budget before host effects.
        payload["compiled_execution_plan"]["runtime"]["argv"] = serde_json::json!([""]);
        let envelope_bytes = canonical_json(&claim(payload.clone()).unwrap().payload)
            .unwrap()
            .len();
        let argument_bytes = MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES - envelope_bytes;
        payload["compiled_execution_plan"]["runtime"]["argv"] =
            serde_json::json!(["x".repeat(argument_bytes)]);
        let at_limit = claim(payload.clone()).unwrap();
        assert_eq!(
            canonical_json(&at_limit.payload).unwrap().len(),
            MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES
        );
        assert!(at_limit.validate().is_ok());

        payload["compiled_execution_plan"]["runtime"]["argv"] =
            serde_json::json!(["x".repeat(argument_bytes + 1)]);
        assert!(claim(payload).unwrap().validate().is_err());
        assert!(
            claim(start_payload(1, 0, None, None, None))
                .unwrap()
                .validate()
                .is_ok()
        );
    }
}

#[cfg(test)]
mod recipe_install_tests {
    use super::*;

    #[test]
    fn schema_two_install_requires_the_inline_compiled_plan() {
        let payload = serde_json::json!({
            "compiled_execution_plan": serde_json::from_str::<Value>(include_str!("../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json")).unwrap(),
            "expected_bytes": 1024,
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "plan_digest": "a".repeat(64),
        });
        let payload: generated::AgentClaimPayload = serde_json::from_value(payload).unwrap();
        let claim = AgentClaim {
            observation_budget_seconds: 3600,
            deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
            fence: Uuid::new_v4(),
            operation: generated::AgentOperation::RecipeInstall,
            payload,
        };
        let RecipeOperationRequest::Install(request) =
            RecipeOperationRequest::parse(&claim).expect("schema 2 install wire should parse")
        else {
            panic!("expected install request");
        };
        let _ = request;
    }
}

#[cfg(test)]
mod recipe_reconcile_tests {
    use super::*;

    #[test]
    fn reconciliation_names_one_install_and_its_plan() {
        let payload = serde_json::json!({
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "plan_digest": "b".repeat(64),
        });
        let claim = AgentClaim {
            observation_budget_seconds: 3600,
            deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
            fence: Uuid::new_v4(),
            operation: generated::AgentOperation::RecipeReconcile,
            payload: serde_json::from_value(payload).unwrap(),
        };
        let parsed = RecipeOperationRequest::parse(&claim).unwrap();
        assert!(matches!(parsed, RecipeOperationRequest::Reconcile(_)));
    }
}
