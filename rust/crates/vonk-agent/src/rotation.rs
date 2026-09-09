use chrono::Utc;
use thiserror::Error;

use crate::{
    client::{AgentHttpClient, ClientError},
    config::AgentConfig,
    identity::{
        IdentityError, IdentityMaterial, active_identity_paths, clear_pending, generate_pending,
        identity_expired, load_pending, persist_pending, publish_staged, renewal_due,
        retire_expired_staged, stage_identity, staged_identity_paths,
    },
    pair::{PairingError, validate_issued},
};

#[derive(Debug, Error)]
pub enum RotationError {
    #[error("credential operation failed: {0}")]
    Client(#[from] ClientError),
    #[error("credential storage failed: {0}")]
    Identity(#[from] IdentityError),
    #[error("issued credential is invalid: {0}")]
    Issued(#[from] PairingError),
}

impl RotationError {
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Client(error) if error.retryable())
    }

    pub fn code(&self) -> String {
        match self {
            Self::Client(error) => error
                .code()
                .map(str::to_owned)
                .unwrap_or_else(|| "controller.request_failed".to_owned()),
            Self::Identity(_) => "local.identity_failed".to_owned(),
            Self::Issued(_) => "local.issued_identity_invalid".to_owned(),
        }
    }

    pub fn decision(&self) -> &'static str {
        if self.retryable() { "retry" } else { "exit" }
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
    let root = config.data_dir.join("credentials");
    let now = Utc::now();
    if let Some((generation, paths)) = staged_identity_paths(&root)? {
        if identity_expired(&paths, now)? {
            retire_expired_staged(&root, generation)?;
        } else {
            AgentHttpClient::from_identity_paths(config, &paths)?
                .activate(generation)
                .await?;
            publish_staged(&root, generation)?;
            client.replace_identity(config, &paths)?;
            return Ok(true);
        }
    }
    if !renewal_due(&root, now)? {
        return Ok(false);
    }
    let pending = match load_pending(&root)? {
        Some(value) => value,
        None => {
            let value = generate_pending(&config.node_id)?;
            persist_pending(&root, &value)?;
            value
        }
    };
    let issued = AgentHttpClient::from_config(config)?
        .renew(&pending.csr_pem)
        .await?;
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
    AgentHttpClient::from_identity_paths(config, &paths)?
        .activate(generation)
        .await?;
    publish_staged(&root, generation)?;
    clear_pending(&root)?;
    let active = active_identity_paths(&root)?;
    if active != paths {
        return Err(IdentityError::Node.into());
    }
    client.replace_identity(config, &paths)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::{ClientError, RotationError};

    #[test]
    fn rotation_preserves_controller_status_code_and_decision() {
        let error = RotationError::Client(ClientError::Controller(
            crate::client::ControllerError::from_status(403),
        ));
        assert_eq!(error.code(), "controller.request_rejected");
        assert_eq!(error.decision(), "exit");
        assert!(!error.retryable());
        assert!(error.to_string().contains("HTTP 403"));
    }
}
