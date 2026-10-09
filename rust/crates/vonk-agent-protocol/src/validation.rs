//! Validation.

use super::*;

#[derive(Debug, Error)]
pub enum ProtocolError {
    #[error("protocol JSON is invalid")]
    Json(#[from] serde_json::Error),
    #[error("protocol identity is invalid: {0}")]
    Identity(&'static str),
}

impl AgentClaim {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if !matches!(
            self.operation,
            generated::AgentOperation::AgentUpgradeV1
                | generated::AgentOperation::RuntimePreflightV1
                | generated::AgentOperation::ArtifactDistributionV1
                | generated::AgentOperation::RecipeBuildV1
                | generated::AgentOperation::RecipeBuildCleanupV1
                | generated::AgentOperation::RecipeJobRunV1
                | generated::AgentOperation::RecipeInstall
                | generated::AgentOperation::RecipeStart
                | generated::AgentOperation::RecipeStop
                | generated::AgentOperation::RecipeUninstall
                | generated::AgentOperation::RecipeReconcile
        ) {
            return Err(ProtocolError::Identity("claim operation"));
        }
        let payload = canonical_json(&self.payload)?;
        let maximum_bytes = if matches!(
            self.operation,
            generated::AgentOperation::RecipeInstall
                | generated::AgentOperation::RecipeStart
                | generated::AgentOperation::RecipeStop
                | generated::AgentOperation::RecipeUninstall
                | generated::AgentOperation::RecipeJobRunV1
        ) {
            MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES
        } else {
            MAX_DOCUMENT_BYTES
        };
        if payload.len() > maximum_bytes {
            return Err(ProtocolError::Identity("claim payload size"));
        }
        Ok(())
    }
}

/// Agent command to consume one Controller assignment. The destination is
/// selected by the agent configuration, so claims cannot choose a filesystem
/// path; only the plan identity crosses the wire.
/// Canonical schema-1 inventory evidence reported by a Spark agent.
impl InventoryRequest {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || self.disk_total_bytes > 16 * 1024_u64.pow(4)
            || self.disk_free_bytes > 16 * 1024_u64.pow(4)
            || self.host_memory_total_bytes > 16 * 1024_u64.pow(4)
            || self.host_memory_free_bytes > 16 * 1024_u64.pow(4)
            || self.gpu_memory_total_bytes > 16 * 1024_u64.pow(4)
            || self.gpu_memory_free_bytes > 16 * 1024_u64.pow(4)
            || self.gpu_count > 64
            || self.disk_free_bytes > self.disk_total_bytes
            || self.host_memory_free_bytes > self.host_memory_total_bytes
            || self.gpu_memory_free_bytes > self.gpu_memory_total_bytes
            || (self.memory_pool == MemoryPool::Shared && self.gpu_count == 0)
            || self.capabilities.len() > 64
            || self
                .capabilities
                .iter()
                .any(|value| !valid_inventory_capability(value))
            || {
                let mut unique = BTreeSet::new();
                self.capabilities.iter().any(|value| !unique.insert(value))
            }
            || self.nvidia_driver_version.is_empty()
            || self.container_runtime_version.is_empty()
            || self.nvidia_driver_version.len() > 256
            || self.container_runtime_version.len() > 256
            || !self.nvidia_driver_version.is_ascii()
            || !self.container_runtime_version.is_ascii()
            || !self.network_evidence_is_consistent()
            || (self.fabric_address.is_none() != self.fabric_bandwidth_mbps.is_none())
            || self
                .fabric_bandwidth_mbps
                .is_some_and(|value| !(1..=1_000_000).contains(&value))
        {
            return Err(ProtocolError::Identity("inventory request"));
        }
        Ok(())
    }
}

impl InventoryRequest {
    pub(super) fn network_evidence_is_consistent(&self) -> bool {
        let interfaces = self.network_interfaces.as_deref().unwrap_or_default();
        let mut names = BTreeSet::new();
        interfaces.len() <= 16
            && interfaces.iter().all(|value| {
                valid_interface_name(&value.name)
                    && names.insert(value.name.as_str())
                    && value
                        .link_speed_mbps
                        .is_none_or(|speed| (1..=1_000_000).contains(&speed))
            })
            && self
                .nas_route_interface
                .as_deref()
                .is_none_or(|route| names.contains(route))
    }
}

/// The interface-name rule of the inventory wire contract: at most 15 ASCII
/// characters, alphanumeric first, then alphanumerics and `._:-`.
pub fn valid_interface_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 15
        && value.starts_with(|first: char| first.is_ascii_alphanumeric())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

