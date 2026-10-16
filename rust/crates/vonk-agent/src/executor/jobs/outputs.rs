//! The outputs an exited job left in its output root.
//!
//! Collection runs after the job's exit is receipted, so the files it reads
//! no longer change. Outputs that break the job's signed output contract (too
//! many, too large, an undeclared or refused media type, an entry that is not
//! a plain file) are the same on every later look: the job ends failed at
//! once, naming the rule, its bound and what the job left. Only an output
//! root or a file that could not be read leaves the effect unobserved.

use super::*;

/// How many output-root entries a refusal names; the count is always exact.
const NAMED_ENTRIES: usize = 8;

/// Why the outputs of an exited job were not accepted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(in crate::executor) enum OutputRefusal {
    /// The job's own outputs break its signed output contract.
    Breach(OutputBreach),
    /// The output root or a file in it could not be read whole; another look
    /// may succeed.
    Unreadable(&'static str),
}

/// The output-contract rule a job broke, measured where it has a bound, and
/// the entries the job left.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(in crate::executor) struct OutputBreach {
    pub(in crate::executor) cause: &'static str,
    pub(in crate::executor) rule: &'static str,
    /// The bound and the measured value, for a rule that has one.
    pub(in crate::executor) bound: Option<(u64, u64)>,
    /// The entry that broke the rule, for a rule about one entry.
    pub(in crate::executor) entry: Option<String>,
    /// Every entry the job left in its output root, sorted.
    pub(in crate::executor) entries: Vec<String>,
}

impl OutputBreach {
    /// The definite failure of a job whose outputs broke their contract. The
    /// exit and the process logs travel with it; nothing is uploaded.
    pub(in crate::executor) fn failure(self, receipt: &RecipeJobRunResult) -> ExecutionResult {
        let mut diagnostic = format!("exit_code={} rule={}", receipt.exit_code, self.rule);
        if let Some((limit, observed)) = self.bound {
            diagnostic.push_str(&format!(" limit={limit} observed={observed}"));
        }
        if let Some(entry) = &self.entry {
            diagnostic.push_str(&format!(" entry={entry}"));
        }
        let named = self.entries[..self.entries.len().min(NAMED_ENTRIES)].join(",");
        diagnostic.push_str(&format!(" entries={} [{named}]", self.entries.len()));
        let reason = if receipt.exit_code == 0 {
            format!(
                "job outputs break the signed output contract: {}",
                self.cause
            )
        } else {
            format!(
                "job adapter exited unsuccessfully and its outputs break the signed output contract: {}",
                self.cause
            )
        };
        let logs = receipt.diagnostics.as_ref().map(|diagnostics| {
            crate::failure_evidence::FailureProcessLogs {
                stdout: diagnostics.stdout.clone(),
                stderr: diagnostics.stderr.clone(),
            }
        });
        ExecutionResult::Failed(
            Failure::new(reason)
                .code(FailureCode::RecipeJobRunFailed)
                .kind(AgentFailureKind::InvalidContract)
                .stage(FailureStage::OutputStorage)
                .diagnostic(diagnostic)
                .refusal_bound(self.bound.map(|(limit, observed)| RefusalBound {
                    rule: self.rule.to_owned(),
                    limit: Some(limit),
                    observed,
                }))
                .process_logs(logs),
        )
    }
}

