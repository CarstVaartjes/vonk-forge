//! Certificate renewal transports share the normal identity and error fences.
use std::fs;

use reqwest::{Certificate, Client, StatusCode};
use vonk_agent_protocol::generated::{
    ExpiredRenewRequest, IssuedCertificateResponse, RenewRequest, SecurityRefusalReason,
};
use vonk_agent_protocol::{canonical_generated_json, hex_sha256, parse_strict};

use super::{
    AgentHttpClient, ClientError, ROTATION_REQUEST_TIMEOUT, bounded_body, classify_response,
    controller_error, is_rotation_conflict, valid_error_code, valid_error_token,
};
use crate::{config::AgentConfig, identity::active_identity_paths, pair::verify_ca_pin};

impl AgentHttpClient {
    pub async fn renew_expired(
        &self,
        config: &AgentConfig,
        csr: &[u8],
    ) -> Result<IssuedCertificateResponse, ClientError> {
        let root = config.data_dir.join("credentials");
        let paths = active_identity_paths(&root).map_err(|_| ClientError::Identity)?;
        let key_pem = fs::read(&paths.private_key)?;
        let key = rustls_pemfile::private_key(&mut key_pem.as_slice())
            .map_err(|_| ClientError::Identity)?
            .ok_or(ClientError::Identity)?;
        let signer = ring::signature::Ed25519KeyPair::from_pkcs8(key.secret_der())
            .map_err(|_| ClientError::Identity)?;
        let signed_at = chrono::Utc::now().timestamp();
        let serial =
            crate::identity::active_certificate_serial(&root).map_err(|_| ClientError::Identity)?;
        let request = expired_renewal_request(&self.node_id, serial, csr, signed_at, &signer)?;
        let ca_pem = fs::read(&config.ca_path)?;
        verify_ca_pin(&ca_pem, &config.ca_sha256).map_err(|_| ClientError::Pin)?;
        let ca = Certificate::from_pem(&ca_pem).map_err(|_| ClientError::Identity)?;
        // Server authentication is identical; no expired client identity is
        // sent to the public, proof-authenticated enrollment ingress.
        let transport = Client::builder()
            .https_only(true)
            .tls_certs_only([ca])
            .connect_timeout(ROTATION_REQUEST_TIMEOUT)
            .timeout(ROTATION_REQUEST_TIMEOUT)
            .build()?;
        let response = transport
            .post(
                config
                    .enrollment_url
                    .join("/agent/renew/expired")
                    .map_err(|_| ClientError::Protocol)?,
            )
            .header("content-type", "application/json")
            .body(canonical_generated_json(&request).map_err(|_| ClientError::Protocol)?)
            .send()
            .await?;
        classify_response(&response)?;
        let issued: IssuedCertificateResponse =
            parse_strict(&bounded_body(response).await?).map_err(|_| ClientError::Protocol)?;
        if issued.node_id != self.node_id || issued.generation == 0 {
            return Err(ClientError::Protocol);
        }
        Ok(issued)
    }

    pub async fn renew(&self, csr: &[u8]) -> Result<IssuedCertificateResponse, ClientError> {
        let csr = std::str::from_utf8(csr).map_err(|_| ClientError::Protocol)?;
        if csr.is_empty() || csr.len() > 16 * 1024 {
            return Err(ClientError::Protocol);
        }
        let request = RenewRequest {
            csr: csr.to_owned(),
            node_id: self.node_id.clone(),
        };
        let body = canonical_generated_json(&request).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/renew")?)
            .timeout(ROTATION_REQUEST_TIMEOUT)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if !response.status().is_success() {
            // Renewal can identify one narrowly defined recovery case from
            // its bounded, safe error body. Keep the status, path, request ID,
            // and canonical code in the contextual Controller error for all
            // outcomes; generic 401/403 responses remain rejections.
            let status = response.status();
            let endpoint = response.url().path().to_owned();
            let operation = format!("controller.request {endpoint}");
            let request_id = response
                .headers()
                .get("x-request-id")
                .and_then(|value| value.to_str().ok())
                .filter(|value| valid_error_token(value))
                .map(str::to_owned);
            let header_code = response
                .headers()
                .get("x-vonk-error-code")
                .and_then(|value| value.to_str().ok())
                .filter(|value| valid_error_code(value))
                .map(str::to_owned);
            let body = bounded_body(response).await?;
            let code = if status == StatusCode::FORBIDDEN && is_rotation_conflict(&body) {
                Some(SecurityRefusalReason::AgentCertificateRotationConflict.to_string())
            } else {
                header_code
            };
            return Err(ClientError::Controller(Box::new(controller_error(
                status, &endpoint, &operation, request_id, code,
            ))));
        }
        let body = bounded_body(response).await?;
        let issued: IssuedCertificateResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        if issued.node_id != self.node_id || issued.generation == 0 {
            return Err(ClientError::Protocol);
        }
        Ok(issued)
    }
}

pub(super) fn expired_renewal_request(
    node_id: &str,
    serial: String,
    csr: &[u8],
    signed_at: i64,
    signer: &ring::signature::Ed25519KeyPair,
) -> Result<ExpiredRenewRequest, ClientError> {
    let proof = format!(
        "vonk-expired-renew-v1\n{node_id}\n{serial}\n{signed_at}\n{}",
        hex_sha256(csr)
    );
    Ok(ExpiredRenewRequest {
        node_id: node_id.to_owned(),
        serial,
        signed_at: signed_at.into(),
        csr: std::str::from_utf8(csr)
            .map_err(|_| ClientError::Protocol)?
            .to_owned(),
        signature: hex::encode(signer.sign(proof.as_bytes()).as_ref()),
    })
}
