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
/// A request carries its exact typed Start, JobRun, or Stop plan plus projected
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
    /// Arguments are present or absent for the wrong action.
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
            )
        {
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
                || self.run_generation.is_some() =>
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
        for value in &self.arguments {
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
mod installation_cleanup_contract_tests {
    use super::*;

    fn request(action: HostRuntimeAction, installation_id: Option<Uuid>) -> HostRuntimeRequest {
        HostRuntimeRequest {
            action,
            fence: Uuid::new_v4(),
            arguments: Vec::new(),
            job_plan: None,
            installation_id,
            reconciliation_identity: None,
            run_generation: None,
            start_plan: None,
            stop_plan: None,
        }
    }

    #[test]
    fn cleanup_requires_one_installation_identity_and_other_actions_reject_it() {
        let installation_id = Uuid::new_v4();
        assert!(
            request(
                HostRuntimeAction::InstallationCleanup,
                Some(installation_id)
            )
            .validate()
            .is_ok()
        );
        assert!(
            request(HostRuntimeAction::InstallationCleanup, None)
                .validate()
                .is_err()
        );
        let mut ordinary = request(HostRuntimeAction::Start, Some(installation_id));
        ordinary.arguments.push("run".to_owned());
        assert!(ordinary.validate().is_err());

        for raw in [
            serde_json::json!({
                "action": "installation-cleanup",
                "fence": Uuid::new_v4(),
                "arguments": [],
            }),
            serde_json::json!({
                "action": "installation-cleanup",
                "fence": Uuid::new_v4(),
                "arguments": [],
                "installation_id": null,
            }),
        ] {
            let parsed: HostRuntimeRequest = serde_json::from_value(raw).unwrap();
            assert!(parsed.validate().is_err());
        }
    }
}

/// The bounds that decide whether a Start is framed at all.
///
/// Every test here names the wrong implementation it refuses to accept, so a
/// future change cannot quietly restore a round-number cap beside the byte
/// budget.
#[cfg(test)]
mod host_runtime_request_bound_tests {
    use super::{
        HOST_RUNTIME_REQUEST_ENVELOPE_BYTES, HostRuntimeAction, HostRuntimeRequest,
        HostRuntimeRequestRule, MAX_HOST_RUNTIME_REQUEST_BYTES, canonical_json,
    };
    use uuid::Uuid;

    fn start(arguments: Vec<String>) -> HostRuntimeRequest {
        let plan = super::recipe_start_tests::valid_start_plan();
        HostRuntimeRequest {
            action: HostRuntimeAction::Start,
            fence: Uuid::new_v4(),
            arguments,
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: Some(plan.run_generation),
            start_plan: Some(plan),
            stop_plan: None,
        }
    }

    fn canonical_length(request: &HostRuntimeRequest) -> usize {
        canonical_json(request)
            .expect("a host runtime request must canonically encode")
            .len()
    }

    #[test]
    fn the_declared_envelope_covers_the_largest_contract_permitted_request() {
        // Wrong implementation: `MAX_ARGV_BYTES` was set equal to the request
        // ceiling while its comment called it a backstop "below" that ceiling,
        // so an argv inside the plan's own budget could still be unframeable.
        // Re-measure the envelope rather than restating the subtraction.
        let request = HostRuntimeRequest {
            action: HostRuntimeAction::RunInspect,
            fence: Uuid::new_v4(),
            arguments: Vec::new(),
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: None,
            start_plan: None,
            stop_plan: None,
        };
        let measured = canonical_length(&request);
        assert!(
            measured <= HOST_RUNTIME_REQUEST_ENVELOPE_BYTES,
            "the measured envelope {measured} outgrew the declared \
             HOST_RUNTIME_REQUEST_ENVELOPE_BYTES {HOST_RUNTIME_REQUEST_ENVELOPE_BYTES}, \
             so the derived MAX_ARGV_BYTES no longer leaves room for the request around it"
        );
    }

    #[test]
    fn the_exchange_ceiling_is_admitted_and_one_byte_over_is_refused_with_both_numbers() {
        // Wrong implementation: the request had no byte bound at all, so the
        // helper's private `64 * 1024` read cap refused it opaquely as
        // `helper.unsafe_path` after the agent had called it valid.
        let base = start(vec!["sha256:image".to_owned()]);
        let base_length = canonical_length(&base);
        let filler = MAX_HOST_RUNTIME_REQUEST_BYTES - base_length - 3;

        let mut at_limit = base.clone();
        at_limit.arguments.push("x".repeat(filler));
        assert_eq!(canonical_length(&at_limit), MAX_HOST_RUNTIME_REQUEST_BYTES);
        assert_eq!(at_limit.validate(), Ok(()));

        let mut over_limit = base;
        over_limit.arguments.push("x".repeat(filler + 1));
        assert_eq!(
            canonical_length(&over_limit),
            MAX_HOST_RUNTIME_REQUEST_BYTES + 1
        );
        assert_eq!(
            over_limit.validate(),
            Err(HostRuntimeRequestRule::RequestBytes {
                limit: MAX_HOST_RUNTIME_REQUEST_BYTES as u64,
                observed: MAX_HOST_RUNTIME_REQUEST_BYTES as u64 + 1,
            })
        );
    }

    #[test]
    fn a_command_line_above_the_retired_count_ceiling_is_admitted() {
        // Wrong implementation: `MAX_HOST_RUNTIME_ARGUMENTS = 4096` refused a
        // legitimate command line of 6000 short elements while the canonical
        // request was tens of kilobytes inside the byte budget. The plan
        // projects two arguments per mounted file at an artifact ceiling of
        // 4096, so a count this large is admitted by the plan contract.
        let arguments = (0..6000)
            .map(|index| format!("--mount=type=bind,src={index:05}"))
            .collect();
        let request = start(arguments);
        assert!(canonical_length(&request) < MAX_HOST_RUNTIME_REQUEST_BYTES);
        assert_eq!(request.validate(), Ok(()));
    }

    #[test]
    fn an_empty_argument_and_a_multiline_argument_are_legal_bytes() {
        // Wrong implementation: an argument an exec argv can carry -- empty, or
        // one with CR/LF -- was refused for carrying it, though the plan's own
        // opaque-argv contract permits it byte for byte.
        let request = start(vec![
            "sha256:image".to_owned(),
            String::new(),
            "line one\nline two\r\n".to_owned(),
            String::new(),
        ]);
        assert_eq!(request.validate(), Ok(()));
    }
}

#[cfg(test)]
mod host_helper_reconciliation_identity_tests {
    use super::*;

    fn identity() -> RecipeReconciliationIdentity {
        RecipeReconciliationIdentity {
            installation_id: Uuid::new_v4(),
            plan_digest: "b".repeat(64),
        }
    }

    fn claims(identity: RecipeReconciliationIdentity) -> HostHelperGrantClaims {
        HostHelperGrantClaims {
            schema_version: 1,
            authority: HOST_HELPER_AUTHORITY.to_owned(),
            request_id: Uuid::new_v4(),
            node_id: "spk_11111111111111111111111111111111".to_owned(),
            issued_at: 2_100_000_000,
            expires_at: 2_100_000_060,
            operation: HostHelperOperation::ExecuteContainerRuntimeRequestOperation(
                generated::ExecuteContainerRuntimeRequestOperation {
                    type_: "execute-container-runtime-request".into(),
                    installation_intent_nonce: None,
                    installation_intent_ordinal: Some(1),
                    action: HostHelperContainerRuntimeAction::InstallationCleanup,
                    fence: Uuid::new_v4(),
                    request_sha256: "e".repeat(64),
                    installation_id: Some(identity.installation_id),
                    reconciliation_identity: Some(identity),
                    run_generation: None,
                    runtime_installation_id: None,
                    runtime_run_id: None,
                    runtime_target_id: None,
                    start_plan_sha256: None,
                    stop_plan_sha256: None,
                },
            ),
        }
    }

    #[test]
    fn signed_cleanup_grant_identity_is_strictly_bound_to_action_and_install() {
        let exact = claims(identity());
        exact.validate().unwrap();

        let mut wrong_installation = exact.clone();
        let HostHelperOperation::ExecuteContainerRuntimeRequestOperation(operation) =
            &mut wrong_installation.operation
        else {
            unreachable!();
        };
        operation.installation_id = Some(Uuid::from_u128(8));
        assert!(wrong_installation.validate().is_err());

        let mut wrong_action = exact;
        let HostHelperOperation::ExecuteContainerRuntimeRequestOperation(operation) =
            &mut wrong_action.operation
        else {
            unreachable!();
        };
        operation.action = HostHelperContainerRuntimeAction::Start;
        operation.installation_id = None;
        assert!(wrong_action.validate().is_err());
    }
}
