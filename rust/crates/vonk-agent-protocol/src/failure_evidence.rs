use crate::generated::FailureLogPolicy;
pub use crate::generated::{
    FailureDiagnostics, FailureDiagnosticsCategory as FailureCategory, FailureLogTail,
    FailureProperty,
};

include!(concat!(env!("OUT_DIR"), "/failure_log_policy.rs"));

/// Bytes retained per failure log stream, from `FailureLogPolicy` in the Python
/// protocol: the helper, the agent and the Controller bound a log by this one number.
#[expect(
    clippy::cast_possible_truncation,
    reason = "a small policy constant; usize is 64-bit on every supported target and `try_from` is not const"
)]
pub const LOG_TAIL_BYTES: usize = FAILURE_LOG_POLICY.log_tail_bytes as usize;

/// The field of a process-exit summary that carries the uncaught exception
/// the workload printed last, as free text.  It is always the summary's final
/// field, so a reader takes the facts before it as `key=value` tokens and the
/// text after it as the workload's own words.
pub const EXIT_SUMMARY_EXCEPTION_FIELD: &str = " exception=";

/// The key of a process-exit summary's cause: `exit_cause=<ExitCause token>`.
pub const EXIT_CAUSE_FIELD: &str = "exit_cause";

/// The key of a process-exit summary's kernel OOM-kill fact (`oom_killed=<bool>`).
pub const OOM_KILLED_FIELD: &str = "oom_killed";

/// How many preflight properties a diagnostics document holds (its wire bound).
pub const MAX_PREFLIGHT_PROPERTIES: usize = 8;
/// The longest value one failure property carries (the wire contract's bound).
pub const FAILURE_PROPERTY_VALUE_CHARS: usize = 256;
/// A process-exit summary's bound on the helper's reply.
pub const EXIT_SUMMARY_CHARS: usize = 480;
/// A host-memory-guard summary is all facts, each its own failure property:
/// bounded by those properties, not cut at `EXIT_SUMMARY_CHARS`.
pub const HOST_MEMORY_SUMMARY_CHARS: usize =
    MAX_PREFLIGHT_PROPERTIES * (64 + 2 + FAILURE_PROPERTY_VALUE_CHARS);

/// Why a container exited, as a stable token the Controller can show and
/// group. The helper names it; the agent decides by it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExitCause {
    OomKilled,
    HostMemoryExhausted,
    BadArguments,
    MissingFile,
    UnsupportedGpuArch,
    ImageArchMismatch,
    StartRefused,
    CrashedBySignal,
    PythonException,
    Unclassified,
}

impl ExitCause {
    const ALL: [Self; 10] = [
        Self::OomKilled,
        Self::HostMemoryExhausted,
        Self::BadArguments,
        Self::MissingFile,
        Self::UnsupportedGpuArch,
        Self::ImageArchMismatch,
        Self::StartRefused,
        Self::CrashedBySignal,
        Self::PythonException,
        Self::Unclassified,
    ];

    pub fn token(self) -> &'static str {
        match self {
            Self::OomKilled => "oom_killed",
            Self::HostMemoryExhausted => "host_memory_exhausted",
            Self::BadArguments => "bad_arguments",
            Self::MissingFile => "missing_file",
            Self::UnsupportedGpuArch => "unsupported_gpu_arch",
            Self::ImageArchMismatch => "image_arch_mismatch",
            Self::StartRefused => "start_refused",
            Self::CrashedBySignal => "crashed_by_signal",
            Self::PythonException => "python_exception",
            Self::Unclassified => "unclassified",
        }
    }

    /// The cause a stable token names.
    pub fn from_token(token: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|cause| cause.token() == token)
    }

    /// The process ended without running its own cleanup: the host memory
    /// guard or the kernel's OOM killer sent it SIGKILL (`host_memory_exhausted`,
    /// `oom_killed`: the runtime's `OOMKilled` fact), or it ended by a signal
    /// (`crashed_by_signal`: SIGABRT/SIGBUS/SIGKILL/SIGSEGV, exit
    /// 134/135/137/139; a bare 137 is not proof of OOM). What it left behind is a
    /// consequence of that ending, not something it chose to produce. A
    /// SIGTERM (143) is `unclassified`: the process could clean up.
    pub fn skipped_cleanup(self) -> bool {
        matches!(
            self,
            Self::HostMemoryExhausted | Self::OomKilled | Self::CrashedBySignal
        )
    }

    /// The cause a process-exit summary names, read from its facts only:
    /// the workload's exception after them may itself read `exit_cause=...`.
    pub fn of_summary(summary: &str) -> Option<Self> {
        summary
            .split(EXIT_SUMMARY_EXCEPTION_FIELD)
            .next()?
            .split_whitespace()
            .find_map(|fact| fact.strip_prefix(EXIT_CAUSE_FIELD)?.strip_prefix('='))
            .and_then(Self::from_token)
    }
}

/// The line a retained log tail carries between the failure lines kept from
/// above its window and the window itself, naming the source bytes omitted.
/// A reader that has to shrink such a tail keeps both sides of it: the lines
/// before it are the cause, the lines after it are how the process ended.
/// The agent's `clamp_retained` and the Controller's `_keep_retained`
/// (`control/src/vonk_control/failure_evidence.py`) are those readers.
pub fn omission_marker(dropped: u64) -> String {
    format!("[{dropped} bytes omitted]")
}

/// True when `line` is an [`omission_marker`].
pub fn is_omission_marker(line: &str) -> bool {
    line.strip_prefix('[')
        .and_then(|rest| rest.strip_suffix(" bytes omitted]"))
        .is_some_and(|count| !count.is_empty() && count.bytes().all(|b| b.is_ascii_digit()))
}

impl FailureDiagnostics {
    pub fn validate(&self) -> Result<(), crate::ProtocolError> {
        let properties = |values: &[FailureProperty], maximum: usize| {
            values.len() <= maximum
                && values.iter().all(|value| {
                    !value.name.is_empty()
                        && value.name.chars().count() <= 64
                        && value.value.chars().count() <= FAILURE_PROPERTY_VALUE_CHARS
                })
        };
        let valid = serde_json::to_vec(self).is_ok_and(|bytes| {
            u64::try_from(bytes.len())
                .is_ok_and(|length| length <= FAILURE_LOG_POLICY.document_bytes)
        }) && self.collected_at.chars().count() <= 64
            && self.process_exit_code.is_none_or(|code| code <= 255)
            && self.schema_version == 1
            && chrono::DateTime::parse_from_rfc3339(&self.collected_at).is_ok()
            && !self.phase.is_empty()
            && self.phase.chars().count() <= 80
            && self.stdout.text.chars().count() <= LOG_TAIL_BYTES
            && self.stderr.text.chars().count() <= LOG_TAIL_BYTES
            && properties(&self.versions, 8)
            && properties(&self.sandbox, 12)
            && properties(&self.storage, 8)
            && properties(&self.preflight, MAX_PREFLIGHT_PROPERTIES)
            && self.collector_errors.len() <= 8
            && self
                .collector_errors
                .iter()
                .all(|value| value.chars().count() <= FAILURE_PROPERTY_VALUE_CHARS);
        if valid {
            Ok(())
        } else {
            Err(crate::ProtocolError::Identity(
                "failure diagnostics are invalid",
            ))
        }
    }
}

#[cfg(test)]
mod tests;
