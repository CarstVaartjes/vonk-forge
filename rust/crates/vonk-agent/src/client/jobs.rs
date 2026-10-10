//! Jobs for the client boundary.

use super::*;

impl AgentHttpClient {
    pub async fn source_bundle(
        &self,
        source_sha256: &str,
        expected_bytes: u64,
    ) -> Result<Vec<u8>, ClientError> {
        if source_sha256.len() != 64
            || !source_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            || !(1..=64 * 1024 * 1024).contains(&expected_bytes)
        {
            return Err(ClientError::Protocol);
        }
        let mut response = self
            .current_client()
            .await?
            .get(self.endpoint(&format!("/agent/source-bundles/{source_sha256}"))?)
            .send()
            .await?;
        classify_response(&mut response).await?;
        if response.content_length() != Some(expected_bytes) {
            return Err(ClientError::Retryable);
        }
        bounded_body_limit(response, expected_bytes as usize)
            .await
            .map_err(|_| ClientError::Retryable)
    }

    pub async fn download_recipe_job_input(
        &self,
        job_id: uuid::Uuid,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        if !valid_sha256(sha256) || expected_bytes > 512 * 1024 * 1024 || !destination.is_absolute()
        {
            return Err(ClientError::Protocol);
        }
        if inspect_trusted_final(destination, expected_bytes)
            .await?
            .is_some()
        {
            return Ok(());
        }
        let mut response = self
            .current_client()
            .await?
            .get(self.endpoint(&format!("/agent/recipe-jobs/{job_id}/inputs/{sha256}"))?)
            .send()
            .await?;
        classify_response(&mut response).await?;
        if response.content_length() != Some(expected_bytes) {
            return Err(ClientError::Retryable);
        }
        let parent = destination.parent().ok_or(ClientError::Retryable)?;
        let temporary = parent.join(format!(".job-input-{}.tmp", uuid::Uuid::new_v4()));
        let result = async {
            let mut output = tokio::fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .mode(0o600)
                .open(&temporary)
                .await?;
            let mut observed = 0_u64;
            let deadline = tokio::time::Instant::now() + CONTROLLER_REQUEST_TIMEOUT;
            while let Some(chunk) = tokio::time::timeout_at(deadline, response.chunk())
                .await
                .map_err(|_| ClientError::Retryable)??
            {
                observed = observed
                    .checked_add(chunk.len() as u64)
                    .filter(|value| *value <= expected_bytes)
                    .ok_or(ClientError::Retryable)?;
                tokio::time::timeout_at(deadline, output.write_all(&chunk))
                    .await
                    .map_err(|_| ClientError::Retryable)??;
            }
            if observed != expected_bytes {
                return Err(ClientError::Retryable);
            }
            output.sync_all().await?;
            drop(output);
            tokio::fs::hard_link(&temporary, destination).await?;
            tokio::fs::remove_file(&temporary).await?;
            Ok(())
        }
        .await;
        if result.is_err() {
            let _ = tokio::fs::remove_file(&temporary).await;
        }
        result
    }

