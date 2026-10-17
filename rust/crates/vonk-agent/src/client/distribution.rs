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
        let deadline = tokio::time::Instant::now() + CONTROLLER_REQUEST_TIMEOUT;
        for attempt in 0..3_u32 {
            let result = tokio::time::timeout_at(deadline, async {
                let mut response = self
                    .current_client()
                    .await?
                    .get(self.endpoint(&format!("/agent/distribution/manifests/{plan_digest}"))?)
                    .send()
                    .await?;
                classify_response(&mut response).await?;
                let body = bounded_body(response).await?;
                let assignment: DistributionAssignment =
                    parse_strict(&body).map_err(|_| ClientError::Protocol)?;
                assignment.validate().map_err(|_| ClientError::Protocol)?;
                Ok::<_, ClientError>(assignment)
            })
            .await
            .unwrap_or(Err(ClientError::Unsettled(
                "manifest request deadline elapsed",
            )));
            match result {
                Err(ref error)
                    if error.retryable()
                        && attempt < 2
                        && tokio::time::Instant::now() < deadline =>
                {
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
        Err(ClientError::Unsettled(
            "bounded attempts ended without a settled manifest",
        ))
    }

    /// Consume a complete assignment. Every model/configuration object is
    /// fetched through the assignment-bound endpoint; the runtime image is
    /// pulled separately by its digests. Complete files are reused by trusted metadata, while `.partial` files
    /// resume by identity and their recorded ranges after an agent process restart. Model
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
                                .unwrap_or_else(|poisoned| poisoned.into_inner())
                                .report(index, object, bytes, false);
                        },
                    )
                    .await?;
                    tracker
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .report(index, object, object.bytes, true);
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
            .unwrap_or_else(|poisoned| poisoned.into_inner())
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
        progress: F,
    ) -> Result<(), ClientError>
    where
        F: FnMut(u64, ProgressPhase),
    {
        self.download_trusted_object_with_progress(
            &format!("/agent/distribution/objects/{sha256}"),
            Some(plan_digest),
            sha256,
            expected_bytes,
            placement,
            progress,
        )
        .await
    }

    pub(super) async fn download_trusted_object_with_progress<F>(
        &self,
        endpoint: &str,
        plan_digest: Option<&str>,
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
        if plan_digest.is_some_and(|digest| !valid_sha256(digest))
            || !valid_sha256(sha256)
            || expected_bytes > 16 * 1024_u64.pow(4)
            || (expected_bytes == 0 && sha256 != hex_sha256(&[]))
            || !destination.is_absolute()
        {
            return Err(ClientError::Protocol);
        }
        let parent = destination.parent().ok_or(ClientError::Protocol)?;
        if !destination.starts_with(managed_root) {
            return Err(ClientError::Protocol);
        }
        // No budget covers the object: it moves while its ranges move bytes,
        // and each range ends only on a whole window without a byte.
        async {
            ensure_private_parent(parent, managed_root).await?;

            // An object at its digest name was renamed there only after a full
            // transfer, so its name, size and private custody are the identity.
            if let Some(object) = inspect_trusted_final(destination, expected_bytes).await? {
                if plan_digest.is_some() {
                    mark_claimed(object).await;
                }
                return Ok(());
            }

            let partial = partial_path(destination);
            let output = open_trusted_partial(&partial).await?;
            let length = output.metadata().await.map_err(ClientError::Local)?.len();
            preallocate(&output, length.min(expected_bytes), expected_bytes);
            let file = Arc::new(output.try_clone().await?.into_std().await);
            let (record, pending) =
                RangeRecord::adopt(file, length, expected_bytes, self.distribution_range_bytes)
                    .await?;
            let remaining: u64 = pending
                .iter()
                .map(|range| range.end + 1 - range.start)
                .sum();
            progress(expected_bytes - remaining, ProgressPhase::Copying);
            let mut url = self.endpoint(endpoint)?;
            if let Some(plan_digest) = plan_digest {
                url.query_pairs_mut()
                    .append_pair("plan_digest", plan_digest);
            }
            let transfer = RangeTransfer {
                url,
                sha256,
                total: expected_bytes,
                record: &record,
                governor,
                accepted: Mutex::new(Accepted {
                    bytes: expected_bytes - remaining,
                    reported: tokio::time::Instant::now(),
                    progress,
                }),
            };
            // The ranges of one object move at once, as many as the governor
            // grants: one stream's rate is no longer a large file's rate.
            // Each range lands at its own place and is recorded once synced.
            let fetched = stream::iter(pending)
                .map(|range| self.fetch_range(&transfer, range))
                .buffer_unordered(DISTRIBUTION_CONCURRENCY)
                .try_collect::<()>()
                .await;
            if let Err(error) = fetched {
                // Publish no final object, but persist the recorded ranges
                // before ending this request; a fresh request skips them.
                record.sync().await?;
                return Err(error);
            }
            let mut progress = transfer
                .accepted
                .into_inner()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .progress;
            progress(expected_bytes, ProgressPhase::Copying);
            record.finish().await?;
            let synced_metadata = output.metadata().await.map_err(ClientError::Local)?;
            let partial_metadata = tokio::fs::symlink_metadata(&partial)
                .await
                .map_err(ClientError::Local)?;
            if !validate_trusted_metadata(&synced_metadata, expected_bytes)
                || !validate_trusted_metadata(&partial_metadata, expected_bytes)
                || !same_file_metadata(&synced_metadata, &partial_metadata)
            {
                isolate_managed_entry(&partial).await?;
                return Err(ClientError::Unsettled(
                    "partial file metadata changed while it was written",
                ));
            }
            // Bytes came over the assignment-bound mTLS channel from our own
            // Controller, so size and custody are checked here and the content is
            // not re-hashed. The digest names the object; ingress hashing happens
            // once, where the Controller's cache first receives the bytes. The
            // object is flushed and about to be renamed into place.
            progress(expected_bytes, ProgressPhase::Finalizing);
            let before_rename = tokio::fs::symlink_metadata(&partial)
                .await
                .map_err(ClientError::Local)?;
            if !same_file_metadata(&synced_metadata, &before_rename) {
                isolate_managed_entry(&partial).await?;
                return Err(ClientError::Unsettled(
                    "partial file changed before it was published",
                ));
            }
            tokio::fs::rename(&partial, destination)
                .await
                .map_err(ClientError::Local)?;
            sync_parent(parent).await?;
            let final_file = inspect_trusted_final(destination, expected_bytes)
                .await?
                .ok_or(ClientError::Unsettled(
                    "published file could not be inspected",
                ))?;
            let final_metadata = final_file.metadata().await.map_err(ClientError::Local)?;
            let output_after = output.metadata().await.map_err(ClientError::Local)?;
            // Rename changes ctime but cannot change the already-synced content
            // of this private inode. Bind the receipt to its post-rename ctime.
            if !same_file_content_identity(&synced_metadata, &output_after)
                || !same_file_metadata(&output_after, &final_metadata)
            {
                isolate_managed_entry(destination).await?;
                return Err(ClientError::Unsettled(
                    "published file changed after it was written",
                ));
            }
            // The transfer filled the page cache with an object of up to hundreds
            // of gigabytes. It stays on disk; the resident pages do not need to.
            release_distribution_page_cache(destination);
            Ok(())
        }
        .await
    }

    /// Fetch the rest of one range into its place in the partial file and
    /// record it once its bytes are synced.
    async fn fetch_range<F>(
        &self,
        transfer: &RangeTransfer<'_, F>,
        range: PendingRange,
    ) -> Result<(), ClientError>
    where
        F: FnMut(u64, ProgressPhase),
    {
        let RangeTransfer {
            url,
            sha256,
            total,
            record,
            governor,
            accepted,
        } = transfer;
        let end = range.end;
        // Accepted bytes not yet written; they belong at `offset - len`.
        let mut buffer = Vec::new();
        let mut offset = range.start;
        // Consecutive range attempts that moved no byte; any byte resets it.
        let mut retries = 0_u32;
        let max_attempts = 4_u32;
        loop {
            let started = offset;
            // One stream slot for this range: the governor decides how many
            // ranges the agent keeps in flight at once. Waiting for a slot is
            // waiting on the ranges that hold one; the range's inactivity
            // window starts once it has its slot, and the slot is held until
            // the range is recorded.
            let stream = governor.acquire().await;
            let attempt = async {
                let inactivity = Inactivity::new(self.transfer_inactivity);
                let mut response = send_streaming(
                    self.current_client()
                        .await?
                        .get(url.clone())
                        .header("range", format!("bytes={offset}-{end}"))
                        .header("if-range", format!("\"sha256:{sha256}\"")),
                    &inactivity,
                )
                .await?;
                classify_response(&mut response).await?;
                let expected_etag = format!("\"sha256:{sha256}\"");
                let expected_range = format!("bytes {offset}-{end}/{total}");
                let etag = response
                    .headers()
                    .get("etag")
                    .and_then(|value| value.to_str().ok());
                // A present substituted digest is an ingress identity failure.
                // Missing/unreadable framing is only an unusable observation.
                if etag.is_some_and(|value| value != expected_etag) {
                    return Err(ClientError::Protocol);
                }
                if response.status() != StatusCode::PARTIAL_CONTENT
                    || response.content_length() != Some(end - offset + 1)
                    || etag.is_none()
                    || response
                        .headers()
                        .get("content-range")
                        .and_then(|value| value.to_str().ok())
                        != Some(expected_range.as_str())
                {
                    return Err(ClientError::Unsettled(
                        "answer range differs from the requested range",
                    ));
                }
                while let Some(chunk) = next_chunk(response.chunk(), &inactivity).await? {
                    if chunk.len() as u64 > end + 1 - offset {
                        return Err(ClientError::Unsettled("answer exceeds the requested range"));
                    }
                    // Accepted bytes survive network retries, so a retry
                    // resumes from them even within an interrupted range.
                    buffer.extend_from_slice(&chunk);
                    offset += chunk.len() as u64;
                    governor.record_bytes(chunk.len() as u64);
                    accepted
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .add(chunk.len() as u64);
                    // TLS/network chunks can be much smaller than an efficient
                    // disk write. Coalesce them so Tokio does not dispatch a
                    // blocking file operation for every received chunk.
                    if buffer.len() >= WRITE_BUFFER_BYTES {
                        write_accepted(record, &inactivity, &mut offset, &mut buffer).await?;
                    }
                }
                if offset != end + 1 {
                    return Err(ClientError::Unsettled(
                        "answer ended before the requested range",
                    ));
                }
                write_accepted(record, &inactivity, &mut offset, &mut buffer).await
            }
            .await;
            if offset > started {
                retries = 0;
            }
            match attempt {
                Ok(()) => {
                    record.complete(range).await?;
                    drop(stream);
                    return Ok(());
                }
                Err(error) if error.retryable() && retries < max_attempts => {
                    drop(stream);
                    governor.throttled();
                    accepted
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .report();
                    tokio::time::sleep(error.retry_delay(
                        retries,
                        Duration::from_millis(500),
                        Duration::from_secs(30),
                    ))
                    .await;
                    retries += 1;
                }
                Err(error) => return Err(error),
            }
        }
    }
}

