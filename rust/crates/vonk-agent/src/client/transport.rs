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

    /// A storage miss must not prevent local observation lanes from starting.
    /// The placeholder trusts no server and has no client authority. Rotation
    /// installs a verified transport after the credential owner recovers.
    pub fn for_observation(config: &AgentConfig) -> Result<Self, ClientError> {
        match Self::from_config(config) {
            Ok(client) => Ok(client),
            Err(error) if error.fatal() => Err(error),
            Err(error) => {
                eprintln!("vonk-agent: credential observation deferred: {error}");
                Ok(Self {
                    client: Arc::new(RwLock::new(
                        Client::builder()
                            .https_only(true)
                            .tls_certs_only(std::iter::empty::<Certificate>())
                            .timeout(CONTROLLER_REQUEST_TIMEOUT)
                            .build()?,
                    )),
                    controller: config.controller_url.clone(),
                    node_id: config.node_id.clone(),
                    progress_phase: Arc::new(Mutex::new(None)),
                })
            }
        }
    }

    pub async fn observe_active_identity(&self, config: &AgentConfig) -> Result<(), ClientError> {
        let paths = active_identity_paths(&config.data_dir.join("credentials"))
            .map_err(|_| ClientError::Identity)?;
        let replacement = Self::build_client(config, &paths)?;
        let mut transport = tokio::time::timeout(ROTATION_REQUEST_TIMEOUT, self.client.write())
            .await
            .map_err(|_| {
                ClientError::Unknown(
                    vonk_agent_protocol::generated::TransientReason::LocalStateUnavailable,
                )
            })?;
        *transport = replacement;
        Ok(())
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
        verify_ca_pin(&ca_pem, &config.ca_sha256).map_err(|_| {
            ClientError::Unknown(
                vonk_agent_protocol::generated::TransientReason::LocalStateUnavailable,
            )
        })?;
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
                return Err(ClientError::Unknown(
                    vonk_agent_protocol::generated::TransientReason::LocalStateUnavailable,
                ));
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
            .map_err(|_| {
                ClientError::Unknown(
                    vonk_agent_protocol::generated::TransientReason::LocalStateUnavailable,
                )
            })
    }

    pub async fn activate(&self, generation: u64) -> Result<(), ClientError> {
        if generation == 0 {
            return Err(ClientError::Protocol);
        }
        let request = ActivateRequest {
            generation,
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