    pub async fn upload_recipe_job_output(
        &self,
        job_id: uuid::Uuid,
        name: &str,
        media_type: &str,
        sha256: &str,
        expected_bytes: u64,
        path: &Path,
    ) -> Result<(), ClientError> {
        if name.is_empty()
            || name == "manifest.json"
            || name.len() > 128
            || !name.as_bytes()[0].is_ascii_alphanumeric()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
            || !valid_sha256(sha256)
            || media_type.is_empty()
            || media_type.len() > 128
        {
            return Err(ClientError::Protocol);
        }
        if tokio::fs::metadata(path)
            .await
            .map_err(|_| ClientError::Retryable)?
            .len()
            != expected_bytes
        {
            return Err(ClientError::Retryable);
        }
        let deadline = tokio::time::Instant::now() + RECIPE_IMAGE_UPLOAD_TIMEOUT;
        for attempt in 0..3_u32 {
            let result = tokio::time::timeout_at(deadline, async {
                let file = tokio::fs::OpenOptions::new()
                    .read(true)
                    .custom_flags(
                        (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
                    )
                    .open(path)
                    .await
                    .map_err(|_| ClientError::Retryable)?;
                let metadata = file.metadata().await.map_err(|_| ClientError::Retryable)?;
                if !metadata.is_file() || metadata.len() != expected_bytes {
                    return Err(ClientError::Retryable);
                }
                let mut response = self
                    .current_client()
                    .await?
                    .put(self.endpoint(&format!("/agent/recipe-jobs/{job_id}/outputs/{sha256}"))?)
                    .header("x-vonk-artifact-name", name)
                    .header("content-type", media_type)
                    .header("content-length", expected_bytes)
                    .timeout(RECIPE_IMAGE_UPLOAD_TIMEOUT)
                    .body(reqwest::Body::wrap_stream(ReaderStream::new(file)))
                    .send()
                    .await?;
                if response.status() == StatusCode::NO_CONTENT {
                    Ok(())
                } else {
                    classify_response(&mut response).await?;
                    Err(ClientError::Protocol)
                }
            })
            .await
            .unwrap_or(Err(ClientError::Retryable));
            match result {
                Err(ref error)
                    if error.retryable()
                        && attempt < 2
                        && tokio::time::Instant::now() < deadline =>
                {
                    // The Controller PUT validates current authority and
                    // attaches the same content receipt idempotently. Repeating
                    // it reconciles a lost acknowledgement without running the
                    // job again or changing the output identity.
                    tokio::time::sleep_until(deadline.min(
                        tokio::time::Instant::now()
                            + error.retry_delay(
                                attempt,
                                Duration::from_millis(100),
                                Duration::from_secs(3),
                            ),
                    ))
                    .await;
                }
                result => return result,
            }
        }
        Err(ClientError::Retryable)
    }

    pub async fn upload_recipe_image<F>(
        &self,
        build_id: uuid::Uuid,
        image_digest: &str,
        oci_layout_sha256: &str,
        image_bytes: u64,
        path: &Path,
        progress: F,
    ) -> Result<(), ClientError>
    where
        F: Fn(u64) + Send + Sync + 'static,
    {
        use futures_util::StreamExt;
        let progress = Arc::new(progress);
        if !valid_oci_digest(image_digest)
            || !valid_sha256(oci_layout_sha256)
            || !(1..=16 * 1024_u64.pow(4)).contains(&image_bytes)
        {
            return Err(ClientError::Protocol);
        }
        if tokio::fs::metadata(path)
            .await
            .map_err(|_| ClientError::Retryable)?
            .len()
            != image_bytes
        {
            return Err(ClientError::Retryable);
        }
        use tokio::io::AsyncSeekExt;
        let endpoint = self.endpoint(&format!("/agent/recipe-builds/{build_id}/image"))?;
        // Retry from the Controller's persisted cursor, never from optimistic sent bytes.
        for attempt in 0..3 {
            let transfer = async {
                let mut status = self
                    .current_client()
                    .await?
                    .head(endpoint.clone())
                    .header("x-vonk-image-digest", image_digest)
                    .header("x-vonk-oci-layout-sha256", oci_layout_sha256)
                    .header("x-vonk-image-bytes", image_bytes)
                    .send()
                    .await?;
                if status.status() != StatusCode::OK {
                    classify_response(&mut status).await?;
                    return Err(ClientError::Protocol);
                }
                let offset = status
                    .headers()
                    .get("x-vonk-upload-offset")
                    .and_then(|value| value.to_str().ok())
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|value| *value <= image_bytes)
                    .ok_or(ClientError::Protocol)?;
                match status
                    .headers()
                    .get("x-vonk-upload-complete")
                    .and_then(|value| value.to_str().ok())
                {
                    Some("true") if offset == image_bytes => {
                        progress(image_bytes);
                        return Ok(());
                    }
                    Some("false") => (),
                    _ => return Err(ClientError::Protocol),
                }
                progress(offset);
                let mut file = tokio::fs::File::open(path).await?;
                file.seek(std::io::SeekFrom::Start(offset)).await?;
                let report = Arc::clone(&progress);
                let mut sent = offset;
                let body = ReaderStream::with_capacity(file, 1024 * 1024).inspect(move |chunk| {
                    if let Ok(bytes) = chunk {
                        sent += bytes.len() as u64;
                        report(sent);
                    }
                });
                let mut response = self
                    .current_client()
                    .await?
                    .put(endpoint.clone())
                    .header("content-type", "application/x-tar")
                    .header("content-length", image_bytes - offset)
                    .header("x-vonk-image-bytes", image_bytes)
                    .header("x-vonk-upload-offset", offset)
                    .header("x-vonk-image-digest", image_digest)
                    .header("x-vonk-oci-layout-sha256", oci_layout_sha256)
                    .timeout(RECIPE_IMAGE_UPLOAD_TIMEOUT)
                    .body(reqwest::Body::wrap_stream(body))
                    .send()
                    .await?;
                if response.status() == StatusCode::NO_CONTENT {
                    Ok(())
                } else {
                    classify_response(&mut response).await?;
                    Err(ClientError::Protocol)
                }
            }
            .await;
            match transfer {
                Err(error) if error.retryable() && attempt < 2 => {
                    // An unreadable upload acknowledgement is observation loss.
                    // Re-enter through HEAD for the exact content identity;
                    // accepted bytes are reused before another PUT is possible.
                    tokio::time::sleep(error.retry_delay(
                        attempt,
                        Duration::from_secs(1),
                        Duration::from_secs(30),
                    ))
                    .await;
                }
                result => return result,
            }
        }
        Err(ClientError::Retryable)
    }
}

#[cfg(test)]
mod tests;
