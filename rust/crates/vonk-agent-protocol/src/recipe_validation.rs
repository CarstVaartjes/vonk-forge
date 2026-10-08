//! Recipe validation.

use super::*;

pub(super) fn validate_recipe_job(value: &RecipeJobRunRequest) -> bool {
    let inputs_valid = value.inputs.len() <= 32
        && value
            .inputs
            .windows(2)
            .all(|pair| pair[0].name < pair[1].name)
        && value.inputs.iter().all(|file| {
            valid_job_slot(&file.slot)
                && valid_job_file_name(&file.name)
                && valid_media_type(&file.media_type)
                && file.size_bytes <= 512 * 1024 * 1024
                && lower_hex(&file.sha256, 64)
        })
        && value.inputs.iter().try_fold(0_u64, |total, file| {
            total.checked_add(u64::from(file.size_bytes))
        }) == Some(u64::from(value.input_total_bytes))
        && value.input_total_bytes <= 1024 * 1024 * 1024;
    let manifest = generated::RecipeJobInputManifest {
        schema_version: 1,
        total_bytes: value.input_total_bytes,
        files: value.inputs.clone(),
    };
    let manifest_valid = canonical_json(&manifest)
        .ok()
        .is_some_and(|bytes| hex_sha256(&bytes) == value.input_manifest_sha256);
    let limits = &value.output_limits;
    let mappings_valid = (1..=32).contains(&value.output_mappings.len())
        && value
            .output_mappings
            .windows(2)
            .all(|pair| pair[0].slot < pair[1].slot)
        && value.output_mappings.iter().all(|mapping| {
            valid_job_slot(&mapping.slot)
                && valid_media_type(&mapping.media_type)
                && (1..=16).contains(&mapping.extensions.len())
                && mapping.extensions.windows(2).all(|pair| pair[0] < pair[1])
                && mapping
                    .extensions
                    .iter()
                    .all(|extension| valid_job_extension(extension))
        })
        && value
            .output_mappings
            .iter()
            .flat_map(|mapping| mapping.extensions.iter())
            .collect::<BTreeSet<_>>()
            .len()
            == value
                .output_mappings
                .iter()
                .map(|mapping| mapping.extensions.len())
                .sum::<usize>();
    let plan = &value.compiled_execution_plan;
    value.job_id.get_version() == Some(uuid::Version::Random)
        && value.run_id.get_version() == Some(uuid::Version::Random)
        && value.installation_id.get_version() == Some(uuid::Version::Random)
        && value.recipe_revision_id.get_version() == Some(uuid::Version::Random)
        && value.mapping_id.get_version() == Some(uuid::Version::Random)
        && (1..=i64::MAX as u64).contains(&value.run_generation)
        && lower_hex(&value.plan_digest, 64)
        && plan.job.is_some()
        && plan.runtime.placement.world_size == 1
        && inputs_valid
        && manifest_valid
        && mappings_valid
        && (1..=32).contains(&limits.max_files)
        && (1..=1024 * 1024 * 1024).contains(&limits.max_file_bytes)
        && (1..=2 * 1024 * 1024 * 1024).contains(&limits.max_total_bytes)
        && limits.max_file_bytes <= limits.max_total_bytes
        && !limits.allowed_media_types.is_empty()
        && limits.allowed_media_types.len() <= 16
        && limits
            .allowed_media_types
            .iter()
            .enumerate()
            .all(|(index, media_type)| {
                valid_media_type(media_type)
                    && !limits.allowed_media_types[..index].contains(media_type)
                    && (index == 0 || limits.allowed_media_types[index - 1] < *media_type)
            })
        && limits.allowed_media_types.iter().all(|allowed| {
            value
                .output_mappings
                .iter()
                .any(|mapping| &mapping.media_type == allowed)
        })
}

pub(super) fn validate_recipe_stop(value: &RecipeStopRequest) -> bool {
    value.run_id.get_version() == Some(uuid::Version::Random)
        && value.target_runtime_id.get_version() == Some(uuid::Version::Random)
        && (1..=i64::MAX as u64).contains(&value.run_generation)
        && value.installation_id.get_version() == Some(uuid::Version::Random)
        && value.recipe_revision_id.get_version() == Some(uuid::Version::Random)
        && value.mapping_id.get_version() == Some(uuid::Version::Random)
        && lower_hex(&value.plan_digest, 64)
        && lower_hex(&value.recipe_content_sha256, 64)
        && valid_role(&value.role)
        && (1..=600).contains(&value.stop_timeout_seconds)
}