/// Accepted bytes are written in pieces of at least this size.
const WRITE_BUFFER_BYTES: usize = 1024 * 1024;

/// Write the accepted bytes at their place. A write that does not finish
/// leaves them unaccepted: the range continues from where they started.
async fn write_accepted(
    record: &RangeRecord,
    inactivity: &Inactivity,
    offset: &mut u64,
    buffer: &mut Vec<u8>,
) -> Result<(), ClientError> {
    let at = *offset - buffer.len() as u64;
    let written = inactivity
        .bound(record.write(at, std::mem::take(buffer)))
        .await
        .map_err(ClientError::from)
        .and_then(|written| written);
    if written.is_err() {
        *offset = at;
    }
    written
}

/// One object's transfer, shared by the ranges that move it at once.
struct RangeTransfer<'a, F> {
    url: Url,
    sha256: &'a str,
    total: u64,
    record: &'a RangeRecord,
    governor: &'a StreamGovernor,
    accepted: Mutex<Accepted<F>>,
}

/// Bytes accepted across an object's ranges; progress is reported from them,
/// at most every 200 ms, so it only grows however the ranges interleave.
struct Accepted<F> {
    bytes: u64,
    reported: tokio::time::Instant,
    progress: F,
}

impl<F: FnMut(u64, ProgressPhase)> Accepted<F> {
    fn add(&mut self, count: u64) {
        self.bytes += count;
        if self.reported.elapsed() >= Duration::from_millis(200) {
            self.report();
        }
    }

    fn report(&mut self) {
        (self.progress)(self.bytes, ProgressPhase::Copying);
        self.reported = tokio::time::Instant::now();
    }
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod recovery_tests;

#[cfg(test)]
mod inactivity_tests;

#[cfg(test)]
mod range_tests;
