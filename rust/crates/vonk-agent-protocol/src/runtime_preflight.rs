//! Typed, bounded runtime preflight evidence. The request contains no commands.
pub use crate::generated::{
    RuntimePreflightFinding, RuntimePreflightFindingStatus as RuntimePreflightStatus,
    RuntimePreflightRequest, RuntimePreflightResult,
};
use crate::{ProtocolError, canonical_json, hex_sha256};

fn capability_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 96
        && value.as_bytes()[0].is_ascii_lowercase()
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.-".contains(&byte)
        })
}

impl RuntimePreflightRequest {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || !matches!(self.architecture.as_str(), "linux-arm64" | "linux-amd64")
            || !matches!(
                self.fabric_connectivity.as_str(),
                "none" | "connected" | "full_mesh" | "switch"
            )
            || self.mandatory_capabilities.len() > 64
            || self
                .mandatory_capabilities
                .iter()
                .enumerate()
                .any(|(index, value)| {
                    !capability_name(value) || self.mandatory_capabilities[..index].contains(value)
                })
        {
            return Err(ProtocolError::Identity("runtime preflight request"));
        }
        Ok(())
    }
    pub fn digest(&self) -> Result<String, ProtocolError> {
        Ok(hex_sha256(&canonical_json(self)?))
    }
}

impl RuntimePreflightResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        let digest = |value: &str| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        };
        if self.schema_version != 1
            || !digest(&self.fingerprint)
            || !digest(&self.request_sha256)
            || self.duration_ms >= 60000
            || self.findings.len() > 96
            || self.findings.iter().enumerate().any(|(index, value)| {
                !capability_name(&value.capability)
                    || value.code.is_empty()
                    || value.code.len() > 96
                    || !value.code.as_bytes()[0].is_ascii_lowercase()
                    || !value.code.bytes().all(|byte| {
                        byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.-".contains(&byte)
                    })
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
            schema_version: 1,
            action: HostRuntimeAction::RuntimePreflight,
            job_id: uuid::Uuid::new_v4(),
            operation_id: uuid::Uuid::new_v4(),
            attempt: 1,
            fence: uuid::Uuid::new_v4(),
            arguments: vec![],
            observation: None,
            installation_id: None,
        };
        assert!(request.validate().is_ok());
        request.arguments = vec!["--privileged".into()];
        assert!(request.validate().is_err());
        request.arguments.clear();
        request.action = HostRuntimeAction::Start;
        assert!(request.validate().is_err());
    }
}