/// A start's placement, addresses and image all come from its compiled plan;
/// the payload adds only the run identity and the distributed phase.
pub(super) fn validate_recipe_start(value: &RecipeStartRequest) -> bool {
    let placement = value.placement();
    let distributed = placement.world_size > 1;
    let utc_deadline = |deadline: &str| {
        chrono::DateTime::parse_from_rfc3339(deadline)
            .is_ok_and(|deadline| deadline.offset().local_minus_utc() == 0)
    };
    let valid_phase = match (&value.phase, &value.start_deadline) {
        // Role-ordered distributed starts are deliberately unphased.
        (None, None) => true,
        (Some(RecipeStartPhase::RankLaunch), Some(deadline)) => {
            distributed && utc_deadline(deadline)
        }
        (Some(RecipeStartPhase::CollectiveReadiness), Some(deadline)) => {
            distributed && value.is_endpoint_owner() && utc_deadline(deadline)
        }
        _ => false,
    };
    let rendezvous_valid = if distributed {
        placement.local_address.is_some_and(valid_fabric_address)
            && placement.master_address.is_some_and(valid_fabric_address)
            && placement.master_port.is_some_and(|port| port >= 1024)
    } else {
        placement.rank == 0
            && placement.local_address.is_none()
            && placement.master_address.is_none()
            && placement.master_port.is_none()
    };
    (1..=i64::MAX as u64).contains(&value.run_generation)
        && value.run_id.get_version() == Some(uuid::Version::Random)
        && value.installation_id.get_version() == Some(uuid::Version::Random)
        && value.recipe_revision_id.get_version() == Some(uuid::Version::Random)
        && value.mapping_id.get_version() == Some(uuid::Version::Random)
        && lower_hex(&value.plan_digest, 64)
        && value.compiled_execution_plan.endpoint.is_some()
        && placement.rank < placement.world_size
        && valid_role(&placement.role)
        && value.port().is_some_and(|port| port >= 1024)
        && placement.reserved_memory_bytes > 0
        && value.endpoint_address().is_some_and(|address| {
            !address.is_loopback()
                && !address.is_unspecified()
                && !address.is_multicast()
                && !link_local(address)
        })
        && rendezvous_valid
        && valid_phase
}

impl RecipeJobRunRequest {
    /// The job's single-rank placement as compiled into the plan.
    pub fn placement(&self) -> &compiled_execution_plan::CompiledRuntimePlacement {
        &self.compiled_execution_plan.runtime.placement
    }
}

impl RecipeStartRequest {
    /// The rank's placement as compiled into the plan.
    pub fn placement(&self) -> &compiled_execution_plan::CompiledRuntimePlacement {
        &self.compiled_execution_plan.runtime.placement
    }

    /// The serving port of this rank.
    pub fn port(&self) -> Option<u16> {
        self.placement().port
    }

    /// The address this rank serves on: the compiled endpoint address, or the
    /// rank's fabric address for a distributed rank without one.
    pub fn endpoint_address(&self) -> Option<std::net::IpAddr> {
        let placement = self.placement();
        placement.endpoint_address.or(if placement.world_size > 1 {
            placement.local_address
        } else {
            None
        })
    }

    /// Whether this rank owns the endpoint: every single-node rank, and the
    /// distributed rank whose fabric address is the rendezvous address.
    pub fn is_endpoint_owner(&self) -> bool {
        let placement = self.placement();
        placement.world_size == 1
            || (placement.local_address.is_some()
                && placement.local_address == placement.master_address)
    }

    /// The pinned runtime image digest (`sha256:<hex>`).
    pub fn image_digest(&self) -> &str {
        &self.compiled_execution_plan.runtime_image.image_digest
    }

    /// The immutable recipe revision content digest.
    pub fn recipe_content_sha256(&self) -> &str {
        &self.compiled_execution_plan.identity.recipe_revision_sha256
    }
}

