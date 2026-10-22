//! Reporting for the client boundary.

use super::*;

impl AgentHttpClient {
    pub async fn report_inventory(
        &self,
        inventory: &Inventory,
    ) -> Result<Option<InventoryVerdict>, ClientError> {
        self.report_inventory_request(inventory.to_request(chrono::Utc::now().into()))
            .await
    }

    /// Report one inventory. A Controller that judged the installations it
    /// named answers its verdict; an older one, or an inventory that named
    /// none, answers nothing (`None`).
    ///
    /// An older Controller refuses optional evidence it does not know (HTTP
    /// 422 on an unknown field). The capacity core must still arrive, so a
    /// refused report that carried optional evidence is sent once more as its
    /// core alone.
    pub async fn report_inventory_request(
        &self,
        mut request: InventoryRequest,
    ) -> Result<Option<InventoryVerdict>, ClientError> {
        for warning in clamp_inventory_request(&mut request) {
            eprintln!("vonk-agent: inventory evidence dropped: {warning}");
        }
        request.validate().map_err(|_| ClientError::Protocol)?;
        let core = inventory_core(&request);
        match self.post_inventory(&request).await {
            Err(ClientError::Controller(error)) if error.request_invalid() && core != request => {
                eprintln!(
                    "vonk-agent: the Controller refused the inventory's optional evidence (HTTP {}); reporting its core",
                    error.status
                );
                self.post_inventory(&core).await
            }
            answer => answer,
        }
    }

    async fn post_inventory(
        &self,
        request: &InventoryRequest,
    ) -> Result<Option<InventoryVerdict>, ClientError> {
        let body = canonical_generated_json(request).map_err(|_| ClientError::Protocol)?;
        let mut response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/inventory")?)
            .timeout(CONTROLLER_REQUEST_TIMEOUT)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        match response.status() {
            StatusCode::NO_CONTENT => Ok(None),
            StatusCode::OK => {
                // The inventory is recorded; a verdict that cannot be read is
                // no verdict, and nothing is removed on it.
                let verdict = bounded_body_limit(response, MAX_VERDICT_BODY_BYTES)
                    .await
                    .ok()
                    .and_then(|body| parse_strict::<InventoryVerdict>(&body).ok());
                if verdict.is_none() {
                    eprintln!("vonk-agent: the Controller's inventory verdict is unreadable");
                }
                Ok(verdict)
            }
            _ => {
                classify_response(&mut response).await?;
                Err(ClientError::Protocol)
            }
        }
    }

    pub async fn report_exact_recipe_run_observations(
        &self,
        observed_at: chrono::DateTime<chrono::Utc>,
        observations: &[ExactRecipeRunObservation],
    ) -> Result<(), ClientError> {
        let envelope = build_exact_recipe_run_observations(observed_at, observations)?;
        let body = canonical_generated_json(&envelope).map_err(|_| ClientError::Protocol)?;
        let mut response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/recipe-runs/observations")?)
            .timeout(CONTROLLER_REQUEST_TIMEOUT)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_response(&mut response).await?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn report_telemetry(&self, samples: &[TelemetrySample]) -> Result<(), ClientError> {
        if !valid_report_batch(samples) {
            return Err(ClientError::Protocol);
        }
        let request = TelemetryRequest {
            samples: samples.to_vec(),
        };
        let body = canonical_generated_json(&request).map_err(|_| ClientError::Protocol)?;
        let mut response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/telemetry")?)
            .timeout(Duration::from_secs(2))
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_response(&mut response).await?;
            Err(ClientError::Protocol)
        }
    }
}

