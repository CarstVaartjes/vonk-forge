//! Transfer for the client boundary.

use super::*;

impl AgentHttpClient {
    pub async fn download_artifact(
        &self,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        self.download_content_addressed(
            &format!("/agent/artifacts/{sha256}"),
            None,
            sha256,
            expected_bytes,
            destination,
        )
        .await
    }

    pub(super) async fn download_content_addressed(
        &self,
        endpoint: &str,
        plan_digest: Option<&str>,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        self.download_content_addressed_with_progress(
            endpoint,
            plan_digest,
            sha256,
            expected_bytes,
            destination,
            |_| {},
        )
        .await
    }

    pub(super) async fn download_content_addressed_with_progress<F>(
        &self,
        endpoint: &str,
        plan_digest: Option<&str>,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
        mut progress: F,
    ) -> Result<(), ClientError>
    where
        F: FnMut(u64),
    {
        if !valid_sha256(sha256) || !(1..=16 * 1024_u64.pow(4)).contains(&expected_bytes) {
            return Err(ClientError::Protocol);
        }
        let existing = match tokio::fs::metadata(destination).await {
            Ok(metadata) => metadata.len(),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
            Err(error) => return Err(ClientError::CredentialRead(error)),
        };
        if existing > expected_bytes {
            return Err(ClientError::Protocol);
        }
        if existing == expected_bytes {
            progress(expected_bytes);
            return Ok(());
        }
        let mut output = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(destination)
            .await?;
        let mut offset = existing;
        preallocate(&output, offset, expected_bytes);
        while offset < expected_bytes {
            let deadline = tokio::time::Instant::now() + CONTROLLER_REQUEST_TIMEOUT;
            let end = range_end(offset, expected_bytes);
            let mut url = self.endpoint(&format!("{endpoint}/{sha256}"))?;
            if let Some(plan_digest) = plan_digest {
                url.query_pairs_mut()
                    .append_pair("plan_digest", plan_digest);
            }
            let response = self
                .current_client()
                .await?
                .get(url)
                .header("range", format!("bytes={offset}-{end}"))
                .header("if-range", format!("\"sha256:{sha256}\""))
                .send()
                .await?;
            let expected_etag = format!("\"sha256:{sha256}\"");
            let expected_range = format!("bytes {offset}-{end}/{expected_bytes}");
            if response.status() != StatusCode::PARTIAL_CONTENT
                || response.content_length() != Some(end - offset + 1)
                || response
                    .headers()
                    .get("etag")
                    .and_then(|value| value.to_str().ok())
                    != Some(expected_etag.as_str())
                || response
                    .headers()
                    .get("content-range")
                    .and_then(|value| value.to_str().ok())
                    != Some(expected_range.as_str())
            {
                classify_response(&response)?;
                return Err(ClientError::Protocol);
            }
            let mut copied = 0_u64;
            let expected_chunk = end - offset + 1;
            let mut response = response;
            while let Some(chunk) = tokio::time::timeout_at(deadline, response.chunk())
                .await
                .map_err(|_| ClientError::Retryable)??
            {
                copied = copied.saturating_add(chunk.len() as u64);
                if copied > expected_chunk {
                    return Err(ClientError::Protocol);
                }
                tokio::time::timeout_at(deadline, output.write_all(&chunk))
                    .await
                    .map_err(|_| ClientError::Retryable)??;
            }
            if copied != expected_chunk {
                return Err(ClientError::Protocol);
            }
            offset = end + 1;
            progress(offset);
        }
        output.sync_all().await?;
        if tokio::fs::metadata(destination).await?.len() != expected_bytes {
            return Err(ClientError::Protocol);
        }
        Ok(())
    }
}