pub(super) fn valid_job_slot(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 32
        && value.as_bytes()[0].is_ascii_alphabetic()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

pub(super) fn valid_job_file_name(value: &str) -> bool {
    !value.is_empty()
        && value != "manifest.json"
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

pub(super) fn valid_job_extension(value: &str) -> bool {
    let Some(value) = value.strip_prefix('.') else {
        return false;
    };
    !value.is_empty()
        && value.len() <= 16
        && (value.as_bytes()[0].is_ascii_lowercase() || value.as_bytes()[0].is_ascii_digit())
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
}

pub(super) fn valid_media_type(value: &str) -> bool {
    value.split_once('/').is_some_and(|(kind, subtype)| {
        let valid_part = |part: &str| {
            !part.is_empty()
                && part.len() <= 64
                && (part.as_bytes()[0].is_ascii_lowercase() || part.as_bytes()[0].is_ascii_digit())
                && part.bytes().all(|byte| {
                    byte.is_ascii_lowercase()
                        || byte.is_ascii_digit()
                        || matches!(
                            byte,
                            b'!' | b'#' | b'$' | b'&' | b'^' | b'_' | b'.' | b'+' | b'-'
                        )
                })
        };
        valid_part(kind) && valid_part(subtype)
    })
}

pub(super) fn validate_build(value: &RecipeBuildRequest) -> bool {
    lower_hex(&value.recipe_content_sha256, 64)
        && lower_hex(&value.source_bundle_sha256, 64)
        && lower_hex(&value.build_input_sha256, 64)
        && (1..=64 * 1024 * 1024).contains(&value.source_bundle_bytes)
        && valid_bundle_path(&value.dockerfile)
        && value.capabilities.len() <= 11
        && value
            .capabilities
            .iter()
            .enumerate()
            .all(|(index, capability)| {
                !capability.starts_with("SYS_")
                    && matches!(
                        capability.as_str(),
                        "CHOWN"
                            | "DAC_OVERRIDE"
                            | "FOWNER"
                            | "FSETID"
                            | "KILL"
                            | "MKNOD"
                            | "NET_BIND_SERVICE"
                            | "SETFCAP"
                            | "SETGID"
                            | "SETPCAP"
                            | "SETUID"
                    )
                    && !value.capabilities[..index].contains(capability)
            })
        && value.base_images.len() <= 8
        && value.base_images.iter().enumerate().all(|(index, image)| {
            valid_pinned_image(&image.reference, &image.manifest_digest)
                && !value.base_images[..index]
                    .iter()
                    .any(|prior| prior.reference == image.reference)
        })
        && if value.base_images.is_empty() {
            value.base_image_storage_bytes == 0
        } else {
            (1..=16 * 1024_u64.pow(4)).contains(&value.base_image_storage_bytes)
        }
        // An empty host list builds offline; otherwise only these public
        // hosts are reachable.
        && value.network.hosts.len() <= 64
        && value
            .network
            .hosts
            .iter()
            .enumerate()
            .all(|(index, host)| !value.network.hosts[..index].contains(host))
        && value
            .network
            .hosts
            .iter()
            .all(|host| valid_public_host(host))
        && valid_build_options(&value.options)
        && value.limits.cpu_cores >= 1
        && value.limits.cpu_cores <= 256
        && value.limits.memory_bytes > 0
        && value.limits.memory_bytes <= 16 * 1024_u64.pow(4)
        && value.limits.temporary_bytes > 0
        && value.limits.temporary_bytes <= 16 * 1024_u64.pow(4)
        && value.limits.processes >= 1
        && value.limits.timeout_seconds >= 1
        && value.limits.timeout_seconds <= 86_400
        && value.limits.output_bytes >= 1
        && value.limits.output_bytes <= 16 * 1024_u64.pow(4)
}

#[cfg(test)]
mod recipe_job_tests {
    use super::*;

    fn request() -> RecipeJobRunRequest {
        let inputs = vec![RecipeJobInputFile {
            slot: "input".to_owned(),
            name: "input.mp4".to_owned(),
            media_type: "video/mp4".to_owned(),
            size_bytes: 123,
            sha256: "a".repeat(64),
        }];
        let manifest = serde_json::json!({
            "schema_version": 1,
            "total_bytes": 123,
            "files": inputs,
        });
        RecipeJobRunRequest {
            job_id: Uuid::new_v4(),
            run_id: Uuid::new_v4(),
            installation_id: Uuid::new_v4(),
            recipe_revision_id: Uuid::new_v4(),
            mapping_id: Uuid::new_v4(),
            run_generation: 1,
            plan_digest: "d".repeat(64),
            input_manifest_sha256: hex_sha256(&canonical_json(&manifest).unwrap()),
            input_total_bytes: 123,
            inputs,
            compiled_execution_plan: serde_json::from_value(serde_json::from_str::<Value>(include_str!("../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json")).unwrap()["payload"]["compiled_execution_plan"].clone()).unwrap(),
            output_mappings: vec![RecipeJobOutputMapping {
                slot: "video".to_owned(),
                media_type: "video/mp4".to_owned(),
                extensions: vec![".mp4".to_owned()],
            }],
            output_limits: RecipeJobOutputLimits {
                max_files: 32,
                max_file_bytes: 1024 * 1024,
                max_total_bytes: 2 * 1024 * 1024,
                allowed_media_types: vec!["video/mp4".to_owned()],
            },
        }
    }

    #[test]
    fn job_contract_binds_canonical_inputs_and_requires_plan_object() {
        let valid = request();
        assert!(validate_recipe_job(&valid));

        let mut cross_manifest = valid.clone();
        cross_manifest.inputs[0].name = "other.mp4".to_owned();
        assert!(!validate_recipe_job(&cross_manifest));

        let mut traversal = valid.clone();
        traversal.inputs[0].name = "../input.mp4".to_owned();
        assert!(!validate_recipe_job(&traversal));

        let mut invalid_slot = valid.clone();
        invalid_slot.inputs[0].slot = "0input".to_owned();
        assert!(!validate_recipe_job(&invalid_slot));

        let mut reserved_manifest = valid.clone();
        reserved_manifest.inputs[0].name = "manifest.json".to_owned();
        let manifest = serde_json::json!({
            "schema_version": 1,
            "total_bytes": reserved_manifest.input_total_bytes,
            "files": reserved_manifest.inputs,
        });
        reserved_manifest.input_manifest_sha256 = hex_sha256(&canonical_json(&manifest).unwrap());
        assert!(!validate_recipe_job(&reserved_manifest));

        let mut command = serde_json::to_value(valid).unwrap();
        command["compiled_execution_plan"] = Value::Null;
        assert!(serde_json::from_value::<RecipeJobRunRequest>(command).is_err());
    }

    #[test]
    fn output_mapping_contract_is_sorted_exact_and_collision_free() {
        let mut valid = request();
        valid.output_mappings = vec![
            RecipeJobOutputMapping {
                slot: "custom".to_owned(),
                media_type: "application/vnd.vonk.custom".to_owned(),
                extensions: vec![".vonk.bin".to_owned()],
            },
            RecipeJobOutputMapping {
                slot: "document".to_owned(),
                media_type: "application/pdf".to_owned(),
                extensions: vec![".pdf".to_owned()],
            },
            RecipeJobOutputMapping {
                slot: "fallback".to_owned(),
                media_type: "application/octet-stream".to_owned(),
                extensions: vec![".bin".to_owned()],
            },
            RecipeJobOutputMapping {
                slot: "image".to_owned(),
                media_type: "image/avif".to_owned(),
                extensions: vec![".avif".to_owned()],
            },
        ];
        valid.output_limits.allowed_media_types = vec![
            "application/octet-stream".to_owned(),
            "application/pdf".to_owned(),
            "application/vnd.vonk.custom".to_owned(),
            "image/avif".to_owned(),
        ];
        assert!(validate_recipe_job(&valid));

        let mut collision = valid.clone();
        collision.output_mappings[3].extensions = vec![".pdf".to_owned()];
        assert!(!validate_recipe_job(&collision));

        let mut undeclared_limit = valid.clone();
        undeclared_limit.output_limits.allowed_media_types = vec!["video/mp4".to_owned()];
        assert!(!validate_recipe_job(&undeclared_limit));

        let mut uppercase = valid;
        uppercase.output_mappings[1].extensions = vec![".PDF".to_owned()];
        assert!(!validate_recipe_job(&uppercase));
    }

    #[test]
    fn python_job_claim_and_result_vectors_have_rust_parity() {
        let claim: AgentClaim = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
        ))
        .unwrap();
        claim.validate().unwrap();
        let parsed = RecipeOperationRequest::parse(&claim).unwrap();
        let RecipeOperationRequest::JobRun(request) = parsed else {
            panic!("job vector parsed as the wrong operation");
        };
        assert_eq!(
            request.input_manifest_sha256,
            "a3fa3ff4a07e23b945e72cda963e6aaf24671bc52642d328180b0ea4cde1776d"
        );

        let result: AgentResult = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
        ))
        .unwrap();
        result.validate().unwrap();
        let generated::AgentResultResult::RecipeJobRunResult(typed) = result.result else {
            panic!("expected typed job result")
        };
        typed.validate().unwrap();
        assert_eq!(
            typed.output_manifest.manifest_sha256,
            "9f7781fb8415bc1cb9e835fe4bcc9c8dd8f45f6a6b333f0e0550e812db1da9cd"
        );

        let mut unavailable_peak = typed;
        unavailable_peak.evidence.peak_memory_bytes = None;
        unavailable_peak.validate().unwrap();
        assert!(
            serde_json::to_value(unavailable_peak).unwrap()["evidence"]["peak_memory_bytes"]
                .is_null()
        );
    }

    #[test]
    fn cancelled_is_a_typed_terminal_agent_result_state() {
        let result = AgentResult {
            fence: Uuid::new_v4(),
            result: serde_json::from_value(
                serde_json::json!({"reason": "controller cancellation requested"}),
            )
            .unwrap(),
            state: "cancelled".parse().unwrap(),
        };

        result.validate().unwrap();
    }
}
