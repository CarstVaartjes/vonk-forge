use super::*;

pub(super) fn require_bound_response(
    response: &HelperResponse,
    request_id: &str,
) -> Result<(), HostRuntimeError> {
    if response.schema_version != 1 {
        return Err(HostRuntimeError::HelperProtocol(
            HelperProtocolCause::ResponseUnbound,
        ));
    }
    if response.request_id.map(|id| id.to_string()).as_deref() == Some(request_id) {
        return Ok(());
    }
    if response.request_id.is_none()
        && response
            .error_code
            .as_deref()
            .is_some_and(unbound_rejection_is_expected)
    {
        return Ok(());
    }
    Err(HostRuntimeError::HelperProtocol(
        HelperProtocolCause::ResponseUnbound,
    ))
}

/// The codes the helper can only raise before it has trusted the grant, and so
/// the only rejections that legitimately arrive without the request identity.
/// Every other code is produced after the helper knows the request, so an
/// unbound reply claiming one is a reply this agent cannot account for.
fn unbound_rejection_is_expected(code: &str) -> bool {
    crate::helper_codes::runtime_rejection(code)
        .is_some_and(crate::helper_codes::is_unbound_rejection)
}

/// The executed-outcome contract. A successful helper reply carries no
/// rejection, no capture diagnostic and no inspection outcome, names the
/// executed status unless it is the deliberate stop-uncertain outcome, and
/// reports a 64-character lowercase evidence digest with, at most, a byte-sized
/// exit code.
pub(super) fn require_executed_outcome(
    response: &HelperResponse,
    stop_uncertain: bool,
) -> Result<(), HostRuntimeError> {
    let malformed = || HostRuntimeError::HelperProtocol(HelperProtocolCause::OutcomeMalformed);
    if response.process_running.is_some() || response.error_code.is_some() {
        return Err(malformed());
    }
    // An exit account and output accompany only a job that did not exit
    // cleanly; they are evidence of that exit, never of a clean one.
    if response.installation_intent_nonce.is_some() {
        return Err(malformed());
    }
    let evidence = response.diagnostic.is_some() || response.process_logs.is_some();
    if evidence && !response.exit_code.is_some_and(|code| code != 0)
        || !stop_uncertain
            && response.status != HostHelperResponseStatus::ContainerRuntimeRequestExecuted
    {
        return Err(malformed());
    }
    if response
        .exit_code
        .is_some_and(|code| !(0..=255).contains(&code))
    {
        return Err(malformed());
    }
    Ok(())
}

pub(super) fn runtime_rejection(
    response: &HelperResponse,
    action: HostRuntimeAction,
) -> HostRuntimeError {
    let Some(code) = response
        .error_code
        .as_deref()
        .and_then(crate::helper_codes::runtime_rejection)
    else {
        return HostRuntimeError::HelperProtocol(HelperProtocolCause::RejectionMalformed);
    };
    if response.status != HostHelperResponseStatus::Rejected
        || response.exit_code.is_some()
        || response.process_running.is_some()
        || response.diagnostic.is_some()
            && (action != HostRuntimeAction::RunInspect
                || code != HelperErrorCode::RuntimeProcessExited)
    {
        return HostRuntimeError::HelperProtocol(HelperProtocolCause::RejectionMalformed);
    }
    HostRuntimeError::HelperRejected {
        code,
        diagnostic: response
            .diagnostic
            .as_deref()
            .map(crate::failure_evidence::sanitize_text),
        process_logs: response.process_logs.as_ref().map(|logs| {
            Box::new(FailureProcessLogs {
                stdout: sanitize_tail(&logs.stdout),
                stderr: sanitize_tail(&logs.stderr),
            })
        }),
    }
}