pub(super) fn valid_inventory_capability(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || (index > 0 && matches!(byte, b'.' | b'_' | b'-'))
        })
}

impl ArtifactDistributionRequest {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if !lower_hex(&self.plan_digest, 64) {
            return Err(ProtocolError::Identity("artifact distribution request"));
        }
        Ok(())
    }
}

impl AgentUpgradeRequest {
    pub fn parse(claim: &AgentClaim) -> Result<Self, ProtocolError> {
        if claim.operation != generated::AgentOperation::AgentUpgradeV1 {
            return Err(ProtocolError::Identity("agent upgrade operation"));
        }
        let generated::AgentClaimPayload::AgentUpgradePayload(value) = &claim.payload else {
            return Err(ProtocolError::Identity("agent upgrade payload"));
        };
        let value = value.clone();
        let url = url::Url::parse(&value.package_url)
            .map_err(|_| ProtocolError::Identity("agent upgrade URL"))?;
        let source_url = url::Url::parse(&value.source_package_url)
            .map_err(|_| ProtocolError::Identity("rollback package URL"))?;
        if !value.rollback.valid()
            || !(1..=1024 * 1024 * 1024).contains(&value.source_package_bytes)
            || source_url.scheme() != "https"
            || source_url.host_str() != Some("install.vonkforge.ai")
            || source_url.port().is_some()
            || !source_url.username().is_empty()
            || source_url.password().is_some()
            || source_url.query().is_some()
            || source_url.fragment().is_some()
            || !source_url.path().ends_with("/vonk-forge-agent.deb")
            || value.schema_version != 1
            || value.architecture != "linux-arm64"
            || !(1..=1024 * 1024 * 1024).contains(&value.package_bytes)
            || !lower_hex(&value.package_sha256, 64)
            || !lower_hex(&value.package_signature, 128)
            || !lower_hex(&value.target_binary_digest, 64)
            || !value.target_build_digest.starts_with("sha256:")
            || !lower_hex(&value.target_build_digest[7..], 64)
            || value.package_version.is_empty()
            || value.package_version.len() > 128
            || !value
                .package_version
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphanumeric)
            || value
                .package_version
                .bytes()
                .any(|byte| !byte.is_ascii_alphanumeric() && !b".+~-".contains(&byte))
            || !value
                .package_url
                .starts_with("https://install.vonkforge.ai/")
            || url.scheme() != "https"
            || url.host_str() != Some("install.vonkforge.ai")
            || url.port().is_some()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || !url.path().ends_with("/vonk-forge-agent.deb")
        {
            return Err(ProtocolError::Identity("agent upgrade payload"));
        }
        Ok(value)
    }
}

impl AgentProgress {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if let Some(progress) = &self.progress {
            if canonical_json(progress)?.len() > MAX_DOCUMENT_BYTES {
                return Err(ProtocolError::Identity("progress document"));
            }
            progress.validate()?;
        }
        Ok(())
    }
}

/// A content-addressed object authorized by one exact Controller assignment.
impl DistributionObject {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        let valid_name = !self.name.is_empty()
            && self.name.chars().count() <= 512
            && !self.name.starts_with('/')
            && !self.name.contains(['\\', '\0']);
        if !valid_name
            || self
                .name
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == "..")
            || !lower_hex(&self.sha256, 64)
            || self.bytes > 16 * 1024_u64.pow(4)
            || self.bytes == 0
                && !(self.kind == "model"
                    && self.sha256
                        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
            || self.kind != "model"
        {
            return Err(ProtocolError::Identity("distribution object"));
        }
        Ok(())
    }
}

