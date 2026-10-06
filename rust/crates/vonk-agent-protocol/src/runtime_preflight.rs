//! Typed, bounded runtime preflight evidence. The request contains no commands.
use crate::ProtocolError;
pub use crate::generated::{
    RuntimePreflightFinding, RuntimePreflightFindingCode,
    RuntimePreflightFindingStatus as RuntimePreflightStatus, RuntimePreflightRequest,
    RuntimePreflightResult,
};

fn capability_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 96
        && value.as_bytes()[0].is_ascii_lowercase()
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.-".contains(&byte)
        })
}

impl RuntimePreflightResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        let digest = |value: &str| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        };
        if !digest(&self.fingerprint)
            || self.findings.len() > 96
            || self.findings.iter().enumerate().any(|(index, value)| {
                !capability_name(&value.capability)
                    || value.code.parse::<RuntimePreflightFindingCode>().is_err()
                    || self.findings[..index]
                        .iter()
                        .any(|old| old.capability == value.capability)
            })
        {
            return Err(ProtocolError::Identity("runtime preflight result"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use crate::{HostRuntimeAction, HostRuntimeRequest};
    #[test]
    fn fixed_helper_preflight_cannot_carry_a_command_or_mount_argument() {
        let mut request = HostRuntimeRequest {
            action: HostRuntimeAction::RuntimePreflight,
            fence: uuid::Uuid::new_v4(),
            arguments: vec![],
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: None,
            start_plan: None,
            stop_plan: None,
        };
        assert!(request.validate().is_ok());
        request.arguments = vec!["--privileged".into()];
        assert!(request.validate().is_err());
        request.arguments.clear();
        request.action = HostRuntimeAction::Start;
        assert!(request.validate().is_err());
    }
}
