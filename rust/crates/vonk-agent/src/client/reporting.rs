//! Reporting for the client boundary.

use super::*;

impl AgentHttpClient {
    pub async fn report_inventory(&self, inventory: &Inventory) -> Result<(), ClientError> {
        let mut request = inventory.to_request(chrono::Utc::now().into());
        clamp_inventory_request(&mut request);
        for warning in clamp_inventory_request(&mut request) {
            eprintln!("vonk-agent: inventory evidence dropped: {warning}");
        }
        request.validate().map_err(|_| ClientError::Protocol)?;
        let body = canonical_generated_json(&request).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/inventory")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_response(&response)?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn report_exact_recipe_run_observations(
        &self,
        observed_at: chrono::DateTime<chrono::Utc>,
        observations: &[ExactRecipeRunObservation],
    ) -> Result<(), ClientError> {
        let envelope = build_exact_recipe_run_observations(observed_at, observations)?;
        let body = canonical_generated_json(&envelope).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await?
            .post(self.endpoint("/agent/recipe-runs/observations")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_response(&response)?;
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
        let response = self
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
            classify_response(&response)?;
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
pub(super) fn clamp_inventory_request(request: &mut InventoryRequest) -> Vec<AgentEvidenceCode> {
    let mut warnings = Vec::new();
    const MAX_BYTES: u64 = 16 * 1024_u64.pow(4);
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
    clamp_network_evidence(request, &mut warnings);
    warnings
}

/// Keep the consistent part of the NIC evidence; see `clamp_inventory_request`.
pub(super) fn clamp_network_evidence(
    request: &mut InventoryRequest,
    warnings: &mut Vec<AgentEvidenceCode>,
) {
    const MAX_INTERFACES: usize = 16;
    if let Some(interfaces) = request.network_interfaces.as_mut() {
        let reported = interfaces.len();
        let mut seen = std::collections::BTreeSet::new();
        interfaces.retain_mut(|interface| {
            if !vonk_agent_protocol::valid_interface_name(&interface.name)
                || !seen.insert(interface.name.clone())
            {
                return false;
            }
            if interface
                .link_speed_mbps
                .is_some_and(|speed| !(1..=1_000_000).contains(&speed))
            {
                interface.link_speed_mbps = None;
            }
            true
        });
        if interfaces.len() > MAX_INTERFACES {
            // Keep the interface the NAS route uses when the list must be bounded.
            let route = request.nas_route_interface.clone();
            interfaces.sort_by_key(|interface| Some(&interface.name) != route.as_ref());
            interfaces.truncate(MAX_INTERFACES);
        }
        if interfaces.len() != reported {
            warnings.push(AgentEvidenceCode::AgentEvidenceInventoryNetworkInterfaceDropped);
        }
    }
    let route_reported = request.nas_route_interface.as_deref().is_none_or(|route| {
        request
            .network_interfaces
            .as_deref()
            .is_some_and(|interfaces| interfaces.iter().any(|value| value.name == route))
    });
    if !route_reported {
        request.nas_route_interface = None;
        warnings.push(AgentEvidenceCode::AgentEvidenceInventoryNasRouteDropped);
    }
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
