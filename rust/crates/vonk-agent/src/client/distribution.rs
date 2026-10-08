//! Distribution for the client boundary.

use super::*;

impl AgentHttpClient {
    /// Download one object from the Controller's assignment-bound delivery
    /// API. A `.partial` destination is a resumable checkpoint; the assignment,
    /// ETag, length, and range are checked on every response.
    pub async fn download_distribution_object(
        &self,
        plan_digest: &str,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        if !valid_sha256(plan_digest) {
            return Err(ClientError::Protocol);
        }
        self.download_trusted_distribution_object_with_progress(
            plan_digest,
            sha256,
            expected_bytes,
            ObjectPlacement {
                destination,
                managed_root: destination.parent().ok_or(ClientError::Protocol)?,
                governor: &StreamGovernor::default(),
            },
            |_, _| {},
        )
        .await
    }

    /// Fetch and validate the assignment manifest before selecting model
    /// files. The manifest is bounded and mTLS-authenticated.
    pub async fn distribution_manifest(
        &self,
        plan_digest: &str,
    ) -> Result<DistributionAssignment, ClientError> {
        if !valid_sha256(plan_digest) {
            return Err(ClientError::Protocol);
        }
        for attempt in 0..3_u32 {
            let result = async {
                let response = self
                    .current_client()
                    .await?
                    .get(self.endpoint(&format!("/agent/distribution/manifests/{plan_digest}"))?)
                    .send()
                    .await?;
                classify_response(&response)?;
                let body = bounded_body(response).await?;
                let assignment: DistributionAssignment =
                    parse_strict(&body).map_err(|_| ClientError::Retryable)?;
                assignment.validate().map_err(|_| ClientError::Retryable)?;
                Ok::<_, ClientError>(assignment)
            }
            .await;
            match result {
                Err(ref error) if error.retryable() && attempt < 2 => {
                    tokio::time::sleep(Duration::from_millis(100 * u64::from(attempt + 1))).await;
                }
                result => return result,
            }
        }
        unreachable!("the final manifest attempt returns")
    }

    /// Consume a complete assignment. Every model/configuration object is
    /// fetched through the assignment-bound endpoint; the runtime image is
    /// pulled separately by its digests. Complete files are reused by trusted metadata, while `.partial` files
    /// resume by identity and length after an agent process restart. Model
    /// objects are stored below one content-addressed root so assignments with
    /// different plan digests can reuse the same verified bytes.
    pub async fn download_distribution(
        &self,
        plan_digest: &str,
        destination_root: &Path,
    ) -> Result<DistributionDownloadEvidence, ClientError> {
        self.download_distribution_with_progress(plan_digest, destination_root, |_| {})
            .await
    }

    pub async fn download_distribution_with_progress<F>(
        &self,
        plan_digest: &str,
        destination_root: &Path,
        progress: F,
    ) -> Result<DistributionDownloadEvidence, ClientError>
    where
        F: FnMut(DistributionProgress),
    {
        if !valid_sha256(plan_digest) || !destination_root.is_absolute() {
            return Err(ClientError::Protocol);
        }
        let assignment = self.distribution_manifest(plan_digest).await?;
        let model_root = destination_root.join("models");
        ensure_managed_directory(destination_root).await?;
        ensure_managed_directory(&model_root).await?;
        ensure_private_parent(&model_root, destination_root).await?;
        let tracker = Mutex::new(DistributionProgressTracker {
            object_bytes: vec![0; assignment.objects.len()],
            bytes: 0,
            completed_items: 0,
            total_bytes: assignment.objects.iter().map(|object| object.bytes).sum(),
            callback: progress,
        });
        let mut pending = Vec::new();
        for (index, object) in assignment.objects.iter().enumerate() {
            let path = model_root.join(&object.sha256);
            let managed_root = destination_root;
            if !path.starts_with(managed_root) {
                return Err(ClientError::Protocol);
            }
            if let Some(parent) = path.parent() {
                tokio::fs::create_dir_all(parent).await?;
            }
            pending.push((index, object, path, managed_root));
        }
        // Start large objects first so a large model file does not become a
        // lone serial tail after all of the smaller files finish.
        pending.sort_by_key(|(_, object, _, _)| std::cmp::Reverse(object.bytes));
        let governor = StreamGovernor::default();
        // Bound both network traffic and disk buffers. These futures stay
        // owned by this call: an error or cancellation drops the remaining
        // transfers, whose partial files remain resumable on disk.
        let mut completed: Vec<_> = stream::iter(pending)
            .map(|(index, object, path, managed_root)| {
                let tracker = &tracker;
                let governor = &governor;
                async move {
                    self.download_trusted_distribution_object_with_progress(
                        plan_digest,
                        &object.sha256,
                        object.bytes,
                        ObjectPlacement {
                            destination: &path,
                            managed_root,
                            governor,
                        },
                        |bytes, _step| {
                            tracker
                                .lock()
                                .expect("distribution progress lock")
                                .report(index, object, bytes, false);
                        },
                    )
                    .await?;
                    tracker.lock().expect("distribution progress lock").report(
                        index,
                        object,
                        object.bytes,
                        true,
                    );
                    Ok::<_, ClientError>((index, path))
                }
            })
            .buffer_unordered(DISTRIBUTION_CONCURRENCY)
            .try_collect()
            .await?;
        completed.sort_unstable_by_key(|(index, _)| *index);
        let mut model_paths = Vec::new();
        let mut model_digests = Vec::new();
        for (index, path) in completed {
            model_paths.push(path);
            model_digests.push(assignment.objects[index].sha256.clone());
        }
        let downloaded_bytes = tracker
            .into_inner()
            .expect("distribution progress lock")
            .bytes;
        Ok(DistributionDownloadEvidence {
            model_digests,
            model_paths,
            oci_image_digest: assignment.oci_image_digest,
            oci_image_config_digest: assignment.oci_image_config_digest,
            downloaded_bytes,
        })
    }