/// The finding code of a helper code.  A code the runtime boundary does not
/// accept keeps the opaque `helper_protocol_invalid` label.
pub(super) fn helper_finding_code(code: HelperErrorCode) -> RuntimePreflightFindingCode {
    use RuntimePreflightFindingCode as Finding;
    match code {
        HelperErrorCode::GrantInvalid => Finding::PreflightFindingHelperGrantInvalid,
        HelperErrorCode::GrantNodeMismatch => Finding::PreflightFindingHelperGrantNodeMismatch,
        HelperErrorCode::GrantUnauthorized => Finding::PreflightFindingHelperGrantUnauthorized,
        HelperErrorCode::PeerIdentityInvalid => Finding::PreflightFindingHelperPeerIdentityInvalid,
        HelperErrorCode::OperationInvalidArtifact => {
            Finding::PreflightFindingHelperOperationInvalidArtifact
        }
        HelperErrorCode::RuntimeImageIdentityInvalid => {
            Finding::PreflightFindingHelperRuntimeImageIdentityInvalid
        }
        HelperErrorCode::RequestReplayed => Finding::PreflightFindingHelperRequestReplayed,
        HelperErrorCode::OperationFailed => Finding::PreflightFindingHelperOperationFailed,
        HelperErrorCode::InstallationReconciliationBusy => {
            Finding::PreflightFindingHelperInstallationReconciliationBusy
        }
        HelperErrorCode::OperationInvalid => Finding::PreflightFindingHelperOperationInvalid,
        HelperErrorCode::OperationUnsafePath => Finding::PreflightFindingHelperOperationUnsafePath,
        HelperErrorCode::OperationCommandFailed => {
            Finding::PreflightFindingHelperOperationCommandFailed
        }
        HelperErrorCode::OperationStopUncertain => {
            Finding::PreflightFindingHelperOperationStopUncertain
        }
        HelperErrorCode::OperationIo => Finding::PreflightFindingHelperOperationIo,
        HelperErrorCode::RuntimeImageLoadFailed => {
            Finding::PreflightFindingHelperRuntimeImageLoadFailed
        }
        HelperErrorCode::RuntimeImageInspectFailed => {
            Finding::PreflightFindingHelperRuntimeImageInspectFailed
        }
        HelperErrorCode::RuntimeImageReceiptFailed => {
            Finding::PreflightFindingHelperRuntimeImageReceiptFailed
        }
        HelperErrorCode::RuntimeProcessExited => {
            Finding::PreflightFindingHelperRuntimeProcessExited
        }
        HelperErrorCode::RuntimeRunMissing => Finding::PreflightFindingHelperRuntimeRunMissing,
        HelperErrorCode::RuntimeFabricUnavailable => {
            Finding::PreflightFindingHelperRuntimeFabricUnavailable
        }
        HelperErrorCode::RuntimeFabricFirewallRejected => {
            Finding::PreflightFindingHelperRuntimeFabricFirewallRejected
        }
        HelperErrorCode::RuntimeEndpointFirewallRejected => {
            Finding::PreflightFindingHelperRuntimeEndpointFirewallRejected
        }
        HelperErrorCode::InstallationReconciliationStorageUnavailable => {
            Finding::PreflightFindingHelperInstallationReconciliationStorageUnavailable
        }
        HelperErrorCode::RequestInvalid => Finding::PreflightFindingHelperRequestInvalid,
        HelperErrorCode::RequestLedgerFailed => Finding::PreflightFindingHelperRequestLedgerFailed,
        HelperErrorCode::RequestEncodingInvalid => {
            Finding::PreflightFindingHelperRequestEncodingInvalid
        }
        HelperErrorCode::CallJoinFailed => Finding::PreflightFindingHelperCallJoinFailed,
        HelperErrorCode::MessageFramingInvalid => {
            Finding::PreflightFindingHelperMessageFramingInvalid
        }
        HelperErrorCode::ResponseUnbound => Finding::PreflightFindingHelperResponseUnbound,
        HelperErrorCode::RejectionMalformed => Finding::PreflightFindingHelperRejectionMalformed,
        HelperErrorCode::OutcomeMalformed => Finding::PreflightFindingHelperOutcomeMalformed,
        HelperErrorCode::RequestDocumentInvalid => {
            Finding::PreflightFindingHelperRequestDocumentInvalid
        }
        HelperErrorCode::RequestSchemaVersionInvalid => {
            Finding::PreflightFindingHelperRequestSchemaVersionInvalid
        }
        HelperErrorCode::RequestAttemptInvalid => {
            Finding::PreflightFindingHelperRequestAttemptInvalid
        }
        HelperErrorCode::RequestArgumentsPresenceInvalid => {
            Finding::PreflightFindingHelperRequestArgumentsPresenceInvalid
        }
        HelperErrorCode::RequestPlanBindingInvalid => {
            Finding::PreflightFindingHelperRequestPlanBindingInvalid
        }
        HelperErrorCode::RequestInstallationIdentityInvalid => {
            Finding::PreflightFindingHelperRequestInstallationIdentityInvalid
        }
        HelperErrorCode::RequestBytesInvalid => Finding::PreflightFindingHelperRequestBytesInvalid,
        HelperErrorCode::RequestPlanBytesInvalid => {
            Finding::PreflightFindingHelperRequestPlanBytesInvalid
        }
        HelperErrorCode::RequestArgumentNulByte => {
            Finding::PreflightFindingHelperRequestArgumentNulByte
        }
        HelperErrorCode::RequestStorageInvalid => {
            Finding::PreflightFindingHelperRequestStorageInvalid
        }
        HelperErrorCode::SystemClockInvalid => Finding::PreflightFindingHelperSystemClockInvalid,
        HelperErrorCode::InspectionOutcomeInvalid => {
            Finding::PreflightFindingHelperInspectionOutcomeInvalid
        }
        _ => Finding::PreflightFindingHelperProtocolInvalid,
    }
}

/// The finding code without its `preflight_finding.` domain prefix: the word
/// an operator reads in an admission receipt.
pub fn finding_word(code: RuntimePreflightFindingCode) -> String {
    let word = code.to_string();
    word.strip_prefix("preflight_finding.")
        .map_or(word.clone(), str::to_owned)
}
