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
    #[error("credential transport failed")]
    Client(#[from] ClientError),
    #[error("credential storage failed")]
    Identity(#[from] IdentityError),
    #[error("issued credential is invalid")]
    Issued(#[from] PairingError),
}

pub async fn rotate_if_due(config: &AgentConfig) -> Result<bool, RotationError> {
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
    Ok(true)
}
