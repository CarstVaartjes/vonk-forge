//! Operations for the client boundary.

use super::*;

impl AgentHttpClient {
    pub async fn claim(
        &self,
        preflight_fingerprint: Option<&str>,
        wait_seconds: u64,
        runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        let hostname = local_hostname();
        let body = claim_request_document(
            preflight_fingerprint,
            hostname.as_deref(),
            wait_seconds,
            runtime_identity.ok_or(ClientError::Protocol)?,
        )?;
        let mut response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/claim")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        let status = response.status();
        if status == StatusCode::NO_CONTENT {
            return Ok(None);
        }
        classify_response(&mut response).await?;
        let body = bounded_claim_body(response).await?;
        parse_claim_response(status.as_u16(), &body)
    }

    pub async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        result.validate().map_err(|_| ClientError::Protocol)?;
        let body = canonical_json(result).map_err(|_| ClientError::Protocol)?;
        let mut response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/result")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        match response.status() {
            StatusCode::NO_CONTENT | StatusCode::ACCEPTED => Ok(()),
            // A 409 fences this result: the attempt is no longer current, or
            // its outcome was already consumed.  Either way the Controller did
            // not acknowledge this submission, so the caller keeps the evidence
            // instead of deleting it on the strength of a refusal.
            StatusCode::CONFLICT => Err(ClientError::ResultSuperseded),
            // A 422 refuses these exact bytes at the Controller's ingress
            // validation boundary.  The caller records the bounded refusal and
            // keeps the loop alive; it never treats the refusal as acceptance.
            // This is the one boundary that reads the error body: the
            // Controller publishes a bounded, redacted validation problem
            // there, and a refusal recorded without it cannot say which field
            // or rule rejected the result.  Anything absent, oversized or not
            // of the declared shape leaves the digest unset rather than
            // guessing at it.
            StatusCode::UNPROCESSABLE_ENTITY => {
                let mut error = response_controller_error(&response);
                error.summary = bounded_body(response)
                    .await
                    .ok()
                    .and_then(|body| controller_rejection_digest(&body));
                Err(ClientError::ResultRejected(Box::new(error)))
            }
            _ => {
                classify_response(&mut response).await?;
                Err(ClientError::Protocol)
            }
        }
    }

    pub(crate) fn set_progress_phase(&self, fence: uuid::Uuid, phase: ProgressPhase) {
        *self
            .progress_phase
            .lock()
            .expect("progress phase lock poisoned") = Some(ProgressSnapshot {
            fence,
            phase,
            counters: None,
        });
    }

    pub(crate) fn set_progress_bytes(&self, fence: uuid::Uuid, bytes: u64, total: u64) {
        if let Some(snapshot) = self
            .progress_phase
            .lock()
            .expect("progress phase lock poisoned")
            .as_mut()
            && snapshot.fence == fence
        {
            let high_water = snapshot
                .counters
                .map(|(current, _)| current)
                .unwrap_or(0)
                .max(bytes);
            snapshot.counters = Some((high_water, total));
        }
    }

    pub async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        let mut progress = progress.clone();
        if let Some(measured) = progress.progress.as_mut()
            && crate::vocabulary::is(&measured.phase, ProgressPhase::Executing)
            && let Some(snapshot) = self
                .progress_phase
                .lock()
                .expect("progress phase lock poisoned")
                .as_ref()
            && snapshot.fence == progress.fence
        {
            measured.phase = snapshot.phase.to_string();
            if let Some((bytes, total)) = snapshot.counters {
                measured.completed_bytes = bytes.into();
                measured.total_bytes = Some(total.into());
                measured.total_bytes_known = true;
            }
        }
        progress.validate().map_err(|_| ClientError::Protocol)?;
        let body = canonical_json(&progress).map_err(|_| ClientError::Protocol)?;
        // A renewal that arrives after the accepted lease deadline is still
        // accepted inside the Controller's renewal allowance, so the attempt
        // must not be truncated to the lease that is being recovered: bounding
        // it that way turned "the lease is nearly spent" into "no round trip can
        // complete", which is what made one late renewal the last one.
        let mut response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/heartbeat")?)
            .header("content-type", "application/json")
            .timeout(HEARTBEAT_REQUEST_TIMEOUT)
            .body(body)
            .send()
            .await?;
        classify_response(&mut response).await?;
        let body = bounded_body(response).await?;
        let directive = parse_strict::<AgentDirective>(&body).map_err(|_| ClientError::Protocol)?;
        if directive.fence != progress.fence {
            return Err(ClientError::Protocol);
        }
        Ok(directive)
    }

    /// Read one object of the Controller's runtime image store.
    ///
    /// Only the registry ping and the two digest routes are reachable; the
    /// caller (the loopback image store) passes Docker's `Accept` and
    /// `Range` through so manifests negotiate and interrupted blobs resume.
    pub async fn image_store_request(
        &self,
        head: bool,
        path: &str,
        accept: Option<&str>,
        range: Option<&str>,
    ) -> Result<reqwest::Response, ClientError> {
        if !crate::image_store::valid_store_path(path) {
            return Err(ClientError::Protocol);
        }
        let client = self.current_client().await?;
        let url = self.endpoint(path)?;
        let mut request = if head {
            client.head(url)
        } else {
            client.get(url)
        };
        if let Some(accept) = accept {
            request = request.header("accept", accept);
        }
        if let Some(range) = range {
            request = request.header("range", range);
        }
        Ok(request.send().await?)
    }
}

#[cfg(test)]
mod tests;
