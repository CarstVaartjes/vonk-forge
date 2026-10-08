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
        let response = self
            .current_client()
            .await?
            .get(self.endpoint(&format!("/agent/source-bundles/{source_sha256}"))?)
            .send()
            .await?;
        classify_response(&response)?;
        if response.content_length() != Some(expected_bytes) {
            return Err(ClientError::Protocol);
        }
        bounded_body_limit(response, expected_bytes as usize).await
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
        let mut response = self
            .current_client()
            .await?
            .get(self.endpoint(&format!("/agent/recipe-jobs/{job_id}/inputs/{sha256}"))?)
            .send()
            .await?;
        classify_response(&response)?;
        if response.content_length() != Some(expected_bytes) {
            return Err(ClientError::Protocol);
        }
        let parent = destination.parent().ok_or(ClientError::Protocol)?;
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
                    .ok_or(ClientError::Protocol)?;
                tokio::time::timeout_at(deadline, output.write_all(&chunk))
                    .await
                    .map_err(|_| ClientError::Retryable)??;
            }
            if observed != expected_bytes {
                return Err(ClientError::Protocol);
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
            || tokio::fs::metadata(path).await?.len() != expected_bytes
        {
            return Err(ClientError::Protocol);
        }
        let file = tokio::fs::File::open(path).await?;
        let response = self
            .current_client()
            .await?
            .put(self.endpoint(&format!("/agent/recipe-jobs/{job_id}/outputs/{sha256}"))?)
            .header("x-vonk-artifact-name", name)
            .header("content-type", media_type)
            .header("content-length", expected_bytes)
            .timeout(Duration::from_secs(3600))
            .body(reqwest::Body::wrap_stream(ReaderStream::new(file)))
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_response(&response)?;
            Err(ClientError::Protocol)
        }
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
            || tokio::fs::metadata(path).await?.len() != image_bytes
        {
            return Err(ClientError::Protocol);
        }
        use tokio::io::AsyncSeekExt;
        let endpoint = self.endpoint(&format!("/agent/recipe-builds/{build_id}/image"))?;
        // Retry from the Controller's persisted cursor, never from optimistic sent bytes.
        for attempt in 0..3 {
            let transfer = async {
                let status = self
                    .current_client()
                    .await?
                    .head(endpoint.clone())
                    .header("x-vonk-image-digest", image_digest)
                    .header("x-vonk-oci-layout-sha256", oci_layout_sha256)
                    .header("x-vonk-image-bytes", image_bytes)
                    .send()
                    .await?;
                if status.status() == StatusCode::CONFLICT {
                    return Err(ClientError::Retryable);
                }
                if status.status() != StatusCode::OK {
                    classify_response(&status)?;
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
                let response = self
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
                } else if response.status() == StatusCode::CONFLICT {
                    Err(ClientError::Retryable)
                } else {
                    classify_response(&response)?;
                    Err(ClientError::Protocol)
                }
            }
            .await;
            match transfer {
                Err(error) if error.retryable() && attempt < 2 => {
                    tokio::time::sleep(Duration::from_secs(1 << attempt)).await;
                }
                result => return result,
            }
        }
        unreachable!("last transfer attempt returns")
    }
}

#[cfg(test)]
mod tests;