/// The model object set of one distribution plan and the runtime image the
/// node pulls by digest from the Controller's layered store. The plan digest
/// scopes every object fetch, so an enrolled agent cannot turn a digest into a
/// general object browser.
impl DistributionAssignment {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if !valid_oci_digest(&self.oci_image_digest)
            || !valid_oci_digest(&self.oci_image_config_digest)
            || self.objects.is_empty()
            || self.objects.len() > 4096
        {
            return Err(ProtocolError::Identity("distribution assignment"));
        }
        let mut digests = BTreeSet::new();
        for object in &self.objects {
            object.validate()?;
            if !digests.insert(object.sha256.as_str()) {
                return Err(ProtocolError::Identity("distribution object duplicate"));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod inventory_tests {
    use super::*;

    fn inventory() -> InventoryRequest {
        InventoryRequest {
            schema_version: 1,
            observed_at: "2026-08-03T00:00:00Z".parse().unwrap(),
            disk_total_bytes: 2 * 1024_u64.pow(4),
            disk_free_bytes: 1024_u64.pow(4),
            host_memory_total_bytes: 2 * 1024_u64.pow(4),
            host_memory_free_bytes: 1024_u64.pow(4),
            gpu_memory_total_bytes: 100_000,
            gpu_memory_free_bytes: 80_000,
            gpu_count: 1,
            memory_pool: MemoryPool::Separate,
            artifact_store_read_only: false,
            capabilities: vec![generated::AgentOperation::RecipeBuildV1.to_string()],
            fabric_address: None,
            fabric_bandwidth_mbps: None,
            network_interfaces: None,
            nas_route_interface: None,
            nvidia_driver_version: "550.1".to_owned(),
            container_runtime_version: "podman-5".to_owned(),
        }
    }

    #[test]
    fn inventory_validation_matches_python_bounds_and_shapes() {
        let value = inventory();
        value.validate().unwrap();

        let mut invalid = value.clone();
        invalid.gpu_count = 65;
        assert!(invalid.validate().is_err());
        let mut invalid = value.clone();
        invalid.capabilities = vec!["Recipe.Build".to_owned()];
        assert!(invalid.validate().is_err());
        let mut invalid = value.clone();
        invalid.fabric_bandwidth_mbps = Some(0);
        assert!(invalid.validate().is_err());
        let mut invalid = value;
        invalid.nvidia_driver_version = "é".to_owned();
        assert!(invalid.validate().is_err());
    }

    #[test]
    fn network_evidence_must_name_its_route_interface() {
        let wifi = NetworkInterface {
            name: "wlP9s9".to_owned(),
            kind: NetworkInterfaceKind::Wifi,
            link_speed_mbps: None,
            carrier: true,
        };
        let mut value = inventory();
        value.network_interfaces = Some(vec![wifi.clone()]);
        value.nas_route_interface = Some("wlP9s9".to_owned());
        value.validate().unwrap();
        value.nas_route_interface = Some("enP7s7".to_owned());
        assert!(value.validate().is_err());
        value.nas_route_interface = None;
        value.network_interfaces = Some(vec![wifi.clone(), wifi]);
        assert!(value.validate().is_err());
    }
}

#[cfg(test)]
mod distribution_tests {
    use super::*;

    fn assignment() -> DistributionAssignment {
        DistributionAssignment {
            objects: vec![DistributionObject {
                name: "weights/model.bin".to_owned(),
                sha256: "d".repeat(64),
                bytes: 13,
                kind: "model".parse().unwrap(),
            }],
            oci_image_digest: "sha256:".to_owned() + &"f".repeat(64),
            oci_image_config_digest: "sha256:".to_owned() + &"e".repeat(64),
        }
    }

    #[test]
    fn assignment_serde_round_trip_matches_python_wire_shape() {
        let value = assignment();
        value.validate().unwrap();
        let encoded = serde_json::to_value(&value).unwrap();
        let decoded: DistributionAssignment = serde_json::from_value(encoded.clone()).unwrap();
        assert_eq!(decoded, value);
        assert_eq!(serde_json::to_value(decoded).unwrap(), encoded);
    }

    #[test]
    fn object_name_validation_matches_python_boundary() {
        let mut value = assignment();
        for name in [
            "weights/model bin",
            "__init__.py",
            "nested/UPPERCASE.bin",
            "模型.bin",
        ] {
            value.objects[0].name = name.to_owned();
            value.validate().unwrap();
        }
        value.objects[0].name = "模型 file_".repeat(64);
        value.validate().unwrap();
        value.objects[0].name = "../model.bin".to_owned();
        assert!(value.validate().is_err());
        value.objects[0].name = "model\0.bin".to_owned();
        assert!(value.validate().is_err());
    }

    #[test]
    fn empty_model_support_object_uses_the_canonical_empty_digest() {
        let mut value = assignment();
        value.objects[0] = DistributionObject {
            name: "tokenizer_config.json".to_owned(),
            sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855".to_owned(),
            bytes: 0,
            kind: "model".parse().unwrap(),
        };
        value.validate().unwrap();
        value.objects[0].kind = "oci-archive".parse().unwrap();
        assert!(value.validate().is_err());
    }
}
