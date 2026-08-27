use std::{fs, path::Path, time::Duration};

use reqwest::{Certificate, Client, Identity, StatusCode};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::io::AsyncWriteExt;
use tokio_util::io::ReaderStream;
use url::Url;
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, HostRuntimeAction, canonical_json,
    parse_strict,
};

use crate::{
    config::AgentConfig,
    identity::{IdentityPaths, active_identity_paths},
    inventory::Inventory,
    oci::{MAX_MANAGED_RECIPE_RUNS, RecipeRunObservation},
    pair::{IssuedResponse, verify_ca_pin},
    runtime_identity::AgentRuntimeIdentity,
    telemetry::{TelemetrySample, valid_report_batch},
    workloads::WorkloadSpec,
};

const MAX_BODY_BYTES: usize = 64 * 1024;
const RECIPE_IMAGE_UPLOAD_TIMEOUT: Duration = Duration::from_secs(60 * 60);
const HOST_RUNTIME_GRANT_TTL_SECONDS: u16 = 10;

#[derive(Debug, Error)]
pub enum ClientError {
    #[error("agent credential could not be read")]
    CredentialRead(#[from] std::io::Error),
    #[error("agent TLS identity is invalid")]
    Identity,
    #[error("controller transport failed")]
    Transport(#[from] reqwest::Error),
    #[error("controller temporarily rejected the request")]
    Retryable,
    #[error("agent identity is not authorized")]
    Authentication,
    #[error("controller protocol response is invalid")]
    Protocol,
    #[error("controller CA pin is invalid")]
    Pin,
}

impl ClientError {
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Transport(_) | Self::Retryable)
    }
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct ClaimRequest<'a> {
    capabilities: &'a [&'a str],
    #[serde(skip_serializing_if = "Option::is_none")]
    hostname: Option<&'a str>,
    lease_seconds: u64,
    node_id: &'a str,
    protocol_version: u32,
    runtime_identity: Option<&'a AgentRuntimeIdentity>,
    wait_seconds: u64,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct InventoryRequest<'a> {
    schema_version: u8,
    observed_at: chrono::DateTime<chrono::Utc>,
    #[serde(flatten)]
    inventory: &'a Inventory,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct RecipeRunObservationsRequest<'a> {
    schema_version: u8,
    observed_at: chrono::DateTime<chrono::Utc>,
    runs: &'a [RecipeRunObservation],
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct TelemetryRequest<'a> {
    schema_version: u8,
    samples: &'a [TelemetrySample],
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct RenewRequest<'a> {
    csr: &'a str,
    node_id: &'a str,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct ActivateRequest<'a> {
    generation: u64,
    node_id: &'a str,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct HostRuntimeGrantRequest<'a> {
    node_id: &'a str,
    job_id: uuid::Uuid,
    operation_id: uuid::Uuid,
    attempt: u32,
    fence: uuid::Uuid,
    action: HostRuntimeAction,
    request_sha256: &'a str,
    expires_in_seconds: u16,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct AgentUpgradeGrantRequest<'a> {
    node_id: &'a str,
    job_id: uuid::Uuid,
    operation_id: uuid::Uuid,
    attempt: u32,
    fence: uuid::Uuid,
    package_sha256: &'a str,
    package_signature: &'a str,
    expires_in_seconds: u16,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct HostRuntimeGrantResponse {
    grant: serde_json::Value,
}

#[derive(Clone)]
pub struct AgentHttpClient {
    client: Client,
    controller: Url,
    node_id: String,
}

impl AgentHttpClient {
    pub fn from_config(config: &AgentConfig) -> Result<Self, ClientError> {
        let paths = active_identity_paths(&config.data_dir.join("credentials"))
            .map_err(|_| ClientError::Identity)?;
        Self::from_identity_paths(config, &paths)
    }

    pub fn from_identity_paths(
        config: &AgentConfig,
        paths: &IdentityPaths,
    ) -> Result<Self, ClientError> {
        let ca_pem = fs::read(&config.ca_path)?;
        verify_ca_pin(&ca_pem, &config.ca_sha256).map_err(|_| ClientError::Pin)?;
        let mut identity_pem = fs::read(&paths.certificate)?;
        identity_pem.extend_from_slice(&fs::read(&paths.chain)?);
        identity_pem.extend_from_slice(&fs::read(&paths.private_key)?);
        let identity = Identity::from_pem(&identity_pem).map_err(|_| ClientError::Identity)?;
        let ca = Certificate::from_pem(&ca_pem).map_err(|_| ClientError::Identity)?;
        let client = Client::builder()
            .https_only(true)
            .tls_built_in_root_certs(false)
            .add_root_certificate(ca)
            .identity(identity)
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(75))
            .build()?;
        Ok(Self {
            client,
            controller: config.controller_url.clone(),
            node_id: config.node_id.clone(),
        })
    }

    pub async fn claim(
        &self,
        capabilities: &[&str],
        wait_seconds: u64,
        runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        let hostname = local_hostname();
        let response = self
            .client
            .post(self.endpoint("/agent/v1/claim")?)
            .json(&ClaimRequest {
                capabilities,
                hostname: hostname.as_deref(),
                lease_seconds: 60,
                node_id: &self.node_id,
                protocol_version: 3,
                runtime_identity,
                wait_seconds: wait_seconds.min(60),
            })
            .send()
            .await?;
        let status = response.status();
        if status == StatusCode::NO_CONTENT {
            return Ok(None);
        }
        classify_status(status)?;
        let body = bounded_body(response).await?;
        parse_claim_response(status.as_u16(), &body)
    }

    pub async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        result.validate().map_err(|_| ClientError::Protocol)?;
        let body = canonical_json(result).map_err(|_| ClientError::Protocol)?;
        let response = self
            .client
            .post(self.endpoint("/agent/v1/result")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if matches!(
            response.status(),
            StatusCode::NO_CONTENT | StatusCode::CONFLICT
        ) {
            Ok(())
        } else {
            classify_status(response.status())?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        progress.validate().map_err(|_| ClientError::Protocol)?;
        if progress.node_id != self.node_id {
            return Err(ClientError::Protocol);
        }
        let body = canonical_json(progress).map_err(|_| ClientError::Protocol)?;
        let response = self
            .client
            .post(self.endpoint("/agent/v1/heartbeat")?)
            .header("content-type", "application/json")
            .timeout(Duration::from_secs(15))
            .body(body)
            .send()
            .await?;
        classify_status(response.status())?;
        let body = bounded_body(response).await?;
        let directive = parse_strict::<AgentDirective>(&body)
            .or_else(|_| parse_strict::<AgentProgress>(&body).map(AgentDirective::from_progress))
            .map_err(|_| ClientError::Protocol)?;
        directive.validate().map_err(|_| ClientError::Protocol)?;
        if directive.schema_version != progress.schema_version
            || directive.job_id != progress.job_id
            || directive.operation_id != progress.operation_id
            || directive.attempt != progress.attempt
            || directive.fence != progress.fence
            || directive.node_id != progress.node_id
            || directive.deadline < progress.deadline
        {
            return Err(ClientError::Protocol);
        }
        Ok(directive)
    }

    pub async fn host_runtime_grant(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        request_sha256: &str,
    ) -> Result<serde_json::Value, ClientError> {
        if claim.node_id != self.node_id || !valid_sha256(request_sha256) || claim.attempt == 0 {
            return Err(ClientError::Protocol);
        }
        let body = canonical_json(&HostRuntimeGrantRequest {
            node_id: &self.node_id,
            job_id: claim.job_id,
            operation_id: claim.operation_id,
            attempt: claim.attempt,
            fence: claim.fence,
            action,
            request_sha256,
            expires_in_seconds: HOST_RUNTIME_GRANT_TTL_SECONDS,
        })
        .map_err(|_| ClientError::Protocol)?;
        let response = self
            .client
            .post(self.endpoint("/agent/v1/host-runtime/grant")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        classify_status(response.status())?;
        let body = bounded_body(response).await?;
        let response: HostRuntimeGrantResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        if !response.grant.is_object() {
            return Err(ClientError::Protocol);
        }
        Ok(response.grant)
    }

    pub async fn agent_upgrade_grant(
        &self,
        claim: &AgentClaim,
        package_sha256: &str,
        package_signature: &str,
    ) -> Result<serde_json::Value, ClientError> {
        if claim.node_id != self.node_id
            || !valid_sha256(package_sha256)
            || package_signature.len() != 128
            || !package_signature
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            || claim.attempt == 0
        {
            return Err(ClientError::Protocol);
        }
        let body = canonical_json(&AgentUpgradeGrantRequest {
            node_id: &self.node_id,
            job_id: claim.job_id,
            operation_id: claim.operation_id,
            attempt: claim.attempt,
            fence: claim.fence,
            package_sha256,
            package_signature,
            expires_in_seconds: HOST_RUNTIME_GRANT_TTL_SECONDS,
        })
        .map_err(|_| ClientError::Protocol)?;
        let response = self
            .client
            .post(self.endpoint("/agent/v1/agent-upgrade/grant")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        classify_status(response.status())?;
        let body = bounded_body(response).await?;
        let response: HostRuntimeGrantResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        if !response.grant.is_object() {
            return Err(ClientError::Protocol);
        }
        Ok(response.grant)
    }

    pub async fn recipe_spec(&self, installation_id: &str) -> Result<WorkloadSpec, ClientError> {
        if uuid::Uuid::parse_str(installation_id).is_err() {
            return Err(ClientError::Protocol);
        }
        let response = self
            .client
            .get(self.endpoint(&format!(
                "/agent/v1/recipe-installations/{installation_id}/spec"
            ))?)
            .send()
            .await?;
        classify_status(response.status())?;
        let body = bounded_body(response).await?;
        let spec: WorkloadSpec =
            serde_json::from_slice(&body).map_err(|_| ClientError::Protocol)?;
        spec.validate().map_err(|_| ClientError::Protocol)?;
        Ok(spec)
    }

    pub async fn source_bundle(
        &self,
        source_sha256: &str,
        expected_bytes: u64,
    ) -> Result<Vec<u8>, ClientError> {
        if source_sha256.len() != 64
            || !source_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            || !(1..=64 * 1024 * 1024).contains(&expected_bytes)
        {
            return Err(ClientError::Protocol);
        }
        let response = self
            .client
            .get(self.endpoint(&format!("/agent/v1/source-bundles/{source_sha256}"))?)
            .send()
            .await?;
        classify_status(response.status())?;
        if response.content_length() != Some(expected_bytes) {
            return Err(ClientError::Protocol);
        }
        bounded_body_limit(response, expected_bytes as usize).await
    }

    pub async fn upload_recipe_image(
        &self,
        build_id: uuid::Uuid,
        image_digest: &str,
        oci_layout_sha256: &str,
        image_bytes: u64,
        path: &Path,
    ) -> Result<(), ClientError> {
        if !valid_oci_digest(image_digest)
            || !valid_sha256(oci_layout_sha256)
            || !(1..=16 * 1024_u64.pow(4)).contains(&image_bytes)
            || tokio::fs::metadata(path).await?.len() != image_bytes
        {
            return Err(ClientError::Protocol);
        }
        let file = tokio::fs::File::open(path).await?;
        let response = self
            .client
            .put(self.endpoint(&format!("/agent/v1/recipe-builds/{build_id}/image"))?)
            // The historical evidence field names the immutable layout digest,
            // while Spark's native Docker runtime consumes a docker-save tar.
            .header("content-type", "application/x-tar")
            .header("content-length", image_bytes)
            .header("x-vonk-image-digest", image_digest)
            .header("x-vonk-oci-layout-sha256", oci_layout_sha256)
            .timeout(RECIPE_IMAGE_UPLOAD_TIMEOUT)
            .body(reqwest::Body::wrap_stream(ReaderStream::new(file)))
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_status(response.status())?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn download_artifact(
        &self,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        if !valid_sha256(sha256) || !(1..=16 * 1024_u64.pow(4)).contains(&expected_bytes) {
            return Err(ClientError::Protocol);
        }
        let mut output = tokio::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(destination)
            .await?;
        let mut offset = 0_u64;
        while offset < expected_bytes {
            let end = expected_bytes
                .saturating_sub(1)
                .min(offset.saturating_add(8 * 1024 * 1024 - 1));
            let response = self
                .client
                .get(self.endpoint(&format!("/agent/v1/artifacts/{sha256}"))?)
                .header("range", format!("bytes={offset}-{end}"))
                .send()
                .await?;
            if response.status() != StatusCode::PARTIAL_CONTENT
                || response.content_length() != Some(end - offset + 1)
            {
                classify_status(response.status())?;
                return Err(ClientError::Protocol);
            }
            let chunk = bounded_body_limit(response, (end - offset + 1) as usize).await?;
            if chunk.len() as u64 != end - offset + 1 {
                return Err(ClientError::Protocol);
            }
            output.write_all(&chunk).await?;
            offset = end + 1;
        }
        output.sync_all().await?;
        Ok(())
    }

    pub async fn report_inventory(&self, inventory: &Inventory) -> Result<(), ClientError> {
        let response = self
            .client
            .post(self.endpoint("/agent/v1/inventory")?)
            .json(&InventoryRequest {
                schema_version: 1,
                observed_at: chrono::Utc::now(),
                inventory,
            })
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_status(response.status())?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn report_recipe_run_observations(
        &self,
        observations: &[RecipeRunObservation],
    ) -> Result<(), ClientError> {
        if observations.len() > MAX_MANAGED_RECIPE_RUNS {
            return Err(ClientError::Protocol);
        }
        let mut run_ids = std::collections::BTreeSet::new();
        for observation in observations {
            let run_id =
                uuid::Uuid::parse_str(&observation.run_id).map_err(|_| ClientError::Protocol)?;
            if run_id.to_string() != observation.run_id
                || !run_ids.insert(observation.run_id.as_str())
            {
                return Err(ClientError::Protocol);
            }
        }
        let response = self
            .client
            .post(self.endpoint("/agent/v1/recipe-runs/observations")?)
            .json(&RecipeRunObservationsRequest {
                schema_version: 1,
                observed_at: chrono::Utc::now(),
                runs: observations,
            })
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT
            || (response.status() == StatusCode::NOT_FOUND && observations.is_empty())
        {
            Ok(())
        } else {
            classify_status(response.status())?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn report_telemetry(&self, samples: &[TelemetrySample]) -> Result<(), ClientError> {
        if !valid_report_batch(samples) {
            return Err(ClientError::Protocol);
        }
        let response = self
            .client
            .post(self.endpoint("/agent/v1/telemetry")?)
            .timeout(Duration::from_secs(1))
            .json(&TelemetryRequest {
                schema_version: 1,
                samples,
            })
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_status(response.status())?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn renew(&self, csr: &[u8]) -> Result<IssuedResponse, ClientError> {
        let csr = std::str::from_utf8(csr).map_err(|_| ClientError::Protocol)?;
        if csr.is_empty() || csr.len() > 16 * 1024 {
            return Err(ClientError::Protocol);
        }
        let response = self
            .client
            .post(self.endpoint("/agent/v1/renew")?)
            .json(&RenewRequest {
                csr,
                node_id: &self.node_id,
            })
            .send()
            .await?;
        classify_status(response.status())?;
        let body = bounded_body(response).await?;
        let issued: IssuedResponse =
            serde_json::from_slice(&body).map_err(|_| ClientError::Protocol)?;
        if issued.node_id != self.node_id || issued.generation == 0 {
            return Err(ClientError::Protocol);
        }
        Ok(issued)
    }

    pub async fn activate(&self, generation: u64) -> Result<(), ClientError> {
        if generation == 0 {
            return Err(ClientError::Protocol);
        }
        let response = self
            .client
            .post(self.endpoint("/agent/v1/renew/activate")?)
            .json(&ActivateRequest {
                generation,
                node_id: &self.node_id,
            })
            .send()
            .await?;
        if response.status() != StatusCode::NO_CONTENT {
            classify_status(response.status())?;
            return Err(ClientError::Protocol);
        }
        Ok(())
    }

    fn endpoint(&self, path: &str) -> Result<Url, ClientError> {
        self.controller
            .join(path)
            .map_err(|_| ClientError::Protocol)
    }
}

pub fn parse_claim_response(status: u16, body: &[u8]) -> Result<Option<AgentClaim>, ClientError> {
    match status {
        204 if body.is_empty() => Ok(None),
        200 if body.len() <= MAX_BODY_BYTES => {
            let claim: AgentClaim = parse_strict(body).map_err(|_| ClientError::Protocol)?;
            claim.validate().map_err(|_| ClientError::Protocol)?;
            Ok(Some(claim))
        }
        401 | 403 => Err(ClientError::Authentication),
        408 | 429 | 500..=599 => Err(ClientError::Retryable),
        _ => Err(ClientError::Protocol),
    }
}

fn classify_status(status: StatusCode) -> Result<(), ClientError> {
    match status.as_u16() {
        200..=299 => Ok(()),
        401 | 403 => Err(ClientError::Authentication),
        408 | 429 | 500..=599 => Err(ClientError::Retryable),
        _ => Err(ClientError::Protocol),
    }
}

async fn bounded_body(response: reqwest::Response) -> Result<Vec<u8>, ClientError> {
    bounded_body_limit(response, MAX_BODY_BYTES).await
}

async fn bounded_body_limit(
    mut response: reqwest::Response,
    maximum_bytes: usize,
) -> Result<Vec<u8>, ClientError> {
    if response
        .content_length()
        .is_some_and(|length| length > maximum_bytes as u64)
    {
        return Err(ClientError::Protocol);
    }
    let mut body = Vec::with_capacity(response.content_length().unwrap_or(0) as usize);
    while let Some(chunk) = response.chunk().await? {
        if body.len().saturating_add(chunk.len()) > maximum_bytes {
            return Err(ClientError::Protocol);
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn local_hostname() -> Option<String> {
    let raw = fs::read_to_string("/proc/sys/kernel/hostname").ok()?;
    let hostname = raw.trim();
    valid_reported_hostname(hostname).then(|| hostname.to_owned())
}

fn valid_reported_hostname(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && value.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && label
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                && label
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric)
                && label
                    .as_bytes()
                    .last()
                    .is_some_and(u8::is_ascii_alphanumeric)
        })
}

fn valid_oci_digest(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(valid_sha256)
}

#[cfg(test)]
mod tests {
    use super::{AgentHttpClient, ClientError, valid_reported_hostname};
    use crate::{oci::RecipeRunObservation, telemetry::TelemetrySample};
    use chrono::{DateTime, Utc};
    use std::{
        io::{Read, Write},
        net::TcpListener,
        thread,
        time::Duration,
    };
    use url::Url;
    use uuid::Uuid;
    use vonk_agent_protocol::{
        AgentClaim, AgentDirective, AgentProgress, HostRuntimeAction, canonical_json, hex_sha256,
    };

    fn request_capture_client(
        response_status: u16,
        response_headers: Vec<String>,
        response_body: Vec<u8>,
        response_delay: Option<Duration>,
    ) -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            let header_end = loop {
                let size = stream.read(&mut buffer).unwrap();
                assert_ne!(size, 0);
                request.extend_from_slice(&buffer[..size]);
                if let Some(index) = request.windows(4).position(|value| value == b"\r\n\r\n") {
                    break index + 4;
                }
            };
            let headers = std::str::from_utf8(&request[..header_end]).unwrap();
            let content_length = headers
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().unwrap())
                })
                .unwrap();
            while request.len() - header_end < content_length {
                let size = stream.read(&mut buffer).unwrap();
                assert_ne!(size, 0);
                request.extend_from_slice(&buffer[..size]);
            }
            let tolerate_response_write_error = response_delay.is_some();
            if let Some(response_delay) = response_delay {
                thread::sleep(response_delay);
            }
            let response_write = write!(
                stream,
                "HTTP/1.1 {response_status} Test\r\n{}Content-Length: {}\r\nConnection: close\r\n\r\n",
                response_headers
                    .iter()
                    .map(|header| format!("{header}\r\n"))
                    .collect::<String>(),
                response_body.len()
            )
            .and_then(|()| stream.write_all(&response_body));
            if !tolerate_response_write_error {
                response_write.unwrap();
            }
            request
        });
        (
            AgentHttpClient {
                client: reqwest::Client::new(),
                controller: Url::parse(&format!("http://{address}/")).unwrap(),
                node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            },
            server,
        )
    }

    #[test]
    fn reported_hostname_is_bounded_dns_syntax() {
        assert!(valid_reported_hostname("spark-3542"));
        assert!(valid_reported_hostname("spark-3542.lab.internal"));
        assert!(!valid_reported_hostname(""));
        assert!(!valid_reported_hostname("-spark"));
        assert!(!valid_reported_hostname("spark_3542"));
        assert!(!valid_reported_hostname(&"a".repeat(256)));
    }

    fn observation_client(status: u16) -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        request_capture_client(status, Vec::new(), Vec::new(), None)
    }

    fn telemetry_sample(sequence: i64) -> TelemetrySample {
        serde_json::from_value(serde_json::json!({
            "boot_id": "00000000-0000-4000-8000-000000000001",
            "sequence": sequence,
            "observed_at": format!("2026-08-15T12:00:{sequence:02}Z"),
            "cpu_utilization_percent": 12.5,
            "load_average_1m": 1.25,
            "memory_total_bytes": 128000000000_u64,
            "memory_available_bytes": 64000000000_u64,
            "disk_total_bytes": 1000000000000_u64,
            "disk_free_bytes": 750000000000_u64,
            "gpu_utilization_percent": null,
            "gpu_memory_total_bytes": 128000000000_u64,
            "gpu_memory_free_bytes": 63000000000_u64,
            "temperature_c": 41.5,
            "power_watts": 17.25,
            "network_receive_bytes_per_second": 1024.5,
            "network_transmit_bytes_per_second": 512.25,
            "gap_samples": 0,
            "details": {
                "accelerator_name": "NVIDIA GB10",
                "accelerator_performance_state": null
            }
        }))
        .unwrap()
    }

    fn heartbeat_client(
        response: AgentDirective,
    ) -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        let response_body = canonical_json(&response).unwrap();
        request_capture_client(
            200,
            vec!["Content-Type: application/json".to_owned()],
            response_body,
            None,
        )
    }

    fn host_runtime_grant_client() -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        request_capture_client(
            200,
            vec!["Content-Type: application/json".to_owned()],
            br#"{"grant":{}}"#.to_vec(),
            None,
        )
    }

    fn delayed_upload_client(
        response_delay: Duration,
    ) -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        let (base_client, server) =
            request_capture_client(204, Vec::new(), Vec::new(), Some(response_delay));
        let http_client = reqwest::Client::builder()
            .timeout(Duration::from_millis(50))
            .build()
            .unwrap();
        (
            AgentHttpClient {
                client: http_client,
                controller: base_client.controller,
                node_id: base_client.node_id,
            },
            server,
        )
    }

    fn progress() -> AgentProgress {
        AgentProgress {
            attempt: 2,
            deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
            fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
            job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
            progress: serde_json::json!({"phase": "executing"}),
            schema_version: 1,
        }
    }

    #[tokio::test]
    async fn heartbeat_posts_exact_progress_and_accepts_matching_renewal() {
        let progress = progress();
        let directive = AgentDirective {
            attempt: progress.attempt,
            cancel_requested: false,
            deadline: progress.deadline + chrono::Duration::seconds(30),
            fence: progress.fence,
            job_id: progress.job_id,
            node_id: progress.node_id.clone(),
            operation_id: progress.operation_id,
            schema_version: progress.schema_version,
        };
        let (client, server) = heartbeat_client(directive.clone());

        assert_eq!(client.heartbeat(&progress).await.unwrap(), directive);
        let request = server.join().unwrap();
        let (headers, body) = request
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .map(|index| (&request[..index], &request[index + 4..]))
            .unwrap();
        assert!(
            std::str::from_utf8(headers)
                .unwrap()
                .starts_with("POST /agent/v1/heartbeat HTTP/1.1\r\n")
        );
        assert_eq!(
            serde_json::from_slice::<AgentProgress>(body).unwrap(),
            progress
        );
    }

    #[tokio::test]
    async fn heartbeat_rejects_mismatched_or_regressing_renewal() {
        let progress = progress();
        let directive = AgentDirective {
            attempt: progress.attempt,
            cancel_requested: false,
            deadline: progress.deadline - chrono::Duration::seconds(1),
            fence: progress.fence,
            job_id: progress.job_id,
            node_id: progress.node_id.clone(),
            operation_id: progress.operation_id,
            schema_version: progress.schema_version,
        };
        let (client, server) = heartbeat_client(directive);

        assert!(matches!(
            client.heartbeat(&progress).await,
            Err(ClientError::Protocol)
        ));
        server.join().unwrap();
    }

    #[tokio::test]
    async fn host_runtime_grant_ttl_fits_inside_renewed_operation_lease() {
        let payload = serde_json::json!({});
        let claim = AgentClaim {
            schema_version: 1,
            job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
            operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
            attempt: 1,
            fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            operation: "recipe.image.import.v1".to_owned(),
            authority_revision: "a".repeat(64),
            payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
            payload,
            deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
        };
        let (client, server) = host_runtime_grant_client();

        client
            .host_runtime_grant(&claim, HostRuntimeAction::ImageImport, &"c".repeat(64))
            .await
            .unwrap();
        let request = server.join().unwrap();
        let body = request
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .map(|index| &request[index + 4..])
            .unwrap();
        let body: serde_json::Value = serde_json::from_slice(body).unwrap();

        assert_eq!(body["expires_in_seconds"], 10);
    }

    #[tokio::test]
    async fn recipe_run_observations_post_strict_bounded_shape_and_accept_only_204() {
        let observations = vec![RecipeRunObservation {
            run_id: "45ea6921-50c9-4971-be2a-4cd04ce05069".to_owned(),
            ready: true,
        }];
        let (client, server) = observation_client(204);

        client
            .report_recipe_run_observations(&observations)
            .await
            .unwrap();

        let request = server.join().unwrap();
        let (headers, body) = request
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .map(|index| (&request[..index], &request[index + 4..]))
            .unwrap();
        let headers = std::str::from_utf8(headers).unwrap();
        assert!(headers.starts_with("POST /agent/v1/recipe-runs/observations HTTP/1.1\r\n"));
        let body: serde_json::Value = serde_json::from_slice(body).unwrap();
        let mut keys = body
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        keys.sort();
        assert_eq!(keys, ["observed_at", "runs", "schema_version"]);
        assert_eq!(body["schema_version"], 1);
        assert_eq!(
            body["runs"],
            serde_json::json!([{
                "ready": true,
                "run_id": "45ea6921-50c9-4971-be2a-4cd04ce05069"
            }])
        );
        let observed_at = DateTime::parse_from_rfc3339(body["observed_at"].as_str().unwrap())
            .unwrap()
            .with_timezone(&Utc);
        assert!((Utc::now() - observed_at).num_seconds().abs() < 5);

        let (client, server) = observation_client(200);
        assert!(matches!(
            client.report_recipe_run_observations(&observations).await,
            Err(ClientError::Protocol)
        ));
        server.join().unwrap();
    }

    #[tokio::test]
    async fn absent_observation_endpoint_is_optional_only_without_managed_runs() {
        let (client, server) = observation_client(404);

        client.report_recipe_run_observations(&[]).await.unwrap();
        server.join().unwrap();

        let observations = vec![RecipeRunObservation {
            run_id: "45ea6921-50c9-4971-be2a-4cd04ce05069".to_owned(),
            ready: true,
        }];
        let (client, server) = observation_client(404);
        assert!(matches!(
            client.report_recipe_run_observations(&observations).await,
            Err(ClientError::Protocol)
        ));
        server.join().unwrap();
    }

    #[tokio::test]
    async fn absent_endpoint_compatibility_does_not_mask_authentication_errors() {
        for status in [401, 403] {
            let (client, server) = observation_client(status);
            assert!(matches!(
                client.report_recipe_run_observations(&[]).await,
                Err(ClientError::Authentication)
            ));
            server.join().unwrap();
        }
    }

    #[tokio::test]
    async fn telemetry_posts_exact_task_three_shape_without_node_identity() {
        let sample = telemetry_sample(1);
        let (client, server) = observation_client(204);

        client
            .report_telemetry(std::slice::from_ref(&sample))
            .await
            .unwrap();

        let request = server.join().unwrap();
        let (headers, body) = request
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .map(|index| (&request[..index], &request[index + 4..]))
            .unwrap();
        let headers = std::str::from_utf8(headers).unwrap().to_ascii_lowercase();
        assert!(headers.starts_with("post /agent/v1/telemetry http/1.1\r\n"));
        assert!(headers.contains("content-type: application/json"));

        let body: serde_json::Value = serde_json::from_slice(body).unwrap();
        assert_eq!(
            body.as_object()
                .unwrap()
                .keys()
                .cloned()
                .collect::<Vec<_>>(),
            ["schema_version", "samples"]
        );
        assert_eq!(body["schema_version"], 1);
        assert_eq!(body["samples"].as_array().unwrap().len(), 1);
        let sample = body["samples"][0].as_object().unwrap();
        let mut keys = sample.keys().cloned().collect::<Vec<_>>();
        keys.sort();
        assert_eq!(
            keys,
            [
                "boot_id",
                "cpu_utilization_percent",
                "details",
                "disk_free_bytes",
                "disk_total_bytes",
                "gap_samples",
                "gpu_memory_free_bytes",
                "gpu_memory_total_bytes",
                "gpu_utilization_percent",
                "load_average_1m",
                "memory_available_bytes",
                "memory_total_bytes",
                "network_receive_bytes_per_second",
                "network_transmit_bytes_per_second",
                "observed_at",
                "power_watts",
                "sequence",
                "temperature_c",
            ]
        );
        assert!(!body.to_string().contains("node_id"));
        assert_eq!(sample["gpu_utilization_percent"], serde_json::Value::Null);
        assert_eq!(
            sample["details"],
            serde_json::json!({
                "accelerator_name": "NVIDIA GB10",
                "accelerator_performance_state": null
            })
        );
    }

    #[tokio::test]
    async fn telemetry_rejects_empty_or_more_than_sixteen_samples_before_transport() {
        let client = AgentHttpClient {
            client: reqwest::Client::new(),
            controller: Url::parse("http://127.0.0.1:9/").unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
        };
        assert!(matches!(
            client.report_telemetry(&[]).await,
            Err(ClientError::Protocol)
        ));
        let samples = (0..17).map(telemetry_sample).collect::<Vec<_>>();
        assert!(matches!(
            client.report_telemetry(&samples).await,
            Err(ClientError::Protocol)
        ));
    }

    #[tokio::test]
    async fn telemetry_accepts_only_204_and_preserves_status_classification() {
        let samples = [telemetry_sample(1)];
        for (status, expected) in [
            (200, "protocol"),
            (401, "authentication"),
            (429, "retryable"),
        ] {
            let (client, server) = observation_client(status);
            let error = client.report_telemetry(&samples).await.unwrap_err();
            server.join().unwrap();
            assert!(
                matches!(
                    (&error, expected),
                    (ClientError::Protocol, "protocol")
                        | (ClientError::Authentication, "authentication")
                        | (ClientError::Retryable, "retryable")
                ),
                "status {status} classified as {error:?}"
            );
        }
    }

    #[tokio::test]
    async fn recipe_run_observations_reject_unbounded_payload_before_transport() {
        let client = AgentHttpClient {
            client: reqwest::Client::new(),
            controller: Url::parse("http://127.0.0.1:9/").unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
        };
        let observations = (0..=64)
            .map(|value| RecipeRunObservation {
                run_id: uuid::Uuid::from_u128(value).to_string(),
                ready: false,
            })
            .collect::<Vec<_>>();

        assert!(matches!(
            client.report_recipe_run_observations(&observations).await,
            Err(ClientError::Protocol)
        ));
    }

    #[tokio::test]
    async fn recipe_image_upload_overrides_the_short_ordinary_request_timeout() {
        let directory = tempfile::tempdir().unwrap();
        let archive = directory.path().join("image.docker.tar");
        std::fs::write(&archive, b"accepted archive").unwrap();
        let (client, server) = delayed_upload_client(Duration::from_millis(150));

        let result = client
            .upload_recipe_image(
                Uuid::parse_str("45ea6921-50c9-4971-be2a-4cd04ce05069").unwrap(),
                &format!("sha256:{}", "b".repeat(64)),
                &"a".repeat(64),
                16,
                &archive,
            )
            .await;
        let request = server.join().unwrap();

        assert!(
            result.is_ok(),
            "large upload inherited ordinary timeout: {result:?}"
        );
        assert!(request.starts_with(b"PUT /agent/v1/recipe-builds/"));
        assert!(request.ends_with(b"accepted archive"));
    }
}
