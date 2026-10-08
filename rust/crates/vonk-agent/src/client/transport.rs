//! Transport for the client boundary.

use super::*;

impl AgentHttpClient {
    #[cfg(test)]
    pub(crate) fn for_http_test(controller: &str, node_id: &str) -> Self {
        Self {
            client: Arc::new(RwLock::new(
                reqwest::Client::builder()
                    .timeout(CONTROLLER_REQUEST_TIMEOUT)
                    .build()
                    .expect("test transport"),
            )),
            controller: Url::parse(controller).expect("test controller URL must be valid"),
            node_id: node_id.to_owned(),
            progress_phase: Arc::new(Mutex::new(None)),
        }
    }

    #[cfg(test)]
    pub(crate) fn node_id(&self) -> &str {
        &self.node_id
    }

    pub fn from_config(config: &AgentConfig) -> Result<Self, ClientError> {
        let paths = active_identity_paths(&config.data_dir.join("credentials"))
            .map_err(|_| ClientError::Identity)?;
        Self::from_identity_paths(config, &paths)
    }

    pub fn from_identity_paths(
        config: &AgentConfig,
        paths: &IdentityPaths,
    ) -> Result<Self, ClientError> {
        let client = Self::build_client(config, paths)?;
        Ok(Self {
            client: Arc::new(RwLock::new(client)),
            controller: config.controller_url.clone(),
            node_id: config.node_id.clone(),
            progress_phase: Arc::new(Mutex::new(None)),
        })
    }

    pub(super) fn build_client(
        config: &AgentConfig,
        paths: &IdentityPaths,
    ) -> Result<Client, ClientError> {
        let ca_pem = fs::read(&config.ca_path)?;
        verify_ca_pin(&ca_pem, &config.ca_sha256).map_err(|_| ClientError::Pin)?;
        let mut identity_pem = fs::read(&paths.certificate)?;
        identity_pem.extend_from_slice(&fs::read(&paths.chain)?);
        identity_pem.extend_from_slice(&fs::read(&paths.private_key)?);
        let identity = Identity::from_pem(&identity_pem).map_err(|_| ClientError::Identity)?;
        let ca = Certificate::from_pem(&ca_pem).map_err(|_| ClientError::Identity)?;
        let client = Client::builder()
            .https_only(true)
            .tls_certs_only([ca])
            .identity(identity)
            // Bulk transfers are a few long streams. Separate HTTP/1.1
            // connections each get their own TCP window and TLS worker, where
            // HTTP/2 would multiplex them behind one flow-control window.
            .http1_only()
            .connect_timeout(Duration::from_secs(10))
            .timeout(CONTROLLER_REQUEST_TIMEOUT)
            .build()?;
        Ok(client)
    }

    pub(crate) async fn activate_replacement(
        &self,
        replacement: &Self,
        generation: u64,
    ) -> Result<(), ClientError> {
        self.activate_replacement_until(
            replacement,
            generation,
            tokio::time::Instant::now() + RECIPE_IMAGE_UPLOAD_TIMEOUT + ROTATION_REQUEST_TIMEOUT,
        )
        .await
    }

    pub(super) async fn activate_replacement_until(
        &self,
        replacement: &Self,
        generation: u64,
        deadline: tokio::time::Instant,
    ) -> Result<(), ClientError> {
        if self.controller != replacement.controller
            || self.node_id != replacement.node_id
            || Arc::ptr_eq(&self.client, &replacement.client)
        {
            return Err(ClientError::Protocol);
        }
        // Drain requests using the old identity before the Controller revokes
        // it, then replace the shared transport before admitting another one.
        // A PUT can take an hour. Periodically release our place in the writer
        // queue so heartbeats can renew that operation's lease while it drains.
        let mut transport = loop {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            if remaining.is_zero() {
                return Err(ClientError::Retryable);
            }
            if let Ok(guard) = tokio::time::timeout(
                remaining.min(Duration::from_millis(100)),
                self.client.write(),
            )
            .await
            {
                break guard;
            }
            tokio::task::yield_now().await;
        };
        replacement.activate(generation).await?;
        *transport = replacement.current_client().await?.clone();
        Ok(())
    }

    pub(super) async fn current_client(&self) -> Result<RwLockReadGuard<'_, Client>, ClientError> {
        // Each request expression retains this guard through send().await.
        // Streaming response bodies may continue after their authenticated
        // headers arrive; uploads retain it until their response arrives.
        tokio::time::timeout(CONTROLLER_REQUEST_TIMEOUT, self.client.read())
            .await
            .map_err(|_| ClientError::Retryable)
    }

    /// Request recovery of an unactivated staged certificate whose CSR does
    /// not match the durable pending CSR.  The endpoint is authenticated with
    /// this client's active identity and is intentionally separate from the
    /// normal renewal operation so a 403 cannot silently become a replacement
    /// request.
    pub async fn recover_renewal(
        &self,
        csr: &[u8],
    ) -> Result<IssuedCertificateResponse, ClientError> {
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
            .await?
            .post(self.endpoint("/agent/renew/recover")?)
            .timeout(ROTATION_REQUEST_TIMEOUT)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        classify_response(&response)?;
        let body = bounded_body(response).await?;
        let issued: IssuedCertificateResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        if issued.node_id != self.node_id || issued.generation == 0 {
            return Err(ClientError::Protocol);
        }
        Ok(issued)
    }

    pub async fn activate(&self, generation: u64) -> Result<(), ClientError> {
        if generation == 0 {
            return Err(ClientError::Protocol);
        }
        let request = ActivateRequest {
            generation: generation.try_into().map_err(|_| ClientError::Protocol)?,
            node_id: self.node_id.clone(),
        };
        let body = canonical_generated_json(&request).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/renew/activate")?)
            .timeout(ROTATION_REQUEST_TIMEOUT)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if response.status() != StatusCode::NO_CONTENT {
            classify_response(&response)?;
            return Err(ClientError::Protocol);
        }
        Ok(())
    }

    pub(super) fn endpoint(&self, path: &str) -> Result<Url, ClientError> {
        self.controller
            .join(path)
            .map_err(|_| ClientError::Protocol)
    }
}

#[cfg(test)]
mod tests;