    pub(super) async fn download_trusted_distribution_object_with_progress<F>(
        &self,
        plan_digest: &str,
        sha256: &str,
        expected_bytes: u64,
        placement: ObjectPlacement<'_>,
        mut progress: F,
    ) -> Result<(), ClientError>
    where
        F: FnMut(u64, ProgressPhase),
    {
        let ObjectPlacement {
            destination,
            managed_root,
            governor,
        } = placement;
        // The assignment-bound mTLS endpoint and its exact ranged response
        // headers establish the transfer contract. The digest is the object's
        // name; its size and custody are checked, its bytes are not re-hashed.
        if !valid_sha256(plan_digest)
            || !valid_sha256(sha256)
            || !(1..=16 * 1024_u64.pow(4)).contains(&expected_bytes)
            || !destination.is_absolute()
        {
            return Err(ClientError::Protocol);
        }
        let parent = destination.parent().ok_or(ClientError::Protocol)?;
        if !destination.starts_with(managed_root) {
            return Err(ClientError::Protocol);
        }
        ensure_private_parent(parent, managed_root).await?;

        // An object at its digest name was renamed there only after a full
        // transfer, so its name, size and private custody are the identity.
        if inspect_trusted_final(destination, expected_bytes)
            .await?
            .is_some()
        {
            return Ok(());
        }

        let partial = partial_path(destination);
        let output = open_trusted_partial(&partial).await?;
        let metadata = output
            .metadata()
            .await
            .map_err(|_| ClientError::Retryable)?;
        let mut offset = metadata.len();
        if offset > expected_bytes {
            // The checkpoint is disposable. Its handle has passed the
            // private, single-link, no-follow custody checks.
            output
                .set_len(0)
                .await
                .map_err(|_| ClientError::Retryable)?;
            offset = 0;
        }
        // TLS/network chunks can be much smaller than an efficient disk
        // write. Coalesce them so Tokio does not dispatch a blocking file
        // operation for every received chunk. Keep this writer across range
        // retries; a process restart resumes from the actual partial length.
        preallocate(&output, offset, expected_bytes);
        let mut output = BufWriter::with_capacity(1024 * 1024, output);
        let mut write_behind = WriteBehind::new(offset);
        progress(offset, ProgressPhase::Copying);
        let mut last_progress = tokio::time::Instant::now();
        let mut retries = 0_u32;
        while offset < expected_bytes {
            let deadline = tokio::time::Instant::now() + CONTROLLER_REQUEST_TIMEOUT;
            let end = range_end(offset, expected_bytes);
            let mut url = self.endpoint(&format!("/agent/distribution/objects/{sha256}"))?;
            url.query_pairs_mut()
                .append_pair("plan_digest", plan_digest);
            let attempt = tokio::time::timeout_at(deadline, async {
                // One stream slot for this range: the governor decides how
                // many ranges the agent keeps in flight at once.
                let _stream = tokio::time::timeout_at(deadline, governor.acquire())
                    .await
                    .map_err(|_| ClientError::Retryable)?;
                let mut response = self
                    .current_client()
                    .await?
                    .get(url)
                    .header("range", format!("bytes={offset}-{end}"))
                    .header("if-range", format!("\"sha256:{sha256}\""))
                    .send()
                    .await?;
                classify_response(&response)?;
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
                    return Err(ClientError::Protocol);
                }
                while let Some(chunk) = tokio::time::timeout_at(deadline, response.chunk())
                    .await
                    .map_err(|_| ClientError::Retryable)??
                {
                    if chunk.len() as u64 > end + 1 - offset {
                        return Err(ClientError::Protocol);
                    }
                    tokio::time::timeout_at(deadline, output.write_all(&chunk))
                        .await
                        .map_err(|_| ClientError::Retryable)??;
                    // This writer survives network retries, so resume from
                    // its accepted bytes even within an interrupted range.
                    offset += chunk.len() as u64;
                    governor.record_bytes(chunk.len() as u64);
                    write_behind.written(&mut output, offset).await?;
                    if last_progress.elapsed() >= Duration::from_millis(200) {
                        progress(offset, ProgressPhase::Copying);
                        last_progress = tokio::time::Instant::now();
                    }
                }
                if offset != end + 1 {
                    return Err(ClientError::Retryable);
                }
                Ok::<(), ClientError>(())
            })
            .await
            .unwrap_or(Err(ClientError::Retryable));
            match attempt {
                Ok(()) => retries = 0,
                Err(error) if error.retryable() && retries < 4 => {
                    governor.throttled();
                    progress(offset, ProgressPhase::Copying);
                    tokio::time::sleep(Duration::from_millis(500 * (1 << retries))).await;
                    retries += 1;
                }
                Err(error) => {
                    // Publish no final object, but persist the accepted prefix
                    // before ending this range/request budget. A fresh request
                    // resumes from its durable length rather than replaying it.
                    let deadline = tokio::time::Instant::now() + CONTROLLER_REQUEST_TIMEOUT;
                    tokio::time::timeout_at(deadline, output.flush())
                        .await
                        .map_err(|_| ClientError::Retryable)??;
                    tokio::time::timeout_at(deadline, output.get_ref().sync_data())
                        .await
                        .map_err(|_| ClientError::Retryable)??;
                    return Err(error);
                }
            }
        }
        progress(offset, ProgressPhase::Copying);
        write_behind.finish().await?;
        output.flush().await.map_err(|_| ClientError::Retryable)?;
        output
            .get_ref()
            .sync_all()
            .await
            .map_err(|_| ClientError::Retryable)?;
        let output = output.into_inner();
        let synced_metadata = output
            .metadata()
            .await
            .map_err(|_| ClientError::Retryable)?;
        let partial_metadata = tokio::fs::symlink_metadata(&partial)
            .await
            .map_err(|_| ClientError::Retryable)?;
        if !validate_trusted_metadata(&synced_metadata, expected_bytes)
            || !validate_trusted_metadata(&partial_metadata, expected_bytes)
            || !same_file_metadata(&synced_metadata, &partial_metadata)
        {
            isolate_managed_entry(&partial).await?;
            return Err(ClientError::Retryable);
        }
        // Bytes came over the assignment-bound mTLS channel from our own
        // Controller, so size and custody are checked here and the content is
        // not re-hashed. The digest names the object; ingress hashing happens
        // once, where the Controller's cache first receives the bytes. The
        // object is flushed and about to be renamed into place.
        progress(expected_bytes, ProgressPhase::Finalizing);
        let before_rename = tokio::fs::symlink_metadata(&partial)
            .await
            .map_err(|_| ClientError::Retryable)?;
        if !same_file_metadata(&synced_metadata, &before_rename) {
            isolate_managed_entry(&partial).await?;
            return Err(ClientError::Retryable);
        }
        tokio::fs::rename(&partial, destination)
            .await
            .map_err(|_| ClientError::Retryable)?;
        sync_parent(parent).await?;
        let final_file = inspect_trusted_final(destination, expected_bytes)
            .await?
            .ok_or(ClientError::Retryable)?;
        let final_metadata = final_file
            .metadata()
            .await
            .map_err(|_| ClientError::Retryable)?;
        let output_after = output
            .metadata()
            .await
            .map_err(|_| ClientError::Retryable)?;
        // Rename changes ctime but cannot change the already-synced content
        // of this private inode. Bind the receipt to its post-rename ctime.
        if !same_file_content_identity(&synced_metadata, &output_after)
            || !same_file_metadata(&output_after, &final_metadata)
        {
            isolate_managed_entry(destination).await?;
            return Err(ClientError::Retryable);
        }
        // The transfer filled the page cache with an object of up to hundreds
        // of gigabytes. It stays on disk; the resident pages do not need to.
        release_distribution_page_cache(destination);
        Ok(())
    }
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod recovery_tests;
