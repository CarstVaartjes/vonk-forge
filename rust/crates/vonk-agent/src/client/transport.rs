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
            identity_content: Default::default(),
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
                    identity_content: Default::default(),
                    progress_phase: Arc::new(Mutex::new(None)),
                })
            }
        }
    }

    // This is a disposable content cache, never an admission gate. A busy
    // cache is a miss; it cannot hold the transport or delay another request.
    fn observed_transport_content(&self) -> Option<String> {
        match self.identity_content.try_lock() {
            Ok(content) => content.clone(),
            Err(std::sync::TryLockError::Poisoned(error)) => error.into_inner().clone(),
            Err(std::sync::TryLockError::WouldBlock) => None,
        }
    }

    fn record_transport_content(&self, content: Option<String>) {
        match self.identity_content.try_lock() {
            Ok(mut observed) => *observed = content,
            Err(std::sync::TryLockError::Poisoned(error)) => *error.into_inner() = content,
            Err(std::sync::TryLockError::WouldBlock) => {}
        }
    }

    pub async fn observe_active_identity(&self, config: &AgentConfig) -> Result<(), ClientError> {
        let paths = active_identity_paths(&config.data_dir.join("credentials"))
            .map_err(|_| ClientError::Identity)?;
        let (replacement, content) = Self::build_client(config, &paths)?;
        if self.observed_transport_content().as_ref() == Some(&content) {
            return Ok(());
        }
        // Credential observation never joins the writer queue behind an upload.
        // A changed identity is retried by the bounded rotation observation;
        // activation has its separate drain/fence protocol below.
        let mut transport = self
            .client
            .try_write()
            .map_err(|_| ClientError::Retryable)?;
        *transport = replacement;
        self.record_transport_content(Some(content));
        Ok(())
    }

    pub fn from_identity_paths(
        config: &AgentConfig,
        paths: &IdentityPaths,
    ) -> Result<Self, ClientError> {
        let (client, content) = Self::build_client(config, paths)?;
        Ok(Self {
            client: Arc::new(RwLock::new(client)),
            controller: config.controller_url.clone(),
            node_id: config.node_id.clone(),
            identity_content: Arc::new(Mutex::new(Some(content))),
            progress_phase: Arc::new(Mutex::new(None)),
        })
    }

    pub(super) fn build_client(
        config: &AgentConfig,
        paths: &IdentityPaths,
    ) -> Result<(Client, String), ClientError> {
        let ca_pem = fs::read(&config.ca_path)?;
        verify_ca_pin(&ca_pem, &config.ca_sha256).map_err(|_| ClientError::Pin)?;
        let mut identity_pem = fs::read(&paths.certificate)?;
        identity_pem.extend_from_slice(&fs::read(&paths.chain)?);
        identity_pem.extend_from_slice(&fs::read(&paths.private_key)?);
        // The exact credential bytes identify this transport, independent of
        // paths or generation bookkeeping. Unchanged credentials never queue
        // a writer behind a bulk upload just to rebuild the same TLS client.
        let content = hex_sha256(&[identity_pem.as_slice(), ca_pem.as_slice()].concat());
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
        Ok((client, content))
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
        self.record_transport_content(replacement.observed_transport_content());
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
