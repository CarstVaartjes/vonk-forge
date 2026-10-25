pub use crate::generated::{
    FailureDiagnostics, FailureDiagnosticsCategory as FailureCategory, FailureLogTail,
    FailureProperty,
};

/// The field of a process-exit summary that carries the uncaught exception
/// the workload printed last, as free text.  It is always the summary's final
/// field, so a reader takes the facts before it as `key=value` tokens and the
/// text after it as the workload's own words.
pub const EXIT_SUMMARY_EXCEPTION_FIELD: &str = " exception=";

/// The key of a process-exit summary's cause: `exit_cause=<ExitCause token>`.
pub const EXIT_CAUSE_FIELD: &str = "exit_cause";

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

    /// The cause a process-exit summary names, read from its facts only:
    /// the workload's exception after them may itself read `exit_cause=...`.
    pub fn of_summary(summary: &str) -> Option<Self> {
        summary
            .split(EXIT_SUMMARY_EXCEPTION_FIELD)
            .next()?
            .split_whitespace()
            .find_map(|fact| fact.strip_prefix(EXIT_CAUSE_FIELD)?.strip_prefix('='))
            .and_then(|token| Self::ALL.into_iter().find(|cause| cause.token() == token))
    }
}

/// The line a retained log tail carries between the failure lines kept from
/// above its window and the window itself, naming the source bytes omitted.
/// A reader that has to shrink such a tail keeps both sides of it: the lines
/// before it are the cause, the lines after it are how the process ended.
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
                        && value.value.chars().count() <= 256
                })
        };
        let valid = serde_json::to_vec(self).is_ok_and(|bytes| bytes.len() <= 16 * 1024)
            && self.collected_at.chars().count() <= 64
            && self.process_exit_code.is_none_or(|code| code <= 255)
            && self.schema_version == 1
            && chrono::DateTime::parse_from_rfc3339(&self.collected_at).is_ok()
            && !self.phase.is_empty()
            && self.phase.chars().count() <= 80
            && self.stdout.text.chars().count() <= 2048
            && self.stderr.text.chars().count() <= 2048
            && properties(&self.versions, 8)
            && properties(&self.sandbox, 12)
            && properties(&self.storage, 8)
            && properties(&self.preflight, 8)
            && self.collector_errors.len() <= 8
            && self
                .collector_errors
                .iter()
                .all(|value| value.chars().count() <= 256);
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
mod tests {
    use super::FailureDiagnostics;

    #[test]
    fn failure_diagnostics_enforce_utf8_byte_bound() {
        let mut value: FailureDiagnostics = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/failure-diagnostics-v1.json"
        ))
        .unwrap();
        value.stdout.text = "😀".repeat(2048);
        value.stderr.text = "😀".repeat(2048);
        assert!(value.validate().is_err());
    }

    #[test]
    fn canonical_python_failure_diagnostics_vector_round_trips() {
        let document: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/failure-diagnostics-v1.json"
        ))
        .unwrap();
        let diagnostics: FailureDiagnostics = serde_json::from_value(document.clone()).unwrap();
        diagnostics.validate().unwrap();
        assert_eq!(serde_json::to_value(diagnostics).unwrap(), document);
    }
}