pub(in crate::executor) fn collect_job_outputs(
    root: Option<&Path>,
    limits: &RecipeJobOutputLimits,
    mappings: &[RecipeJobOutputMapping],
) -> Result<RecipeJobOutputManifest, OutputRefusal> {
    let unavailable = OutputRefusal::Unreadable("job output directory is unavailable");
    let unreadable = OutputRefusal::Unreadable("job output could not be read");
    let root = root.ok_or_else(|| unavailable.clone())?;
    let mut entries = fs::read_dir(root)
        .map_err(|_| unavailable.clone())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| unavailable.clone())?;
    // Runtime scratch and the mountpoints of runtime mounts nested in the
    // output root (the cache) are owned separately from exported job files.
    // Preserve them for lifecycle cleanup; they are never an output slot or
    // count claim.
    entries.retain(|entry| {
        let name = entry.file_name();
        let runtime_owned = name == "tmp"
            || vonk_agent_protocol::compiled_oci::runtime_output_mountpoints()
                .any(|mountpoint| name == mountpoint);
        !runtime_owned || !entry.file_type().is_ok_and(|kind| kind.is_dir())
    });
    entries.sort_by_key(fs::DirEntry::file_name);
    let left = entries
        .iter()
        .map(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .chars()
                .take(64)
                .collect()
        })
        .collect::<Vec<String>>();
    let breach = |cause, rule, bound, entry: Option<&str>| {
        OutputRefusal::Breach(OutputBreach {
            cause,
            rule,
            bound,
            entry: entry.map(|name| name.chars().take(64).collect()),
            entries: left.clone(),
        })
    };
    let max_files = u64::from(limits.max_files);
    if entries.len() as u64 > max_files {
        return Err(breach(
            "job output file count exceeded its bound",
            "job_output_max_files",
            Some((max_files, entries.len() as u64)),
            None,
        ));
    }
    let mut files = Vec::with_capacity(entries.len());
    let mut total_bytes = 0_u64;
    for (entry, shown) in entries.into_iter().zip(&left) {
        let file_type = entry.file_type().map_err(|_| unreadable.clone())?;
        let name = entry.file_name().into_string().map_err(|_| {
            breach(
                "job output name is invalid",
                "job_output_name",
                None,
                Some(shown),
            )
        })?;
        if !file_type.is_file() || file_type.is_symlink() || !valid_job_output_name(&name) {
            return Err(breach(
                "job output is not a plain file with a valid name",
                "job_output_entry",
                None,
                Some(&name),
            ));
        }
        let metadata = entry.metadata().map_err(|_| unreadable.clone())?;
        let max_file_bytes = u64::from(limits.max_file_bytes);
        if metadata.len() > max_file_bytes {
            return Err(breach(
                "job output file size exceeded its bound",
                "job_output_max_file_bytes",
                Some((max_file_bytes, metadata.len())),
                Some(&name),
            ));
        }
        let max_total_bytes = u64::from(limits.max_total_bytes);
        total_bytes = total_bytes.saturating_add(metadata.len());
        if total_bytes > max_total_bytes {
            return Err(breach(
                "job output total size exceeded its bound",
                "job_output_max_total_bytes",
                Some((max_total_bytes, total_bytes)),
                Some(&name),
            ));
        }
        let Some(media_type) = output_media_type(&name, mappings) else {
            return Err(breach(
                "job output media type is not declared by its signed slot mapping",
                "job_output_media_type",
                None,
                Some(&name),
            ));
        };
        if !limits
            .allowed_media_types
            .iter()
            .any(|allowed| allowed == media_type)
        {
            return Err(breach(
                "job output media type is not allowed",
                "job_output_allowed_media_type",
                None,
                Some(&name),
            ));
        }
        let mut file = File::open(entry.path()).map_err(|_| unreadable.clone())?;
        let mut hasher = Sha256::new();
        let mut observed = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let read = file.read(&mut buffer).map_err(|_| unreadable.clone())?;
            if read == 0 {
                break;
            }
            observed = observed
                .checked_add(read as u64)
                .filter(|bytes| *bytes <= metadata.len())
                .ok_or(OutputRefusal::Unreadable(
                    "job output changed while it was collected",
                ))?;
            hasher.update(&buffer[..read]);
        }
        if observed != metadata.len() {
            return Err(OutputRefusal::Unreadable(
                "job output changed while it was collected",
            ));
        }
        files.push(RecipeJobFile {
            name,
            media_type: media_type.to_owned(),
            // Bounded by `max_file_bytes`, itself a u32.
            size_bytes: u32::try_from(observed).map_err(|_| unreadable.clone())?,
            sha256: hex::encode(hasher.finalize()),
        });
    }
    output_manifest_with_digest(
        vonk_agent_protocol::generated::RecipeJobOutputManifestContent {
            schema_version: 1,
            // Bounded by `max_total_bytes`, itself a u32.
            total_bytes: u32::try_from(total_bytes).map_err(|_| unreadable.clone())?,
            files,
        },
    )
    .map_err(|_| OutputRefusal::Unreadable("job output manifest is invalid"))
}

pub(in crate::executor) fn valid_job_output_name(value: &str) -> bool {
    !value.is_empty()
        && value != "manifest.json"
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

pub(in crate::executor) fn output_media_type<'mapping>(
    name: &str,
    mappings: &'mapping [RecipeJobOutputMapping],
) -> Option<&'mapping str> {
    mappings
        .iter()
        .flat_map(|mapping| {
            mapping
                .extensions
                .iter()
                .map(move |extension| (extension, mapping.media_type.as_str()))
        })
        .filter(|(extension, _)| name.len() > extension.len() && name.ends_with(extension.as_str()))
        .max_by_key(|(extension, _)| extension.len())
        .map(|(_, media_type)| media_type)
}
