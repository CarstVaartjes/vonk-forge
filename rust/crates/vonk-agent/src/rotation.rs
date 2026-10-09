use chrono::{DateTime, Utc};
use thiserror::Error;
use vonk_agent_protocol::generated::SecurityRefusalReason;

use crate::{
    client::{AgentHttpClient, ClientError},
    config::AgentConfig,
    identity::{
        IdentityError, IdentityMaterial, active_identity_paths, clear_pending, generate_pending,
        identity_expired, load_pending, persist_pending, publish_staged, renewal_due,
        retire_expired_staged, retire_unavailable_staged, stage_identity, staged_identity_paths,
    },
    pair::{PairingError, validate_issued},
    vocabulary,
};

#[derive(Debug, Error)]
pub enum RotationError {
    #[error("certificate observation ended without a settled effect")]
    ObservationEnded,
    #[error("active agent certificate has expired")]
    ActiveIdentityExpired,
    #[error("credential operation failed: {0}")]
    Client(#[from] ClientError),
    #[error("credential storage failed: {0}")]
    Identity(#[from] IdentityError),
    #[error("issued credential is invalid: {0}")]
    Issued(#[from] PairingError),
}

impl RotationError {
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Identity(_))
            || matches!(self, Self::Client(error) if error.retryable())
    }

    pub fn code(&self) -> String {
        match self {
            Self::ActiveIdentityExpired => SecurityRefusalReason::LocalIdentityExpired.to_string(),
            Self::ObservationEnded => {
                vonk_agent_protocol::generated::WaitReason::ObservationUnavailable.to_string()
            }
            Self::Client(error) => error.code().map(str::to_owned).unwrap_or_else(|| {
                vonk_agent_protocol::generated::ControllerErrorCode::ControllerUnavailable
                    .to_string()
            }),
            Self::Identity(_) => SecurityRefusalReason::LocalIdentityFailed.to_string(),
            Self::Issued(_) => SecurityRefusalReason::AgentIdentityMismatch.to_string(),
        }
    }

    /// Errors that end the agent: the Controller refused this identity
    /// (401/403, including revocation), the pinned server authority changed,
    /// or an issued credential does not belong to this node/key. Local storage
    /// loss ends a bounded observation attempt and preserves the other lanes.
    pub fn fatal(&self) -> bool {
        match self {
            Self::ActiveIdentityExpired | Self::ObservationEnded => false,
            Self::Client(error) => error.fatal(),
            Self::Identity(_) => false,
            Self::Issued(_) => true,
        }
    }

    pub fn decision(&self) -> &'static str {
        if self.retryable() {
            vonk_agent_protocol::generated::AgentClientDecision::Retry.as_str()
        } else if self.fatal() {
            vonk_agent_protocol::generated::AgentClientDecision::Exit.as_str()
        } else {
            vonk_agent_protocol::generated::AgentClientDecision::Defer.as_str()
        }
    }
}

pub fn active_identity_is_valid(config: &AgentConfig) -> Result<bool, RotationError> {
    let root = config.data_dir.join("credentials");
    let paths = active_identity_paths(&root)?;
    Ok(!identity_expired(&paths, Utc::now())?)
}

pub async fn rotate_if_due(
    config: &AgentConfig,
    client: &AgentHttpClient,
) -> Result<bool, RotationError> {
    rotate_if_due_at(config, client, Utc::now()).await
}

/// Rotate through the same production protocol at a supplied scheduling clock.
/// TLS, staged-identity expiry, Controller and CA clocks remain real wall time.
pub async fn rotate_if_due_at(
    config: &AgentConfig,
    client: &AgentHttpClient,
    scheduling_now: DateTime<Utc>,
) -> Result<bool, RotationError> {
    let root = config.data_dir.join("credentials");
    let now = Utc::now();
    let staged = match staged_identity_paths(&root) {
        Ok(staged) => staged,
        Err(error) => {
            eprintln!("vonk-agent: staged credential projection unavailable: {error}");
            retire_unavailable_staged(&root)?;
            None
        }
    };
    if let Some((generation, paths)) = staged {
        let expired = match identity_expired(&paths, now) {
            Ok(expired) => expired,
            Err(error) => {
                retire_unavailable_staged(&root)?;
                return Err(error.into());
            }
        };
        if expired {
            retire_expired_staged(&root, generation)?;
        } else {
            let replacement = AgentHttpClient::from_identity_paths(config, &paths)?;
            client
                .activate_replacement(&replacement, generation)
                .await?;
            publish_staged(&root, generation)?;
            return Ok(true);
        }
    }
    if !renewal_due(&root, scheduling_now)? {
        client.observe_active_identity(config).await?;
        return Ok(false);
    }
    let expired = !active_identity_is_valid(config)?;
    let pending = match load_pending(&root)? {
        Some(value) => value,
        None => {
            let value = generate_pending(&config.node_id)?;
            persist_pending(&root, &value)?;
            value
        }
    };
    let active_client = AgentHttpClient::from_config(config)?;
    let renewal = if expired {
        active_client.renew_expired(config, &pending.csr_pem).await
    } else {
        active_client.renew(&pending.csr_pem).await
    };
    let issued = match renewal {
        Ok(issued) => issued,
        // A controller-side staged CSR conflict is a typed denial.  The
        // recovery endpoint is authenticated with the same still-active
        // source identity and is bounded to this one durable CSR.  Actual
        // Only the exact Controller context emitted for a staged CSR
        // conflict can enter recovery. Other authentication or rejection
        // responses retain their original status, code, and decision.
        Err(error)
            if error.status() == Some(403)
                && error.code().is_some_and(|code| {
                    vocabulary::is(
                        code,
                        SecurityRefusalReason::AgentCertificateRotationConflict,
                    )
                }) =>
        {
            active_client.recover_renewal(&pending.csr_pem).await?
        }
        Err(error) => return Err(error.into()),
    };
    validate_issued(&issued, &pending, &config.node_id)?;
    let generation = issued.generation;
    stage_identity(
        &root,
        &IdentityMaterial {
            node_id: issued.node_id,
            private_key_pem: pending.private_key_pem,
            certificate_pem: issued.certificate_pem.into_bytes(),
            chain_pem: issued.chain_pem.into_bytes(),
            serial: issued.serial,
            fingerprint: issued.fingerprint,
            generation,
        },
    )?;
    let (_, paths) = staged_identity_paths(&root)?.ok_or(IdentityError::Node)?;
    let replacement = AgentHttpClient::from_identity_paths(config, &paths)?;
    client
        .activate_replacement(&replacement, generation)
        .await?;
    publish_staged(&root, generation)?;
    clear_pending(&root)?;
    let active = active_identity_paths(&root)?;
    if active != paths {
        return Err(IdentityError::Node.into());
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::{ClientError, RotationError};

    #[test]
    fn expired_renewal_refused_for_revocation_never_retries_or_activates() {
        let error = RotationError::Client(ClientError::Controller(Box::new(
            crate::client::ControllerError::from_status(403),
        )));
        assert!(error.fatal());
        assert!(!error.retryable());
        assert_eq!(error.decision(), "exit");
    }

    #[test]
    fn rotation_preserves_controller_status_code_and_decision() {
        let error = RotationError::Client(ClientError::Controller(Box::new(
            crate::client::ControllerError::from_status(403),
        )));
        assert_eq!(error.code(), "controller.request_rejected");
        assert_eq!(error.decision(), "exit");
        assert!(!error.retryable());
        assert!(error.fatal());
        assert!(error.to_string().contains("HTTP 403"));
    }
}
