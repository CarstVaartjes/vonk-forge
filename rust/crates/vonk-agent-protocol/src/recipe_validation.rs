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
        .is_ok_and(|bytes| hex_sha256(&bytes) == value.input_manifest_sha256);
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
    if value
        .gpu_clock
        .as_ref()
        .is_some_and(|clock| !gpu_clock::valid_range(&clock.range))
    {
        return false;
    }
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
    /// Whether this rank owns the job's inputs, outputs and result. The other
    /// ranks of a multi-rank job only compute.
    pub fn is_endpoint_owner(&self) -> bool {
        self.compiled_execution_plan
            .runtime
            .placement
            .is_endpoint_owner()
    }

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
        self.placement().is_endpoint_owner()
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
mod recipe_job_tests;