/// Bring locally observed inventory inside the wire contract instead of
/// refusing the whole report: out-of-range byte counts are clamped, a free
/// value never exceeds its total, invalid or duplicate capabilities and an
/// incomplete fabric pair are omitted.  Nothing is invented: a missing driver
/// or runtime version still fails validation and the report is retried.
///
/// The network interfaces, the NAS route and the fabric pair are optional
/// evidence: an inconsistent part is dropped (an interface with an invalid or
/// repeated name, a route naming no reported interface, an invalid fabric
/// address) and named in the returned typed warnings, never allowed to fail
/// the mandatory capacity report.
/// The mandatory capacity core of an inventory, every optional evidence group
/// left out: what any Controller accepts. Every field is named, so a new one
/// does not compile until it is classified as core or optional.
pub(super) fn inventory_core(request: &InventoryRequest) -> InventoryRequest {
    let InventoryRequest {
        schema_version,
        observed_at,
        disk_total_bytes,
        disk_free_bytes,
        host_memory_total_bytes,
        host_memory_free_bytes,
        gpu_memory_total_bytes,
        gpu_memory_free_bytes,
        gpu_count,
        gpu_health,
        memory_pool,
        artifact_store_read_only,
        capabilities,
        nvidia_driver_version,
        container_runtime_version,
        // Optional evidence: an older Controller may not know it.
        fabric_address: _,
        fabric_bandwidth_mbps: _,
        network_interfaces: _,
        nas_route_interface: _,
        host_settings: _,
        installations: _,
        unused_model_bytes: _,
        model_objects: _,
        runs: _,
        installation_bytes: _,
    } = request.clone();
    InventoryRequest {
        schema_version,
        observed_at,
        disk_total_bytes,
        disk_free_bytes,
        host_memory_total_bytes,
        host_memory_free_bytes,
        gpu_memory_total_bytes,
        gpu_memory_free_bytes,
        gpu_count,
        gpu_health,
        memory_pool,
        artifact_store_read_only,
        capabilities,
        nvidia_driver_version,
        container_runtime_version,
        fabric_address: None,
        fabric_bandwidth_mbps: None,
        network_interfaces: None,
        nas_route_interface: None,
        host_settings: None,
        installations: None,
        unused_model_bytes: None,
        model_objects: None,
        runs: None,
        installation_bytes: None,
    }
}

pub(super) fn clamp_inventory_request(request: &mut InventoryRequest) -> Vec<AgentEvidenceCode> {
    let mut warnings = Vec::new();
    const MAX_BYTES: u64 = 16 * 1024_u64.pow(4);
    if let Some(bytes) = &mut request.unused_model_bytes {
        *bytes = (*bytes).min(MAX_BYTES);
    }
    for size in request.installation_bytes.iter_mut().flatten() {
        size.bytes = size.bytes.min(MAX_BYTES);
    }
    for value in [
        &mut request.disk_total_bytes,
        &mut request.disk_free_bytes,
        &mut request.host_memory_total_bytes,
        &mut request.host_memory_free_bytes,
        &mut request.gpu_memory_total_bytes,
        &mut request.gpu_memory_free_bytes,
    ] {
        *value = (*value).min(MAX_BYTES);
    }
    request.disk_free_bytes = request.disk_free_bytes.min(request.disk_total_bytes);
    request.host_memory_free_bytes = request
        .host_memory_free_bytes
        .min(request.host_memory_total_bytes);
    request.gpu_memory_free_bytes = request
        .gpu_memory_free_bytes
        .min(request.gpu_memory_total_bytes);
    request.gpu_count = request.gpu_count.min(64);
    let mut seen = std::collections::BTreeSet::new();
    request.capabilities.retain(|value| {
        let valid = !value.is_empty()
            && value.len() <= 128
            && value.bytes().enumerate().all(|(index, byte)| {
                byte.is_ascii_lowercase()
                    || byte.is_ascii_digit()
                    || (index > 0 && matches!(byte, b'.' | b'_' | b'-'))
            });
        valid && seen.insert(value.clone())
    });
    request.capabilities.truncate(64);
    for version in [
        &mut request.nvidia_driver_version,
        &mut request.container_runtime_version,
    ] {
        version.retain(|character| character.is_ascii());
        version.truncate(256);
    }
    let fabric_valid = request.fabric_address.as_deref().is_some_and(|value| {
        value.len() <= 45
            && value
                .parse::<std::net::IpAddr>()
                .is_ok_and(|address| address.to_string() == value)
    }) && request
        .fabric_bandwidth_mbps
        .is_some_and(|value| (1..=1_000_000).contains(&value));
    if !fabric_valid {
        if request.fabric_address.is_some() || request.fabric_bandwidth_mbps.is_some() {
            warnings.push(AgentEvidenceCode::AgentEvidenceInventoryFabricDropped);
        }
        request.fabric_address = None;
        request.fabric_bandwidth_mbps = None;
    }
    crate::network::clamp_network_evidence(request, &mut warnings);
    warnings.extend(crate::inventory::bound_model_object_evidence(request));
    warnings
}

pub(super) fn local_hostname() -> Option<String> {
    let raw = fs::read_to_string("/proc/sys/kernel/hostname").ok()?;
    let hostname = raw.trim();
    valid_reported_hostname(hostname).then(|| hostname.to_owned())
}

pub(super) fn valid_reported_hostname(value: &str) -> bool {
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

#[cfg(test)]
mod tests;
