//! Retained job exits.
//!
//! Docker keeps a job's exit only in its container, and the helper removes an
//! exited job container in three places: its own Start's wait, the stray sweep
//! at helper start, and before its run's next job. An agent that did not
//! receive the exit (its call broke, it restarted, its own record is gone)
//! re-observes the job by its identity; with the container removed it would
//! find it absent, and the exit was only in the helper's journal. So the exit
//! Docker recorded is retained before the container is removed, as the answer
//! a re-observation of the live container would have given, and a
//! re-observation that finds the container absent answers with it.
//!
//! A record is named by the digest of the job's whole identity, so only a
//! re-observation of exactly that job finds it. Records are kept a day.

use super::*;
use vonk_agent_protocol::generated::HostHelperResponse;

const DIRECTORY: &str = "job-exits";
const RETENTION: Duration = Duration::from_secs(24 * 60 * 60);
const MAX_RECORD_BYTES: u64 = 256 * 1024;

/// The record's name: the digest of the job's whole identity.
fn record_name(identity: RuntimeEffectIdentity, run_id: uuid::Uuid, plan_digest: &str) -> String {
    let key = format!(
        "{}\t{run_id}\t{}\t{}\t{plan_digest}",
        identity.runtime_id, identity.installation_id, identity.run_generation
    );
    format!("{}.json", hex_sha256(key.as_bytes()))
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// Retain the exit Docker recorded for one job container before it is
    /// removed: its code, and for a failure its exit account and output. A
    /// record that cannot be written is journaled; the removal goes on.
    pub(super) fn retain_job_exit(
        &self,
        identity: RuntimeEffectIdentity,
        run_id: uuid::Uuid,
        plan_digest: &str,
        exit_code: i32,
        evidence: Option<&JobEvidence>,
    ) {
        let Some(code) = u32::try_from(exit_code).ok().filter(|code| *code <= 255) else {
            return;
        };
        let failed = code != 0;
        let record = HostHelperResponse {
            schema_version: 1,
            request_id: None,
            status: HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
            process_running: Some(false),
            job_progress_at: None,
            exit_code: Some(code),
            error_code: None,
            diagnostic: evidence
                .filter(|_| failed)
                .map(|evidence| evidence.summary.clone()),
            process_logs: evidence
                .filter(|_| failed)
                .and_then(|evidence| evidence.logs.as_deref().cloned()),
            installation_intent_nonce: None,
        };
        let name = record_name(identity, run_id, plan_digest);
        if let Err(error) = self.write_job_exit(&name, &record) {
            eprintln!(
                "vonk-agent-helper: job {} exited {code}; its exit is not retained beyond this journal: {error}",
                identity.runtime_id
            );
        }
    }

    fn write_job_exit(
        &self,
        name: &str,
        record: &HostHelperResponse,
    ) -> Result<(), OperationError> {
        let root = self.roots.data.join(DIRECTORY);
        ensure_runtime_directory(&root)?;
        let bytes = canonical_json(record).map_err(|_| OperationError::InvalidOperation)?;
        let temporary = root.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
        let written = (|| {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
                .mode(0o600)
                .open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temporary, root.join(name))?;
            sync_directory(&root)
        })();
        if written.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        prune_job_exits(&root);
        written
    }

    /// The retained exit of exactly this job, as a re-observation answers it:
    /// output and exit account only for a failure, and only when asked.
    pub(super) fn retained_job_exit(
        &self,
        identity: RuntimeEffectIdentity,
        run_id: uuid::Uuid,
        plan_digest: &str,
        include_logs: bool,
    ) -> Option<RunInspection> {
        let path = self
            .roots
            .data
            .join(DIRECTORY)
            .join(record_name(identity, run_id, plan_digest));
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
            )
            .open(path)
            .ok()?;
        if !file.metadata().ok()?.is_file() {
            return None;
        }
        let mut bytes = Vec::new();
        Read::by_ref(&mut file)
            .take(MAX_RECORD_BYTES + 1)
            .read_to_end(&mut bytes)
            .ok()?;
        if bytes.len() as u64 > MAX_RECORD_BYTES {
            return None;
        }
        let record: HostHelperResponse = parse_strict(&bytes).ok()?;
        let code = record.exit_code.filter(|code| *code <= 255)?;
        if record.process_running != Some(false) {
            return None;
        }
        let failed = include_logs && code != 0;
        Some(RunInspection {
            running: false,
            job_progress_at: None,
            exit_code: Some(i32::try_from(code).ok()?),
            logs: record.process_logs.filter(|_| failed).map(Box::new),
            log_error: record.diagnostic.filter(|_| failed),
        })
    }
}

/// Remove records older than the retention. Best effort: a record that
/// cannot be read or removed is tried again at the next write.
fn prune_job_exits(root: &Path) {
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let expired = entry
            .metadata()
            .ok()
            .filter(fs::Metadata::is_file)
            .and_then(|metadata| metadata.modified().ok())
            .and_then(|modified| modified.elapsed().ok())
            .is_some_and(|age| age > RETENTION);
        if expired {
            let _ = fs::remove_file(entry.path());
        }
    }
}

#[cfg(test)]
mod tests;
