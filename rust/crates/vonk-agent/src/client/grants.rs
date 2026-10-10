//! Grants for the client boundary.

use super::*;

impl AgentHttpClient {
    pub async fn host_runtime_grant(
        &self,
        claim: &AgentClaim,
        request: &HostRuntimeRequest,
        request_sha256: &str,
    ) -> Result<SignedHostHelperGrant, ClientError> {
        self.host_runtime_grant_with_intent_nonce(claim, request, request_sha256, None)
            .await
    }

    pub async fn host_runtime_grant_with_intent_nonce(
        &self,
        claim: &AgentClaim,
        request: &HostRuntimeRequest,
        request_sha256: &str,
        nonce: Option<String>,
    ) -> Result<SignedHostHelperGrant, ClientError> {
        let mut grant_request = build_host_runtime_grant_request(claim, request, request_sha256)?;
        grant_request.installation_intent_nonce = nonce;
        let body = canonical_generated_json(&grant_request).map_err(|_| ClientError::Protocol)?;
        let mut response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/host-runtime/grant")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        classify_response(&mut response).await?;
        let body = bounded_body(response).await?;
        let response: HostHelperGrantResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        Ok(response.grant)
    }

    /// Whether the Controller has any record of one locally retained run.
    ///
    /// Needs only the run id, so it also answers for a run directory whose
    /// managed metadata this agent can no longer parse.  The Controller names
    /// only a run it never owned; any other header value is outside the
    /// contract and never guessed at.
    pub async fn recipe_run_disposition(
        &self,
        run_id: uuid::Uuid,
    ) -> Result<RecipeRunDisposition, ClientError> {
        let mut response = self
            .current_client()
            .await?
            .get(self.endpoint(&format!("/agent/recipe-runs/{run_id}/disposition"))?)
            .send()
            .await?;
        classify_response(&mut response).await?;
        match response.headers().get(RECIPE_RUN_DISPOSITION_HEADER) {
            None => Ok(RecipeRunDisposition::Known {
                run_generation: response
                    .headers()
                    .get(RECIPE_RUN_GENERATION_HEADER)
                    .and_then(|value| value.to_str().ok())
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|generation| (1..=i64::MAX as u64).contains(generation)),
            }),
            Some(value) if value.as_bytes() == RECIPE_RUN_UNOWNED.as_bytes() => {
                Ok(RecipeRunDisposition::Unowned)
            }
            Some(_) => Err(ClientError::Protocol),
        }
    }

    pub async fn package_activation_grant(
        &self,
        receipt: &vonk_agent_protocol::PackageActivationReceipt,
        runtime_identity: &AgentRuntimeIdentity,
    ) -> Result<SignedHostHelperGrant, ClientError> {
        let body = canonical_generated_json(&PackageActivationGrantRequest {
            receipt: receipt.clone(),
            runtime_identity: runtime_identity.clone(),
        })
        .map_err(|_| ClientError::Protocol)?;
        let mut response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/agent-upgrade/activation-grant")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        classify_response(&mut response).await?;
        let body = bounded_body(response).await?;
        let response: HostHelperGrantResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        Ok(response.grant)
    }

    pub async fn agent_upgrade_grant(
        &self,
        claim: &AgentClaim,
        package_sha256: &str,
        package_signature: &str,
    ) -> Result<SignedHostHelperGrant, ClientError> {
        if !valid_sha256(package_sha256)
            || package_signature.len() != 128
            || !package_signature
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(ClientError::Protocol);
        }
        let body = canonical_generated_json(&AgentUpgradeGrantRequest {
            fence: claim.fence,
            package_sha256: package_sha256.to_owned(),
            package_signature: package_signature.to_owned(),
            expires_in_seconds: u32::from(HOST_RUNTIME_GRANT_TTL_SECONDS),
        })
        .map_err(|_| ClientError::Protocol)?;
        let mut response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/agent-upgrade/grant")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        classify_response(&mut response).await?;
        let body = bounded_body(response).await?;
        let response: HostHelperGrantResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        Ok(response.grant)
    }
}

#[derive(Default)]
pub(super) struct HostRuntimePlanBinding {
    start_plan_sha256: Option<String>,
    stop_plan_sha256: Option<String>,
    run_generation: Option<u64>,
    runtime_run_id: Option<uuid::Uuid>,
    runtime_target_id: Option<uuid::Uuid>,
    runtime_installation_id: Option<uuid::Uuid>,
}

