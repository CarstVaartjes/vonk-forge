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
            if state.result_rejection(&result, now)?.is_some() {
                continue;
            }
            let projection = match client.submit_result(&result).await {
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
            let heartbeat_state = state
                .reopen()
                .map_err(|error| {
                    eprintln!("vonk-agent: heartbeat projection unavailable: {error}");
                })
                .ok();
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
            let executed = tokio::select! {
                result = &mut work => result,
                _ = tokio::time::sleep(Duration::from_secs(u64::from(claim.observation_budget_seconds))) => {
                    budget_cancellation.send_replace(true);
                    match tokio::time::timeout(JOB_CANCEL_DRAIN_TIMEOUT, &mut work).await {
                        Ok(result) => result,
                        Err(_) => ExecutionResult::unknown(
                            WaitReason::RuntimeEffectUnconfirmed,
                            "operation observation budget elapsed before exact effect settlement",
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
        match client.submit_result(&result).await {
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
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
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
mod tests_1;
