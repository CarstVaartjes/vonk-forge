//! Observation for the oci boundary.

use super::*;

impl<R: ProcessRunner> OciRuntime<'_, R> {
    /// Compatibility callers own a finite aggregate budget. Production scans
    /// persist one bounded page at a time instead of awaiting a whole history.
    pub fn recipe_run_inspection_plans(
        &self,
        deadline: Instant,
    ) -> Result<Vec<RecipeRunInspectionPlan>, OciError> {
        let runs = self.data_root.join("runs");
        let metadata_root = self.data_root.join("run-metadata");
        let runs_stamp = observation_directory_stamp(&runs)?;
        let metadata_stamp = observation_directory_stamp(&metadata_root)?;
        let mut plans = Vec::new();
        let mut checkpoint = None;
        let mut failure = None;
        loop {
            if Instant::now() >= deadline {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "run inspection aggregate budget expired",
                )
                .into());
            }
            let page = self.recipe_run_inspection_page(checkpoint.as_ref())?;
            if Instant::now() >= deadline {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "run inspection aggregate budget expired",
                )
                .into());
            }
            if page.scan_restarted {
                if plans.is_empty() && page.plans.is_empty() {
                    if let Some(error) = failure.take() {
                        return Err(error);
                    }
                    if let Some(fault) = page.failures.into_iter().next() {
                        return Err(fault.error);
                    }
                }
                return Err(std::io::Error::new(
                    std::io::ErrorKind::WouldBlock,
                    "run inspection enumeration restarted during collection",
                )
                .into());
            }
            plans.extend(page.plans);
            for fault in page.failures {
                failure.get_or_insert(fault.error);
            }
            checkpoint = page.checkpoint;
            if page.complete {
                // This is a collection of individually validated positive
                // plans: malformed neighbors remain isolated, not authoritative
                // absence. Changed enumeration authority cannot be accepted.
                if plans.is_empty()
                    && let Some(error) = failure.take()
                {
                    return Err(error);
                }
                if observation_directory_stamp(&runs)? != runs_stamp
                    || observation_directory_stamp(&metadata_root)? != metadata_stamp
                    || (plans.is_empty() && !page.empty_snapshot_safe)
                {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::WouldBlock,
                        "run inspection coverage changed during collection",
                    )
                    .into());
                }
                break;
            }
        }
        if plans.is_empty()
            && let Some(error) = failure
        {
            return Err(error);
        }
        Ok(plans)
    }

    /// Cookies are opaque filesystem positions, not sorted UUID offsets. Before
    /// resuming across a process restart, replay the preceding entry and verify
    /// its exact name, inode and next cookie. Mutation invalidates coverage but
    /// does not rewind a verified position and starve the history tail.
    pub fn recipe_run_inspection_page(
        &self,
        checkpoint: Option<&RecipeRunObservationCheckpoint>,
    ) -> Result<RecipeRunInspectionPage, OciError> {
        let observed_at = chrono::Utc::now();
        let runs = self.data_root.join("runs");
        let metadata_root = self.data_root.join("run-metadata");
        let Some(stamp) = observation_directory_stamp(&runs)? else {
            return Ok(RecipeRunInspectionPage {
                plans: vec![],
                failures: vec![],
                checkpoint: None,
                observed_at,
                complete: true,
                empty_snapshot_safe: checkpoint.is_none(),
                scan_restarted: checkpoint.is_some(),
            });
        };
        let file = open_observation_directory(&runs, &stamp)?;
        let mut directory = rustix::fs::Dir::new(file).map_err(std::io::Error::from)?;
        let metadata_stamp = observation_directory_stamp(&metadata_root)?;
        let mut scan_restarted = checkpoint.is_some_and(|old| {
            Path::new(&old.root) != runs || !same_observation_directory(&old.runs_stamp, &stamp)
        });
        let mut progress = match checkpoint {
            Some(old)
                if Path::new(&old.root) == runs
                    && same_observation_directory(&old.runs_stamp, &stamp) =>
            {
                old.clone()
            }
            _ => RecipeRunObservationCheckpoint {
                root: runs.to_str().ok_or(OciError::Artifact)?.to_owned(),
                runs_stamp: stamp.clone(),
                metadata_stamp: metadata_stamp.clone(),
                started_at: observed_at.to_rfc3339_opts(chrono::SecondsFormat::AutoSi, true),
                witness: None,
                had_plans: false,
                had_failures: checkpoint.is_some(),
            },
        };
        let cutoff = chrono::DateTime::parse_from_rfc3339(&progress.started_at)
            .map_err(|_| OciError::Artifact)?
            .with_timezone(&chrono::Utc);
        progress.had_failures |=
            progress.runs_stamp != stamp || progress.metadata_stamp != metadata_stamp;
        if let Some(witness) = &progress.witness {
            directory
                .seek(witness.before)
                .map_err(std::io::Error::from)?;
            let verified = matches!(directory.next(), Some(Ok(ref entry))
                if entry.offset() == witness.after && entry.file_name().to_bytes() == witness.name
                && format!("{:x}", entry.ino()) == witness.inode);
            if !verified {
                directory.seek(0).map_err(std::io::Error::from)?;
                progress.witness = None;
                progress.had_failures = true;
                scan_restarted = true;
            }
        }
        let mut plans = Vec::new();
        let mut failures = Vec::new();
        let mut bytes = 0usize;
        let started = Instant::now();
        let mut complete = false;
        for _ in 0..MAX_RUN_DIRECTORY_ENTRIES_PER_PAGE {
            if plans.len() + failures.len() >= MAX_RUN_INSPECTIONS_PER_PAGE
                || bytes >= MAX_RUN_INSPECTION_PAGE_BYTES
                || started.elapsed() >= RUN_INSPECTION_PAGE_BUDGET
            {
                break;
            }
            let before = progress.witness.as_ref().map_or(0, |entry| entry.after);
            let entry = match directory.next() {
                None => {
                    complete = true;
                    break;
                }
                Some(Err(error)) => return Err(std::io::Error::from(error).into()),
                Some(Ok(entry)) => entry,
            };
            let name = entry.file_name().to_bytes();
            progress.witness = Some(ObservationCursorWitness {
                before,
                after: entry.offset(),
                name: name.to_vec(),
                inode: format!("{:x}", entry.ino()),
            });
            if name == b"." || name == b".." {
                continue;
            }
            let run_id = std::str::from_utf8(name)
                .ok()
                .filter(|id| canonical_uuid(id));
            if entry.file_type() != rustix::fs::FileType::Directory || run_id.is_none() {
                progress.had_failures = true;
                failures.push(RecipeRunInspectionFailure {
                    run_id: None,
                    error: OciError::Artifact,
                });
                continue;
            }
            let run_id = run_id.expect("validated UUID");
            match self.recipe_run_inspection_plan(run_id) {
                Ok(Some(plan)) => {
                    bytes += plan.arguments.iter().map(String::len).sum::<usize>()
                        + plan.health_path.len();
                    progress.had_plans = true;
                    plans.push(plan);
                }
                Ok(None) => {}
                Err(error) => {
                    progress.had_failures = true;
                    failures.push(RecipeRunInspectionFailure {
                        run_id: Some(run_id.to_owned()),
                        error,
                    });
                }
            }
        }
        progress.had_failures |= observation_directory_stamp(&runs)?
            != Some(progress.runs_stamp.clone())
            || observation_directory_stamp(&metadata_root)? != progress.metadata_stamp;
        let empty_snapshot_safe = complete
            && !progress.had_plans
            && !progress.had_failures
            && observed_at - cutoff <= MAX_EMPTY_SCAN_AGE;
        Ok(RecipeRunInspectionPage {
            plans,
            failures,
            observed_at: if empty_snapshot_safe {
                cutoff
            } else {
                observed_at
            },
            checkpoint: if complete { None } else { Some(progress) },
            complete,
            empty_snapshot_safe,
            scan_restarted,
        })
    }

    pub(super) fn recipe_run_inspection_plan(
        &self,
        run_id: &str,
    ) -> Result<Option<RecipeRunInspectionPlan>, OciError> {
        // Stopped run directories intentionally outlive their lifecycle. A
        // missing lifecycle or run generation is historical; malformed
        // metadata is returned to the caller as this run's isolated failure.
        let Some((spec, installation_id, placement, Some(run_generation))) =
            self.load_run_lifecycle(run_id)?
        else {
            return Ok(None);
        };
        let retained = self.prepare_retained_start(&spec, &installation_id, run_id, &placement)?;
        let mut arguments = vec![
            retained.archive_sha256.clone(),
            retained.registry_index_digest.clone(),
            retained.platform_manifest_digest.clone(),
            retained.image_reference.clone(),
        ];
        arguments.extend(retained.main);
        let endpoint_owner =
            placement.world_size == 1 || placement.local_address == placement.master_address;
        let health_path = spec
            .endpoint
            .as_ref()
            .ok_or(OciError::Artifact)?
            .health_path
            .clone();
        Ok(Some(RecipeRunInspectionPlan {
            run_id: uuid::Uuid::parse_str(run_id).map_err(|_| OciError::Artifact)?,
            run_generation,
            arguments,
            endpoint_address: if endpoint_owner {
                Some(placement.endpoint_address.ok_or(OciError::Artifact)?)
            } else {
                None
            },
            endpoint_port: placement.port.ok_or(OciError::Artifact)?,
            health_path,
        }))
    }

    pub(super) fn load_run_lifecycle(
        &self,
        run_id: &str,
    ) -> Result<Option<LoadedRunLifecycle>, OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        let path = metadata.join("lifecycle.json");
        let Some(record) = self.read_run_lifecycle(&path)? else {
            return Ok(None);
        };
        record.placement.validate_bound()?;
        if !canonical_uuid(&record.installation_id) {
            return Err(OciError::Artifact);
        }
        managed_path(self.data_root, "installations", &record.installation_id)?;
        let spec: CompiledExecutionPlan = serde_json::from_slice(&read_regular_file(
            &metadata.join("runtime.json"),
            MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES as u64,
        )?)?;
        spec.validate()?;
        let installed = self.load_spec(&record.installation_id)?;
        let matches = if spec.job.is_some() {
            same_job_workload(&installed, &spec)
        } else {
            same_installed_workload(&installed, &spec)
        };
        if !matches || spec.runtime.placement != record.placement {
            return Err(OciError::Artifact);
        }
        Ok(Some((
            spec,
            record.installation_id,
            record.placement,
            record.run_generation,
        )))
    }

    pub(super) fn read_run_lifecycle(&self, path: &Path) -> Result<Option<RunLifecycle>, OciError> {
        let metadata = match fs::symlink_metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() > 16 * 1024
        {
            return Err(OciError::Artifact);
        }
        let record: RunLifecycle = serde_json::from_slice(&read_regular_file(path, 16 * 1024)?)?;
        if record
            .run_generation
            .is_some_and(|generation| generation > i64::MAX as u64)
        {
            return Err(OciError::Artifact);
        }
        Ok(Some(record))
    }

    pub(super) fn run_metadata_path(&self, run_id: &str) -> Result<PathBuf, OciError> {
        if !canonical_uuid(run_id) {
            return Err(OciError::Artifact);
        }
        Ok(self.data_root.join("run-metadata").join(run_id))
    }

    pub(super) fn ensure_run_metadata(&self, run_id: &str) -> Result<PathBuf, OciError> {
        let root = self.data_root.join("run-metadata");
        fs::create_dir_all(&root)?;
        let root_metadata = fs::symlink_metadata(&root)?;
        if !root_metadata.file_type().is_dir() || root_metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let metadata = self.run_metadata_path(run_id)?;
        match fs::create_dir(&metadata) {
            Ok(()) => fs::set_permissions(&metadata, fs::Permissions::from_mode(0o700))?,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                let metadata_type = fs::symlink_metadata(&metadata)?.file_type();
                if !metadata_type.is_dir() || metadata_type.is_symlink() {
                    return Err(OciError::Artifact);
                }
            }
            Err(error) => return Err(error.into()),
        }
        Ok(metadata)
    }
}

#[cfg(test)]
mod tests;