pub(super) fn build_host_runtime_grant_request(
    claim: &AgentClaim,
    request: &HostRuntimeRequest,
    request_sha256: &str,
) -> Result<HostRuntimeGrantRequest, ClientError> {
    request.validate().map_err(|_| ClientError::Protocol)?;
    let request_body = canonical_json(request).map_err(|_| ClientError::Protocol)?;
    if !valid_sha256(request_sha256)
        || hex_sha256(&request_body) != request_sha256
        || request.fence != claim.fence
    {
        return Err(ClientError::Protocol);
    }
    let plan_binding = host_runtime_plan_binding(request)?;
    Ok(HostRuntimeGrantRequest {
        installation_intent_nonce: None,
        fence: claim.fence,
        action: host_runtime_grant_action(request.action),
        request_sha256: request_sha256.to_owned(),
        expires_in_seconds: u32::from(HOST_RUNTIME_GRANT_TTL_SECONDS),
        installation_id: request.installation_id,
        reconciliation_identity: request.reconciliation_identity.clone(),
        start_plan_sha256: plan_binding.start_plan_sha256,
        stop_plan_sha256: plan_binding.stop_plan_sha256,
        run_generation: plan_binding.run_generation,
        runtime_run_id: plan_binding.runtime_run_id,
        runtime_target_id: plan_binding.runtime_target_id,
        runtime_installation_id: plan_binding.runtime_installation_id,
    })
}

pub(super) fn host_runtime_plan_binding(
    request: &HostRuntimeRequest,
) -> Result<HostRuntimePlanBinding, ClientError> {
    match request.action {
        HostRuntimeAction::Start => {
            if let Some(plan) = request.start_plan.as_ref() {
                let plan_bytes =
                    canonical_generated_json(plan).map_err(|_| ClientError::Protocol)?;
                Ok(HostRuntimePlanBinding {
                    start_plan_sha256: Some(hex_sha256(&plan_bytes)),
                    run_generation: Some(plan.run_generation),
                    runtime_run_id: Some(plan.run_id),
                    runtime_target_id: Some(plan.run_id),
                    runtime_installation_id: Some(plan.installation_id),
                    ..HostRuntimePlanBinding::default()
                })
            } else if let Some(plan) = request.job_plan.as_ref() {
                let plan_bytes =
                    canonical_generated_json(plan).map_err(|_| ClientError::Protocol)?;
                Ok(HostRuntimePlanBinding {
                    start_plan_sha256: Some(hex_sha256(&plan_bytes)),
                    run_generation: Some(plan.run_generation),
                    runtime_run_id: Some(plan.run_id),
                    runtime_target_id: Some(plan.job_id),
                    runtime_installation_id: Some(plan.installation_id),
                    ..HostRuntimePlanBinding::default()
                })
            } else {
                Err(ClientError::Protocol)
            }
        }
        HostRuntimeAction::Stop => {
            let plan = request.stop_plan.as_ref().ok_or(ClientError::Protocol)?;
            let plan_bytes = canonical_generated_json(plan).map_err(|_| ClientError::Protocol)?;
            Ok(HostRuntimePlanBinding {
                stop_plan_sha256: Some(hex_sha256(&plan_bytes)),
                run_generation: Some(plan.run_generation),
                runtime_run_id: Some(plan.run_id),
                runtime_target_id: Some(plan.target_runtime_id),
                runtime_installation_id: Some(plan.installation_id),
                ..HostRuntimePlanBinding::default()
            })
        }
        _ => Ok(HostRuntimePlanBinding::default()),
    }
}

pub(super) fn host_runtime_grant_action(
    action: HostRuntimeAction,
) -> HostRuntimeGrantRequestAction {
    match action {
        HostRuntimeAction::RuntimePreflight => HostRuntimeGrantRequestAction::RuntimePreflight,
        HostRuntimeAction::ImagePull => HostRuntimeGrantRequestAction::ImagePull,
        HostRuntimeAction::ImageInspect => HostRuntimeGrantRequestAction::ImageInspect,
        HostRuntimeAction::RunInspect => HostRuntimeGrantRequestAction::RunInspect,
        HostRuntimeAction::Start => HostRuntimeGrantRequestAction::Start,
        HostRuntimeAction::Stop => HostRuntimeGrantRequestAction::Stop,
        HostRuntimeAction::InstallationCleanup => {
            HostRuntimeGrantRequestAction::InstallationCleanup
        }
    }
}

#[cfg(test)]
mod tests;
