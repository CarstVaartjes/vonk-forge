pub use crate::generated::{
    FailureDiagnostics, FailureDiagnosticsCategory as FailureCategory, FailureLogTail,
    FailureProperty,
};

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
