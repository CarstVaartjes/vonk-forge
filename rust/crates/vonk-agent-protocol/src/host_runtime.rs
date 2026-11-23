//! Host runtime.

use super::*;

/// The authoritative ceiling on one privileged-helper frame, in bytes.
///
/// A frame is one signed helper message: the agent canonicalizes the signed
/// grant, frames it, and the helper allocates exactly the framed length before
/// it parses. This is the bound on the *grant*, not on the runtime request the
/// grant authorizes. The grant carries the four identities and the request
/// digest; the request document itself is never framed (see
/// [`MAX_HOST_RUNTIME_REQUEST_BYTES`]). The worst case is one framed grant plus
/// one framed reply and their parsed forms -- a few MiB, acceptable on a node
/// that already stages multi-gigabyte images.
pub const MAX_HELPER_FRAME_BYTES: usize = 1024 * 1024;
/// The authoritative ceiling on one canonical [`HostRuntimeRequest`], in bytes.
///
/// A request carries its exact typed Start, `JobRun`, or Stop plan plus projected
/// argv. The nested compiled plan is separately limited to 16 MiB and its
/// enclosing typed claim to 18 MiB; one helper frame of additional space bounds
/// the remaining request envelope. The helper reads the canonical request from
/// the agent's owner-only request file after verifying its signed digest.
pub const MAX_HOST_RUNTIME_REQUEST_BYTES: usize =
    MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES + MAX_HELPER_FRAME_BYTES;
/// The bytes a canonical [`HostRuntimeRequest`] spends on everything but its
/// argument payload: field names, identity and version fields, optional
/// typed-plan keys, and array syntax.
///
/// The projected argv's `MAX_ARGV_BYTES` remains separately bounded by one
/// helper frame minus this envelope. The complete request can be larger because
/// it also carries the exact typed plan and compiled execution claim. The value
/// is a margin above a measured maximum, never merely equal to one:
/// `the_declared_envelope_covers_the_largest_contract_permitted_request`
/// re-measures the largest contract-permitted envelope and fails if it
/// outgrows this constant.
pub const HOST_RUNTIME_REQUEST_ENVELOPE_BYTES: usize = 16 * 1024;
/// The ceiling on one generic claim payload or progress document.
///
/// The load-bearing case is an `artifact.distribution.v1` claim, whose
/// assignment admits up to 4096 distribution objects; at the measured ~120
/// bytes per object a full assignment is roughly 0.5 MiB, so the previous
/// 64 KiB was a latent refusal for a large model (the real GLM shape is ~150
/// objects, ~18 KiB -- only 3x under the old ceiling). 2 MiB is a backstop
/// above the admitted assignment; the compiled plan document is separately
/// bounded below.
pub const MAX_DOCUMENT_BYTES: usize = 2 * 1024 * 1024;
/// The ceiling on the compiled execution plan document.
///
/// This is the largest document on the wire, so it keeps the largest bound.
/// The plan admits 4096 artifacts; at the measured marginal cost of a fixture
/// artifact (~1.2 KiB) that is roughly 5 MiB, and the largest real plan (the
/// GLM EXL3 dual shape, 149 artifacts) is about 180 KiB. 16 MiB is already
/// more than 3x the admitted maximum and ~90x the real maximum, so it is kept.
/// It also bounds the agent's single largest HTTP body allocation, which is
/// why it is not raised further.
pub const MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES: usize =
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES + MAX_DOCUMENT_BYTES;
pub const HOST_HELPER_AUTHORITY: &str = "vonk.host-maintenance-helper";
/// Agent-written requests consumed by the authenticated privileged helper.
pub const HOST_RUNTIME_REQUEST_ROOT: &str = "/run/vonk-forge-agent/runtime-requests";
pub(super) const HOST_HELPER_GRANT_DOMAIN: &[u8] = b"VONK-HOST-MAINTENANCE-HELPER-GRANT-V1\0";

impl HostHelperOperation {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        canonical_generated_json(self)?;
        let valid = match self {
            Self::InstallVonkDebOperation(operation) => {
                operation.rollback.valid()
                    && operation.rollback.source.package_sha256 != operation.package_sha256
            }
            Self::ConfirmPackageActivationOperation(_) => true,
            Self::ApplyGpuClockOperation(operation) => {
                operation.desired.range.min_mhz <= operation.desired.range.max_mhz
            }
            Self::ApplySwapOperation(operation) => swap::valid_intent(&operation.desired),
            Self::ExecuteContainerRuntimeRequestOperation(operation) => {
                operation.fence.get_version() == Some(uuid::Version::Random)
                    && (operation.installation_id.is_some()
                        == (operation.action
                            == HostHelperContainerRuntimeAction::InstallationCleanup))
                    && operation
                        .reconciliation_identity
                        .as_ref()
                        .is_none_or(|identity| {
                            operation.action
                                == HostHelperContainerRuntimeAction::InstallationCleanup
                                && operation.installation_id == Some(identity.installation_id)
                                && valid_reconciliation_identity(identity)
                        })
            }
        };
        if valid {
            Ok(())
        } else {
            Err(ProtocolError::Identity("host helper operation"))
        }
    }
}

