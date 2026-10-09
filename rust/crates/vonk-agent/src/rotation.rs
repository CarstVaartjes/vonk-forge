use chrono::{DateTime, Utc};
use thiserror::Error;
use vonk_agent_protocol::generated::SecurityRefusalReason;

use crate::{
    client::{AgentHttpClient, ClientError},
    config::AgentConfig,
    identity::{
        IdentityError, IdentityMaterial, active_identity_paths, clear_pending, identity_expired,
        observe_staged_identity, prepare_pending, publish_staged, renewal_due, retire_expired_staged,
        stage_identity, staged_identity_paths,
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
            Self::Client(error) => error
                .code()
                .map(str::to_owned)
                .unwrap_or_else(|| "controller.request_failed".to_owned()),
            Self::Identity(_) => {
                vonk_agent_protocol::generated::WaitReason::ObservationUnavailable.to_string()
            }
            Self::Issued(_) => "local.issued_identity_invalid".to_owned(),
        }
    }

    /// Errors that end the agent: the Controller refused this identity
    /// (401/403, including revocation), or the Controller issued a credential that
    /// does not belong to this node.  An expired active certificate is not
    /// fatal: it is never used, and renewal is retried idle.
    pub fn fatal(&self) -> bool {
        match self {
            Self::ActiveIdentityExpired | Self::ObservationEnded | Self::Identity(_) => false,
            Self::Client(ClientError::Identity | ClientError::CredentialRead(_)) => false,
            Self::Client(error) => error.fatal(),
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
    if let Some((generation, paths)) = observe_staged_identity(&root)? {
        if identity_expired(&paths, now)? {
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
        return Ok(false);
    }
    let expired = !active_identity_is_valid(config)?;
    let pending = prepare_pending(&root, &config.node_id)?;
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

    #[test]
    fn expired_active_identity_defers_renewal_instead_of_exiting() {
        let error = RotationError::ActiveIdentityExpired;
        assert_eq!(error.code(), "local.identity_expired");
        assert_eq!(error.decision(), "defer");
        assert!(!error.retryable());
        assert!(!error.fatal());
        assert!(
            error
                .to_string()
                .contains("active agent certificate has expired")
        );
    }
}
