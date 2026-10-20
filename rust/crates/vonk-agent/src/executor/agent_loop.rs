//! Agent loop.

use super::*;

#[async_trait]
impl LoopClient for AgentHttpClient {
    async fn claim(
        &self,
        preflight_fingerprint: Option<&str>,
        wait_seconds: u64,
        runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        AgentHttpClient::claim(self, preflight_fingerprint, wait_seconds, runtime_identity).await
    }

    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        AgentHttpClient::heartbeat(self, progress).await
    }

    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        AgentHttpClient::submit_result(self, result).await
    }

    fn progress_observation(&self, fence: uuid::Uuid) -> Option<OperationProgress> {
        let counters = self.progress_snapshot(fence)?;
        Some(OperationProgress {
            completed_bytes: counters.bytes.into(),
            total_bytes: counters.total_bytes.map(Into::into),
            total_bytes_known: counters.total_bytes.is_some(),
            activity_sequence: Some(counters.activity.into()),
            ..phase_progress(counters.phase)
        })
    }
}

impl Drop for CancelExecutionOnDrop {
    fn drop(&mut self) {
        self.0.send_replace(true);
    }
}

pub async fn run_once<C: LoopClient, E: Executor>(
    client: &C,
    state: &mut StateStore,
    executor: &E,
    preflight_fingerprint: Option<&str>,
    wait_seconds: u64,
    runtime_identity: Option<&AgentRuntimeIdentity>,
) -> Result<(), LoopError> {
    run_once_with_heartbeat_interval(
        client,
        state,
        executor,
        RunOncePolicy {
            preflight_fingerprint,
            wait_seconds,
            runtime_identity,
            heartbeat_interval: HEARTBEAT_INTERVAL,
            heartbeat_retry_interval: HEARTBEAT_RETRY_INTERVAL,
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
}

pub async fn run_once_with_claim_hook<C, E, F>(
    client: &C,
    state: &mut StateStore,
    executor: &E,
    preflight_fingerprint: Option<&str>,
    wait_seconds: u64,
    runtime_identity: Option<&AgentRuntimeIdentity>,
    on_claim_accepted: F,
) -> Result<(), LoopError>
where
    C: LoopClient,
    E: Executor,
    F: FnOnce() -> Result<(), LoopError>,
{
    run_once_with_heartbeat_interval(
        client,
        state,
        executor,
        RunOncePolicy {
            preflight_fingerprint,
            wait_seconds,
            runtime_identity,
            heartbeat_interval: HEARTBEAT_INTERVAL,
            heartbeat_retry_interval: HEARTBEAT_RETRY_INTERVAL,
            lease_renewed: crate::systemd_notify::watchdog,
        },
        on_claim_accepted,
    )
    .await
}

pub(super) async fn run_once_with_heartbeat_interval<C, E, F>(
    client: &C,
    state: &mut StateStore,
    executor: &E,
    policy: RunOncePolicy<'_>,
    on_claim_accepted: F,
) -> Result<(), LoopError>
where
    C: LoopClient,
    E: Executor,
    F: FnOnce() -> Result<(), LoopError>,
{
    state.restore_custody();
    if let Err(error) = content_observation::reobserve_content(state, executor).await {
        eprintln!("vonk-agent: preparation observation deferred: {error}");
    }
    let now = Utc::now();
    // Delivery has one request-sized budget per pass, independent of backlog.
    // Losing an upload response retains custody; it never authorizes replayed
    // host effects and never holds the claim lane until every receipt lands.
    let delivery_deadline = tokio::time::Instant::now() + crate::client::HEARTBEAT_REQUEST_TIMEOUT;
    let delivery = async {
        // Scan at most one bounded receipt at a time, yielding at each network
        // observation. Projection writes never surrender receipt custody.
        while tokio::time::Instant::now() < delivery_deadline {
            let Some((_operation, result, acknowledged)) = state.next_delivery_result()? else {
                break;
            };
            tokio::task::yield_now().await;
            if let Some(rejection) = state.result_rejection(&result, now)? {
                // Never silent: the retained, refused result stays visible on
                // every pass until a fresh fence or a resend settles it.
                eprintln!(
                    "vonk-agent: retained result for fence {} stays refused \
                     (http {} {} request_id={}) since {}",
                    result.fence,
                    rejection.http_status,
                    rejection.code,
                    rejection.request_id.as_deref().unwrap_or_default(),
                    rejection.observed_at.to_rfc3339(),
                );
                continue;
            }
            let projection = match submit_or_core(client, &result).await {
                Ok(()) | Err(ClientError::ResultSuperseded) => {
                    if acknowledged {
                        state.mark_reconciled(&result)
                    } else {
                        state.acknowledge(&result)
                    }
                }
                Err(ClientError::ResultRejected(error)) => {
                    if let Err(error) = record_result_rejection(state, &result, &error, now) {
                        eprintln!("vonk-agent: rejection projection deferred: {error}");
                    }
                    continue;
                }
                Err(error) if error.fatal() => return Err(error.into()),
                // A definite refusal of these bytes (400/404/410): recorded
                // durably like an ingress rejection, never a silent stop.
                Err(ClientError::Controller(error)) if error.disposition() == Disposition::Fail => {
                    if let Err(error) = record_result_rejection(state, &result, &error, now) {
                        eprintln!("vonk-agent: rejection projection deferred: {error}");
                    }
                    continue;
                }
                Err(error) => {
                    eprintln!("vonk-agent: retained result delivery deferred: {error}");
                    break;
                }
            };
            if let Err(error) = projection {
                eprintln!("vonk-agent: receipt acknowledgement deferred: {error}");
            }
        }
        Ok::<(), LoopError>(())
    };
    match tokio::time::timeout(crate::client::HEARTBEAT_REQUEST_TIMEOUT, delivery).await {
        Ok(Err(error @ LoopError::Client(_))) => return Err(error),
        Ok(Err(error)) => eprintln!("vonk-agent: receipt scan deferred: {error}"),
        Ok(Ok(())) => {}
        Err(_) => eprintln!("vonk-agent: retained receipt observation budget elapsed"),
    }
    let claim = client
        .claim(
            policy.preflight_fingerprint,
            policy.wait_seconds,
            policy.runtime_identity,
        )
        .await?;
    if let Err(error) = on_claim_accepted() {
        eprintln!("vonk-agent: readiness projection deferred: {error}");
    }
    let Some(claim) = claim else {
        return Ok(());
    };
    claim.validate().map_err(StateError::from)?;
    let result = match state.begin(&claim, Utc::now()) {
        Ok(BeginDecision::Execute) => {
            let heartbeat_state = match state.reopen() {
                Ok(reopened) => Some(reopened),
                Err(error) => {
                    eprintln!("vonk-agent: heartbeat projection unavailable: {error}");
                    None
                }
            };
            let (stop_heartbeat, heartbeat_stop) = tokio::sync::oneshot::channel();
            let (lease_deadline_sender, lease_deadline) =
                tokio::sync::watch::channel(claim.deadline);
            let (cancellation_sender, cancellation) = tokio::sync::watch::channel(false);
            let cancel_on_exit = CancelExecutionOnDrop(cancellation_sender.clone());
            let budget_cancellation = cancellation_sender.clone();
            let heartbeats = run_heartbeats(
                client.clone(),
                heartbeat_state,
                claim.clone(),
                lease_deadline_sender,
                cancellation_sender,
                heartbeat_stop,
                HeartbeatSchedule {
                    interval: policy.heartbeat_interval,
                    retry_interval: policy.heartbeat_retry_interval,
                    renewed: policy.lease_renewed,
                },
            );
            let heartbeat_task = tokio::spawn(async move {
                let _cancel_on_exit = cancel_on_exit;
                heartbeats.await
            });
            let work = executor.execute(&claim, lease_deadline, cancellation);
            tokio::pin!(work);
            // The observation budget is a stall window: the order ends only
            // when its progress stops advancing for a whole window.
            let mut stall = StallWindow::new(Duration::from_secs(u64::from(
                claim.observation_budget_seconds,
            )));
            let executed = tokio::select! {
                result = &mut work => result,
                () = stall.stalled(|| client.progress_observation(claim.fence)) => {
                    budget_cancellation.send_replace(true);
                    match tokio::time::timeout(JOB_CANCEL_DRAIN_TIMEOUT, &mut work).await {
                        Ok(result) => result,
                        Err(_) => ExecutionResult::unknown(
                            WaitReason::RuntimeEffectUnconfirmed,
                            "operation stalled before exact effect settlement",
                            UnknownEvidence::at(FailureStage::Unknown),
                        ),
                    }
                },
            };
            let _ = stop_heartbeat.send(());
            let heartbeat_result = heartbeat_task
                .await
                .map_err(|_| LoopError::HeartbeatTask)
                .and_then(|result| result);
            // The executor owns the effect and its quiescence proof. Preserve
            // its exact cancelled or uncertain outcome; a heartbeat alone
            // cannot turn an in-flight runtime effect into a terminal result.
            let result = match state.finish(&claim, executed) {
                Ok(result) => result,
                Err(error) => {
                    eprintln!("vonk-agent: completion custody unavailable: {error}");
                    state.restore_custody();
                    let finished = ExecutionResult::unknown(
                        WaitReason::RuntimeEffectUnconfirmed,
                        "completion custody awaits exact effect observation",
                        UnknownEvidence::at(FailureStage::AgentRestart),
                    )
                    .finish(&claim);
                    AgentResult {
                        fence: claim.fence,
                        result: finished.result,
                        state: finished.state,
                    }
                }
            };
            heartbeat_result?;
            result
        }
        Ok(BeginDecision::Replay(result)) => *result,
        Err(StateError::Busy) => return Ok(()),
        Err(error) => {
            // No effect is dispatched without durable custody. Local failure
            // ends this exact attempt as uncertainty; a fresh fence still
            // reaches admission and re-observes through the Controller.
            eprintln!("vonk-agent: effect custody unavailable: {error}");
            let finished = ExecutionResult::unknown(
                WaitReason::AgentRestartInterrupted,
                "effect custody is unavailable",
                UnknownEvidence::at(FailureStage::AgentRestart),
            )
            .finish(&claim);
            AgentResult {
                fence: claim.fence,
                result: finished.result,
                state: finished.state,
            }
        }
    };
    result
        .validate_for_operation(&claim.operation)
        .map_err(StateError::from)?;
    if state.result_rejection(&result, now)?.is_none() {
        match submit_or_core(client, &result).await {
            Ok(()) | Err(ClientError::ResultSuperseded) => {
                if let Err(error) = state.acknowledge(&result) {
                    eprintln!("vonk-agent: receipt acknowledgement deferred: {error}");
                }
            }
            Err(ClientError::ResultRejected(error)) => {
                if let Err(error) = record_result_rejection(state, &result, &error, now) {
                    eprintln!("vonk-agent: rejection projection deferred: {error}");
                }
            }
            Err(ClientError::Controller(error)) if error.disposition() == Disposition::Fail => {
                if let Err(error) = record_result_rejection(state, &result, &error, now) {
                    eprintln!("vonk-agent: rejection projection deferred: {error}");
                }
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

/// Deliver `result`; one the Controller refuses as invalid is offered once
/// more at once in its core-only form ([`crate::outcome::core_only`]).
///
/// An accepted core settles the attempt with its true state and code, and the
/// full result stays in local custody as the evidence.  A refused core answers
/// with the first refusal, which is retained and re-offered under the usual
/// cool-down: a corrected Controller rule then accepts the full result, so a
/// Controller fix alone heals it, and the refusal stays visible on every pass
/// and in the fleet view meanwhile.
async fn submit_or_core<C: LoopClient>(
    client: &C,
    result: &AgentResult,
) -> Result<(), ClientError> {
    let refusal = match client.submit_result(result).await {
        Err(ClientError::ResultRejected(refusal))
            if refusal.code
                == vonk_agent_protocol::generated::ControllerErrorCode::ControllerInvalidRequest
                    .as_str() =>
        {
            refusal
        }
        other => return other,
    };
    let Some(core) = crate::outcome::core_only(result) else {
        return Err(ClientError::ResultRejected(refusal));
    };
    eprintln!(
        "vonk-agent: controller refused result for fence {} as invalid (request_id={}); \
         offering its core only",
        result.fence,
        refusal.request_id.as_deref().unwrap_or_default(),
    );
    match client.submit_result(&core).await {
        Ok(()) => {
            eprintln!(
                "vonk-agent: result_core_only_delivered fence={}: the full result stays in local custody",
                result.fence
            );
            Ok(())
        }
        Err(ClientError::ResultRejected(_)) => Err(ClientError::ResultRejected(refusal)),
        other => other,
    }
}

/// Persist one Controller ingress refusal and make it retrievable.
///
/// Only bounded, correlated control-plane facts are recorded: the durable
/// `result_rejections` row carries the HTTP status, the validated error code,
/// the request id and the Controller's bounded summary, while this line makes
/// the same facts retrievable from the agent's log surface.  The rejected
/// values and the request body are never included.
pub(super) fn record_result_rejection(
    state: &mut StateStore,
    result: &AgentResult,
    error: &ControllerError,
    now: DateTime<Utc>,
) -> Result<(), LoopError> {
    let rejection = state.reject_result(result, error, now)?;
    eprintln!(
        "vonk-agent: controller refused result for fence {} \
         (http {} {} request_id={}): {}; retrying the retained result after {}",
        result.fence,
        rejection.http_status,
        rejection.code,
        rejection.request_id.as_deref().unwrap_or("none"),
        rejection.reason,
        rejection.retry_due_at.to_rfc3339(),
    );
    Ok(())
}

#[cfg(test)]
mod tests_0;

#[cfg(test)]
mod core_only_tests;

#[cfg(test)]
mod tests_1;

#[cfg(test)]
mod stall_tests;

#[cfg(test)]
mod pull_progress_tests;