impl HostHelperGrantClaims {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || self.authority != HOST_HELPER_AUTHORITY
            || self.request_id.get_version() != Some(uuid::Version::Random)
            || !valid_node_id(&self.node_id)
            || self.issued_at <= 0
            || !(1..=300).contains(&(self.expires_at - self.issued_at))
        {
            return Err(ProtocolError::Identity("host helper grant claims"));
        }
        self.operation.validate()
    }
}

impl SignedHostHelperGrant {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        self.claims.validate()?;
        if self.schema_version != 1
            || self.signature.algorithm != "ed25519"
            || !lower_hex(&self.signature.key_id, 64)
            || !lower_hex(&self.signature.value, 128)
        {
            return Err(ProtocolError::Identity("signed host helper grant"));
        }
        Ok(())
    }
}

pub fn host_helper_grant_signing_bytes(
    claims: &HostHelperGrantClaims,
) -> Result<Vec<u8>, ProtocolError> {
    claims.validate()?;
    let mut value = HOST_HELPER_GRANT_DOMAIN.to_vec();
    value.extend(canonical_json(claims)?);
    Ok(value)
}

/// One distinct contract of an agent-built [`HostRuntimeRequest`].
///
/// `validate()` used to answer every violation with one opaque `ProtocolError`,
/// so the agent could only report `helper_request_document_invalid` no matter
/// which rule refused. A live blocked Start could not say whether the typed
/// plan, argument envelope, or one argument's value was wrong.
///
/// A rule that measures a bound carries it, so a refusal can report the limit
/// and the observed value without ever carrying the argument itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum HostRuntimeRequestRule {
    /// Arguments, or a run-inspect target, are present or absent for the wrong
    /// action.
    #[error("host runtime request argument presence is invalid")]
    ArgumentsPresence,
    /// A typed start/stop plan is absent, duplicated, or bound to another
    /// generation.
    #[error("host runtime request plan binding is invalid")]
    PlanBinding,
    /// An installation identity is present or absent for the wrong action.
    #[error("host runtime request installation identity is invalid")]
    InstallationIdentity,
    /// The canonical request document is larger than the bounded helper
    /// exchange reads. The budget is a byte budget, so a count ceiling is
    /// deliberately absent: every argument costs at least three canonical
    /// bytes, so this rule always fires before any derived count could.
    #[error("host runtime request document exceeds the bounded exchange")]
    RequestBytes { limit: u64, observed: u64 },
    /// A typed plan or its nested compiled plan exceeds its declared ceiling.
    #[error("host runtime request plan exceeds its canonical byte ceiling")]
    PlanBytes { limit: u64, observed: u64 },
    /// An argument carries a NUL byte, which an exec argv cannot frame.
    #[error("host runtime request argument carries a NUL byte")]
    ArgumentNulByte { observed: u64 },
    /// The arguments could not be canonicalized.
    #[error("host runtime request arguments could not be encoded")]
    Encoding,
}

impl HostRuntimeRequestRule {
    /// The limit and the observed value of a rule that measured one.
    ///
    /// `None` means the rule is not a measured bound. A `None` limit is a rule
    /// with no numeric ceiling (a refused byte, not a refused length).
    pub fn bound(self) -> Option<(Option<u64>, u64)> {
        match self {
            Self::RequestBytes { limit, observed } | Self::PlanBytes { limit, observed } => {
                Some((Some(limit), observed))
            }
            Self::ArgumentNulByte { observed } => Some((None, observed)),
            _ => None,
        }
    }
}

impl HostRuntimeRequest {
    pub fn validate(&self) -> Result<(), HostRuntimeRequestRule> {
        if self.arguments.is_empty()
            != matches!(
                self.action,
                HostRuntimeAction::RuntimePreflight
                    | HostRuntimeAction::Stop
                    | HostRuntimeAction::InstallationCleanup
                    | HostRuntimeAction::RunInspect
            )
        {
            return Err(HostRuntimeRequestRule::ArgumentsPresence);
        }
        if self.inspect_target.is_some() != (self.action == HostRuntimeAction::RunInspect) {
            return Err(HostRuntimeRequestRule::ArgumentsPresence);
        }
        if self.installation_id.is_some() != (self.action == HostRuntimeAction::InstallationCleanup)
        {
            return Err(HostRuntimeRequestRule::InstallationIdentity);
        }
        match (&self.action, &self.reconciliation_identity) {
            (_, None) => {}
            (HostRuntimeAction::InstallationCleanup, Some(identity))
                if valid_reconciliation_identity(identity)
                    && self.installation_id == Some(identity.installation_id) => {}
            _ => return Err(HostRuntimeRequestRule::InstallationIdentity),
        }
        match self.action {
            HostRuntimeAction::Start => {
                if (self.start_plan.is_none() == self.job_plan.is_none())
                    || self.stop_plan.is_some()
                {
                    return Err(HostRuntimeRequestRule::PlanBinding);
                }
                if let Some(plan) = &self.start_plan {
                    if self.run_generation != Some(plan.run_generation) {
                        return Err(HostRuntimeRequestRule::PlanBinding);
                    }
                    validate_runtime_plan_size(plan, &plan.compiled_execution_plan)?;
                } else if let Some(plan) = &self.job_plan {
                    if self.run_generation != Some(plan.run_generation) {
                        return Err(HostRuntimeRequestRule::PlanBinding);
                    }
                    validate_runtime_plan_size(plan, &plan.compiled_execution_plan)?;
                }
            }
            HostRuntimeAction::Stop => {
                let Some(plan) = &self.stop_plan else {
                    return Err(HostRuntimeRequestRule::PlanBinding);
                };
                if self.start_plan.is_some()
                    || self.job_plan.is_some()
                    || self.run_generation != Some(plan.run_generation)
                {
                    return Err(HostRuntimeRequestRule::PlanBinding);
                }
                let encoded = canonical_json(plan).map_err(|_| HostRuntimeRequestRule::Encoding)?;
                if encoded.len() > MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES {
                    return Err(HostRuntimeRequestRule::PlanBytes {
                        limit: MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES as u64,
                        observed: encoded.len() as u64,
                    });
                }
            }
            _ if self.start_plan.is_some()
                || self.job_plan.is_some()
                || self.stop_plan.is_some()
                // Only an inspection of an exact Start container may name the
                // generation its label carries.
                || self.run_generation.is_some_and(|generation| {
                    !matches!(
                        self.inspect_target,
                        Some(RunInspectTarget::ContainerRunTarget(_))
                    ) || !(1..=i64::MAX as u64).contains(&generation)
                }) =>
            {
                return Err(HostRuntimeRequestRule::PlanBinding);
            }
            _ => {}
        }
        let encoded = canonical_json(self).map_err(|_| HostRuntimeRequestRule::Encoding)?;
        if encoded.len() > MAX_HOST_RUNTIME_REQUEST_BYTES {
            return Err(HostRuntimeRequestRule::RequestBytes {
                limit: MAX_HOST_RUNTIME_REQUEST_BYTES as u64,
                observed: encoded.len() as u64,
            });
        }
        let (image, command): (Option<&String>, &[String]) = match &self.inspect_target {
            Some(RunInspectTarget::ContainerRunTarget(target)) => {
                (Some(&target.image_reference), &target.command)
            }
            _ => (None, &[]),
        };
        for value in self.arguments.iter().chain(image).chain(command) {
            if value.contains('\0') {
                return Err(HostRuntimeRequestRule::ArgumentNulByte {
                    observed: value.len() as u64,
                });
            }
        }
        Ok(())
    }
}

pub(super) fn validate_runtime_plan_size<T: Serialize, C: Serialize>(
    plan: &T,
    compiled_execution_plan: &C,
) -> Result<(), HostRuntimeRequestRule> {
    let compiled_bytes =
        canonical_json(compiled_execution_plan).map_err(|_| HostRuntimeRequestRule::Encoding)?;
    if compiled_bytes.len() > MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES {
        return Err(HostRuntimeRequestRule::PlanBytes {
            limit: MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES as u64,
            observed: compiled_bytes.len() as u64,
        });
    }
    let plan_bytes = canonical_json(plan).map_err(|_| HostRuntimeRequestRule::Encoding)?;
    if plan_bytes.len() > MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES {
        return Err(HostRuntimeRequestRule::PlanBytes {
            limit: MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES as u64,
            observed: plan_bytes.len() as u64,
        });
    }
    Ok(())
}

#[cfg(test)]
mod installation_cleanup_contract_tests;

/// The bounds that decide whether a Start is framed at all.
///
/// Every test here names the wrong implementation it refuses to accept, so a
/// future change cannot quietly restore a round-number cap beside the byte
/// budget.
#[cfg(test)]
mod host_runtime_request_bound_tests;

#[cfg(test)]
mod host_helper_reconciliation_identity_tests;

/// The vectors the Python contract also reads: every `run-inspect` target the
/// agent can write is read back by the helper's reader byte for byte, and every
/// shape the contract refuses is refused here too.
#[cfg(test)]
mod run_inspect_target_vector_tests;
